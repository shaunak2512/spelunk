"""Fetch a REST/JSON API into a local NDJSON snapshot — the ``api:`` source kind.

Design (see design_join_discovery_and_notebook.md §3.3): an API source is *snapshot-on-attach*.
The endpoint is fetched once, at attach time — with pagination, retries, and rate-limit backoff
— into ``<workspace>/snapshots/<name>.ndjson``, and the source's view reads that local file.
Queries and ``replay`` therefore run against a pinned, deterministic snapshot (no re-fetch per
query, no mid-pipeline rate-limit surprises); refreshing is an explicit re-attach. Fetching uses
stdlib ``urllib`` only (consistent with the no-SQLAlchemy ethos — no new dependency).

Spec grammar (the part after the ``api:`` scheme prefix)::

    api:<url> [key=value ...]

    gh=api:https://api.github.com/repos/duckdb/duckdb/issues paginate=link
    pokemon=api:https://pokeapi.co/api/v2/pokemon records=results paginate=cursor cursor_path=next
    posts=api:https://example.com/posts paginate=page page_param=page size_param=per_page page_size=100

Options (whitespace-separated ``key=value`` after the URL; unknown keys are rejected):

* ``records=<dot.path>`` — where the record array lives in each response (``results``,
  ``data.items``). Default: the response itself if it is an array; for an object, a common
  wrapper key (results/data/items/records/rows), else the single list-valued key, else the
  whole object as one record.
* ``paginate=none|page|offset|cursor|keyset|link`` — pagination style (default ``none``, one
  request):
  - ``page``   — a page-number query param (``page_param``, default ``page``; first page
                 ``start``, default 1). Stops on an empty page.
  - ``offset`` — offset/limit params (``offset_param`` default ``offset``, ``size_param``
                 default ``limit``, ``page_size`` default 100). Stops when a page comes back
                 short.
  - ``cursor`` — the response carries the next cursor at ``cursor_path`` (dot path). A cursor
                 that is itself an absolute URL is followed directly (PokeAPI-style ``next``);
                 otherwise it is sent as the ``cursor_param`` query param. Stops when the
                 cursor is null/absent.
  - ``keyset`` — seek pagination (Stripe's ``starting_after``, ``since_id`` feeds): the next
                 request sends a field of the *last record* — ``keyset_field`` (dot path into
                 the record) — as the ``cursor_param`` query param, verbatim (Stripe's
                 exclusive-of-the-given-id convention). Stops on an empty page, a short page
                 (when ``size_param`` is set), or a last record missing the field.
  - ``link``   — follow the RFC-5988 ``Link: <...>; rel="next"`` response header (GitHub
                 style). Stops when no ``next`` link remains.
  - ``odata``  — sugar for the OData v4 conventions: ``records=value`` and the next page at
                 the literal key ``@odata.nextLink`` (absolute or relative URL), i.e. cursor
                 style pre-configured. Explicit ``records=``/``cursor_path=`` override, so an
                 OData v2 service works with ``records=d.results cursor_path=d.__next``.
                 NB: ``$filter`` values contain spaces — percent-encode them in the URL
                 (``$filter=Amount%20gt%20100``), since spec options split on whitespace.
* ``page_param`` / ``start`` / ``size_param`` / ``page_size`` / ``offset_param`` /
  ``cursor_param`` / ``cursor_path`` / ``keyset_field`` — style knobs, above. **The defaults
  (``page``/``offset``/``limit``) are conventions, not requirements: every paging param name is
  yours to set**, so an API that pages with its own vocabulary needs no special support —
  ``paginate=offset offset_param=startIndex size_param=resultsPerPage`` and
  ``paginate=offset offset_param=startAt size_param=maxResults`` are the same feature. Sending a
  paging param an API does not recognize is not always harmless (some reject the request
  outright), so set these rather than letting the defaults ride.
* ``json=true`` — type the snapshot as ONE raw ``json`` column instead of inferring columns from
  the records. The escape hatch for genuinely polymorphic payloads (a field that is an object in
  some records and an array in others has no inferred type to find); query it with
  ``json_extract`` / ``->>``. Inference handles ordinary variation on its own — see
  ``_JSON_SNAPSHOT_OPTS`` in ``sources.py`` — so reach for this only after a scan actually fails.
* ``max_pages=<n>`` — hard cap on requests (default 20). ``max_rows=<n>`` — optional row cap.
* ``auth_env=<ENV_VAR>`` — send ``Authorization: Bearer $ENV_VAR``. The spec carries the env
  var *name*, never the token, so specs stay safe to log and echo.
* ``filter="<SQL predicate>"`` / ``select=<col,col>`` — OData only (``paginate=odata``): the
  SQL predicate is translated to ``$filter`` (comparisons, AND/OR/NOT, IN, edge-anchored
  LIKE/ILIKE, IS [NOT] NULL — anything else errors, telling you to hand-write that part) and
  the column list to ``$select``, so the agent writes SQL on both sides of the wire. Quote
  values containing spaces — options are tokenized shell-style.
* ``header=<Name>:<ENV_VAR>`` (repeatable) — send any extra header with its value from the
  environment: ``header=X-Api-Key:NASA_KEY``. Covers API-key headers and non-Bearer
  ``Authorization`` schemes (put the full value, e.g. ``token xxx``, in the env var).
* ``param=<name>:<ENV_VAR>`` (repeatable) — append a query param with its value from the
  environment: ``param=api_key:NASA_KEY``. This is how ``?api_key=...`` APIs are used
  WITHOUT putting the key in the spec (specs are logged and recorded as the source locator).
  Injected values are scrubbed from error messages (replaced by ``$ENV_VAR``) so a failing
  URL can't leak them either.

Safety rails: every page is retried on 429/5xx/network errors with exponential backoff
(honouring ``Retry-After``); a paginating fetch that receives the *same page twice* stops (an
API that ignores its page param would otherwise loop to ``max_pages``); the snapshot is written
to a temp file and moved into place, so a failed fetch never leaves a half-written snapshot.

Connections vs requests
-----------------------
The ``api:<url>`` grammar above describes *one endpoint* — fine for a single feed, absurd for
an API you want to explore, where every endpoint would be another permanent source. So the
same machinery is also driven from two halves:

* :class:`ApiConnection` — base URL, credentials, and shared conventions, declared ONCE (an
  ``openapi:`` source builds one; see ``sources.py``).
* :class:`ApiRequest` — a path under it, params, and per-call fetch options.

:func:`resolve_request` binds the two into the same :class:`ApiSpec` the ``api:`` grammar
produces, so pagination, retries, auth and record extraction have exactly one implementation.
Params are layered — the query string inside ``path``, then the connection's defaults, then the
call's own — while pagination-managed and credential param names are *reserved* (passing one is
an error, not a silent override). ``{placeholder}``s in the path's *segments* are filled from
params and percent-encoded, so a bound value can never redirect the request; one in the query
string is refused, since nothing would substitute it.

:func:`fetch_fanout` is the list→detail primitive: one URL per row of a prep query, fetched
through a small thread pool sharing one :class:`HostLimiter` so a 429 backs every worker off
together. Each row is stamped ``_key_<placeholder>`` for the join back; a per-entity 404 is
data (skipped and reported), while 401/403 or anything else aborts — a half-fetched detail
table is a footgun, because aggregates over it look valid.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import Any
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

_PAGINATE_STYLES = frozenset({"none", "page", "offset", "cursor", "keyset", "link", "odata"})
# Wrapper keys tried, in order, when the response is an object and no records= path was given.
# ("value" is the OData v4 convention.)
_COMMON_RECORD_KEYS = ("results", "data", "items", "records", "rows", "value")

_DEFAULT_MAX_PAGES = 20
_DEFAULT_OFFSET_PAGE_SIZE = 100
_TIMEOUT_SECONDS = 30.0
_ATTEMPTS_PER_PAGE = 3
_MAX_BACKOFF_SECONDS = 30.0
_USER_AGENT = "spelunk-api-source (github.com/shaunak2512/spelunk)"

_INT_OPTIONS = frozenset({"start", "page_size", "max_pages", "max_rows"})
_BOOL_OPTIONS = frozenset({"json"})
_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;[^,]*\brel="?next"?')


class ApiHttpError(ValueError):
    """A non-retryable HTTP error response, carrying its status code.

    A ``ValueError`` like every other fetch failure (callers that don't care about the code
    handle it uniformly), but the pagination loop needs the status: a 404 *mid-pagination* is
    how some APIs (e.g. TVMaze) say "past the last page" and must end the fetch, not fail it.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ApiSpec:
    """A parsed ``api:`` locator: the URL plus fetch options (see the module docstring)."""

    url: str
    records: str | None = None
    paginate: str = "none"
    page_param: str = "page"
    start: int = 1
    size_param: str | None = None
    page_size: int | None = None
    offset_param: str = "offset"
    cursor_param: str | None = None
    cursor_path: str | None = None
    keyset_field: str | None = None
    max_pages: int = _DEFAULT_MAX_PAGES
    max_rows: int | None = None
    auth_env: str | None = None
    # OData only (paginate=odata): a SQL predicate translated to $filter, and a column list
    # for $select — so the agent writes SQL on both sides of the wire. Quoted values are
    # supported: filter="Freight > 500 AND ShipCountry = 'Germany'".
    filter: str | None = None
    select: str | None = None
    # Type the snapshot as one raw JSON column instead of inferring columns from the records.
    # A *typing* option rather than a fetch option — it changes nothing about the request — but
    # it rides the same grammar because it is a property of the endpoint's payload shape.
    json: bool = False
    # (header name, ENV var) / (query param, ENV var) pairs from repeatable header=/param=
    # options — values are resolved from the environment at fetch time, never stored.
    extra_headers: list[tuple[str, str]] = field(default_factory=list)
    url_params: list[tuple[str, str]] = field(default_factory=list)


_OPTION_NAMES = frozenset(f.name for f in fields(ApiSpec)) - {"url", "extra_headers", "url_params"}
# header=/param= are special-cased (repeatable, <name>:<ENV> form) but still valid option keys.
_VALID_OPTION_HELP = ", ".join(sorted(_OPTION_NAMES | {"header", "param"}))


def parse_api_spec(text: str) -> ApiSpec:
    """Parse the locator body of an ``api:`` spec — ``<url> [key=value ...]``.

    Tokenized shell-style so an option value may be quoted to contain spaces
    (``filter="Freight > 500"``); a locator with an unbalanced quote character falls back to
    plain whitespace splitting.
    """
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        raise ValueError("api: source needs a URL, e.g. api:https://example.com/data")
    url = parts[0]
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"api: source URL must be http(s), got {url!r}. "
            "Write the spec as api:<url> [key=value ...]."
        )
    options: dict[str, Any] = {}
    extra_headers: list[tuple[str, str]] = []
    url_params: list[tuple[str, str]] = []
    for tok in parts[1:]:
        key, sep, value = tok.partition("=")
        if not sep or not key or not value:
            raise ValueError(
                f"Malformed api: option {tok!r} — options are key=value tokens after the URL."
            )
        if key in ("header", "param"):
            name, csep, env = value.partition(":")
            if not csep or not name or not env:
                example = "header=X-Api-Key:MY_KEY" if key == "header" else "param=api_key:MY_KEY"
                raise ValueError(
                    f"api: option {key}= takes <name>:<ENV_VAR> (value read from the environment "
                    f"at fetch time, never stored), e.g. {example}; got {value!r}."
                )
            (extra_headers if key == "header" else url_params).append((name, env))
            continue
        if key not in _OPTION_NAMES:
            raise ValueError(
                f"Unknown api: option {key!r}. Valid options: {_VALID_OPTION_HELP}."
            )
        options[key] = _coerce_option(key, value)
    spec = ApiSpec(url=url, extra_headers=extra_headers, url_params=url_params, **options)
    return _validated(spec)


def _coerce_option(key: str, value: str) -> Any:
    """One ``key=value`` option's string coerced to the type :class:`ApiSpec` declares.

    Shared by both grammars that build options from text — the ``api:`` locator and a
    connection's defaults — because a default that survives as the wrong *type* is worse than
    a rejected one: a string ``"false"`` is truthy, so a connection-wide ``json=false`` would
    have switched raw-JSON typing ON for every fetch that inherited it.
    """
    if key in _INT_OPTIONS:
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"api: option {key} must be an integer, got {value!r}.") from None
    if key in _BOOL_OPTIONS:
        if value.lower() not in _TRUTHY | _FALSY:
            raise ValueError(f"api: option {key} must be a boolean (true/false), got {value!r}.")
        return value.lower() in _TRUTHY
    return value


def _validated(spec: ApiSpec) -> ApiSpec:
    """Cross-check a built :class:`ApiSpec` and desugar ``paginate=odata``.

    Shared by ``parse_api_spec`` (the ``api:<url>`` grammar) and :func:`resolve_request` (a
    connection + request), so both paths reject the same nonsense with the same wording.
    """
    if spec.paginate not in _PAGINATE_STYLES:
        raise ValueError(
            f"Unknown paginate style {spec.paginate!r}. "
            f"Valid styles: {', '.join(sorted(_PAGINATE_STYLES))}."
        )
    if spec.paginate == "cursor" and not spec.cursor_path:
        raise ValueError(
            "paginate=cursor needs cursor_path=<dot.path> — where in each response the next "
            "cursor (or next-page URL) lives, e.g. cursor_path=next."
        )
    if spec.paginate == "keyset" and not (spec.keyset_field and spec.cursor_param):
        raise ValueError(
            "paginate=keyset needs keyset_field=<dot.path into the last record> AND "
            "cursor_param=<query-param-name>, e.g. keyset_field=id cursor_param=starting_after."
        )
    if (spec.filter or spec.select) and spec.paginate != "odata":
        raise ValueError(
            "filter=/select= are OData options and need paginate=odata (they translate to "
            "$filter/$select). For a non-OData API, put the service's own query params in "
            "the URL instead."
        )
    if spec.paginate == "odata":
        # Sugar for the OData v4 conventions: records under "value", the next page as a full
        # (or relative) URL at the literal key "@odata.nextLink". Explicit records=/cursor_path=
        # win, so a v2 service works with records=d.results cursor_path=d.__next.
        url = spec.url
        if spec.filter:
            url = _with_param(url, "$filter", sql_to_odata_filter(spec.filter))
        if spec.select:
            url = _with_param(url, "$select", _select_list(spec.select))
        spec = replace(
            spec,
            url=url,
            paginate="cursor",
            cursor_path=spec.cursor_path or "@odata.nextLink",
            records=spec.records or "value",
        )
    if spec.max_pages < 1:
        raise ValueError("max_pages must be >= 1.")
    return spec


def _select_list(select: str) -> str:
    """Validate a ``select=`` column list (comma-separated identifiers) for $select."""
    cols = [c.strip() for c in select.split(",") if c.strip()]
    bad = [c for c in cols if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c)]
    if not cols or bad:
        raise ValueError(
            f"select= takes a comma-separated column list, e.g. select=OrderID,Freight; "
            f"got {select!r}" + (f" (invalid: {', '.join(bad)})" if bad else "") + "."
        )
    return ",".join(cols)


# --------------------------------------------------------------------------- #
# Connections and requests
# --------------------------------------------------------------------------- #
# Query-param names that almost certainly carry a credential. A caller passing one as a plain
# value has put a secret somewhere it will be written to the tool-call log verbatim, so we
# refuse and point at param=<name>:<ENV>, which resolves from the environment at fetch time.
_CREDENTIAL_PARAM_NAMES = frozenset({
    "api_key", "apikey", "api-key", "key", "token", "access_token", "auth", "auth_token",
    "password", "passwd", "secret", "client_secret", "private_key", "signature",
})
# Separator used to join a list param when it is NOT exploded, by OpenAPI serialization style.
_STYLE_SEPARATORS = {"form": ",", "simple": ",", "spaceDelimited": " ", "pipeDelimited": "|"}
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ApiConnection:
    """One API registered once: base URL, credentials, and its endpoints' shared conventions.

    The counterpart to :class:`ApiRequest` — this half is declared on the source and reused by
    every fetch, so an agent chasing five endpoints supplies the credential zero further times.
    ``params`` are default query params merged into every request; ``defaults`` are default
    :class:`ApiSpec` options (``records``, ``paginate``, …) a request may override.
    """

    base_url: str
    auth_env: str | None = None
    extra_headers: list[tuple[str, str]] = field(default_factory=list)
    url_params: list[tuple[str, str]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc.lower()


@dataclass
class ApiRequest:
    """One endpoint call against an :class:`ApiConnection`: a path, params, and fetch options.

    ``path`` is relative to the connection's base URL and its *segments* may contain
    ``{placeholder}``s, each filled from ``params`` (percent-encoded as a single path segment)
    or, for a fan-out, bound per row. A placeholder in the query string is an error, not a
    substitution — query params are passed by name. ``param_styles`` maps a param name to its
    ``(style, explode)`` from the endpoint
    catalog, which is what decides whether a list serializes as ``a,b`` or ``k=a&k=b``.
    """

    path: str
    params: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    param_styles: dict[str, tuple[str, bool]] = field(default_factory=dict)
    # Per-endpoint fetch options read from the spec's catalog (this endpoint paginates by page,
    # its records live under "results"). They sit between the connection's blanket defaults and
    # the caller's explicit args — most specific wins — so a connection-wide `paginate=page`
    # cannot make a detail endpoint try to paginate a single object.
    hints: dict[str, Any] = field(default_factory=dict)


def placeholders_in(path: str) -> list[str]:
    """The ``{placeholder}`` names in a request path, in order of appearance."""
    return _PLACEHOLDER_RE.findall(path.split("?")[0])


def parse_connection(base_url: str, tokens: list[str]) -> ApiConnection:
    """Build an :class:`ApiConnection` from the option tokens of a connection source spec.

    Same option vocabulary as ``api:`` — ``auth_env=``, ``header=<Name>:<ENV>``,
    ``param=<name>:<ENV>``, and any fetch option (``records=``, ``paginate=``, …) which becomes
    a *default* every request inherits — plus ``default_param=<name>:<value>`` for a plain
    query param that every request should carry (``default_param=language:en-US``).

    ``base_url=<url>`` overrides *base_url*. A spec read off disk whose ``servers[0].url`` is
    relative (Petstore's ``/api/v3``) carries no host to resolve against, and this is how the
    caller supplies it.
    """
    conn = ApiConnection(base_url=base_url.rstrip("/"))
    for tok in tokens:
        key, sep, value = tok.partition("=")
        if not sep or not key or not value:
            raise ValueError(
                f"Malformed connection option {tok!r} — options are key=value tokens after "
                "the spec locator."
            )
        if key in ("header", "param", "default_param"):
            name, csep, rest = value.partition(":")
            if not csep or not name or not rest:
                raise ValueError(
                    f"Option {key}= takes <name>:<"
                    + ("value" if key == "default_param" else "ENV_VAR")
                    + f">, got {value!r}."
                )
            if key == "header":
                conn.extra_headers.append((name, rest))
            elif key == "param":
                conn.url_params.append((name, rest))
            else:
                conn.params[name] = rest
            continue
        if key == "auth_env":
            conn.auth_env = value
            continue
        if key == "base_url":
            conn.base_url = value.rstrip("/")
            continue
        if key not in _OPTION_NAMES:
            raise ValueError(
                f"Unknown connection option {key!r}. Valid options: {_VALID_OPTION_HELP}, "
                "default_param, base_url."
            )
        conn.defaults[key] = _coerce_option(key, value)
    # Fail at attach time on a nonsense default rather than on the first fetch that inherits it.
    _validated(ApiSpec(url=conn.base_url, **conn.defaults))
    return conn


def _scalar(value: Any) -> str:
    """One param value as the wire sees it — JSON's booleans, not Python's ``True``/``False``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _param_pairs(
    name: str, value: Any, styles: dict[str, tuple[str, bool]]
) -> list[tuple[str, str]]:
    """Serialize one param to ``(key, value)`` pairs — ``None`` drops it, a list obeys its style.

    A dropped ``None`` is deliberate: it lets a caller pass a param conditionally
    (``{"region": maybe_region}``) without building the dict by hand.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [_scalar(v) for v in value if v is not None]
        if not items:
            return []
        style, explode = styles.get(name, ("form", False))
        if explode:
            return [(name, item) for item in items]
        return [(name, _STYLE_SEPARATORS.get(style, ",").join(items))]
    return [(name, _scalar(value))]


def _with_params(url: str, pairs: list[tuple[str, str]]) -> str:
    """Append *pairs* to *url*'s query string, replacing any existing keys of the same name."""
    if not pairs:
        return url
    parts = urlsplit(url)
    replaced = {k for k, _ in pairs}
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in replaced
    ]
    query.extend(pairs)
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def _reserved_param_names(spec_options: dict[str, Any], conn: ApiConnection) -> dict[str, str]:
    """Param names the fetcher or the connection owns, mapped to the fix for using one."""
    paginate = spec_options.get("paginate", "none")
    reserved: dict[str, str] = {}
    if paginate == "page":
        reserved[spec_options.get("page_param") or "page"] = (
            "the pagination loop sets it — use start=<n> for the first page number"
        )
    if paginate == "offset":
        reserved[spec_options.get("offset_param") or "offset"] = "the pagination loop sets it"
    if paginate in ("cursor", "keyset") and spec_options.get("cursor_param"):
        reserved[spec_options["cursor_param"]] = "the pagination loop sets it"
    if paginate in ("page", "offset", "keyset"):
        size = spec_options.get("size_param")
        if size:
            reserved[size] = "the pagination loop sets it — use page_size=<n>"
    for param_name, env_name in conn.url_params:
        reserved[param_name] = (
            f"the connection already sends it from ${env_name}; credentials never travel as "
            "plain param values"
        )
    return reserved


def resolve_request(
    conn: ApiConnection, req: ApiRequest, *, allow_placeholders: bool = False
) -> tuple[ApiSpec, dict[str, str]]:
    """Bind a request to its connection: one validated :class:`ApiSpec` plus the values used.

    Params are layered lowest-to-highest — the query string embedded in ``path``, then the
    connection's default ``params``, then the request's own — with each layer replacing only
    its own keys. Pagination-managed and credential params are *reserved*: passing one is an
    error rather than a silent override, because a caller who sets ``page`` while the loop also
    sets it gets page 1 as many times as it asks for.

    Fetch *options* layer by specificity — the connection's blanket defaults, then the catalog's
    per-endpoint ``hints``, then the caller's explicit args. Per-endpoint knowledge beating a
    connection-wide default is what stops ``paginate=page`` on the connection from making every
    detail fetch re-request one object; an under-documented endpoint is corrected per call,
    which is where endpoint-specific knowledge belongs anyway.

    Returns ``(spec, path_values)`` where ``path_values`` are the ``{placeholder}``
    substitutions actually made, so a fan-out can stamp them onto the rows it fetched.
    """
    path = req.path.strip()
    lowered = path.lower()
    if lowered.startswith(("http://", "https://", "//")) or "://" in lowered.split("?")[0]:
        raise ValueError(
            f"fetch path must be relative to the source's base URL, got {req.path!r}. "
            "A source reaches only its own API; attach another source for another host."
        )
    if ".." in path.split("?")[0].split("/"):
        raise ValueError(f"fetch path may not contain '..' segments, got {req.path!r}.")

    path_part, _, inline_query = path.partition("?")
    # Placeholders are a PATH-segment feature: only path_part is templated below, so a
    # `{name}` in the query string would otherwise travel to the API percent-encoded as the
    # literal `%7Bname%7D` — and, if the caller also passed it, alongside a second param under
    # the placeholder's own name. Both requests look fine and neither is the one asked for, so
    # refuse rather than send it.
    stray = [
        (key, _PLACEHOLDER_RE.findall(value))
        for key, value in parse_qsl(inline_query, keep_blank_values=True)
        if _PLACEHOLDER_RE.search(value)
    ]
    if stray:
        # Name the QUERY KEY in the fix, not the placeholder: `?language={lang}` wants
        # params={"language": ...}, and suggesting {"lang": ...} would send the wrong param.
        raise ValueError(
            f"path {req.path!r} templates query param(s) {sorted(k for k, _ in stray)} with "
            f"{{placeholder}}(s) {sorted({p for _, ps in stray for p in ps})}, which are only "
            'substituted in path segments. Pass the value by param name instead: path="'
            + path_part + '", params={"' + stray[0][0] + '": ...}.'
        )
    merged: dict[str, Any] = {}
    for key, value in parse_qsl(inline_query, keep_blank_values=True):
        merged[key] = value
    merged.update(conn.params)
    merged.update(req.params)

    # {placeholder}s consume their param and become path segments, percent-encoded so a value
    # containing / ? or # cannot redirect the request to a different endpoint.
    path_values: dict[str, str] = {}
    missing: list[str] = []
    for placeholder in _PLACEHOLDER_RE.findall(path_part):
        if placeholder not in merged or merged[placeholder] is None:
            missing.append(placeholder)
            continue
        value = _scalar(merged.pop(placeholder))
        path_values[placeholder] = value
        path_part = path_part.replace("{" + placeholder + "}", quote(value, safe=""))
    if missing and not allow_placeholders:
        raise ValueError(
            f"path {req.path!r} has unfilled placeholder(s) {sorted(missing)}. Pass them in "
            "params (params={\"" + missing[0] + "\": ...}), or bind them to a result's columns "
            "with rows_from=<result> to fetch one URL per row."
        )

    options = dict(conn.defaults)
    options.update(req.hints)
    options.update({k: v for k, v in req.options.items() if v is not None})
    reserved = _reserved_param_names(options, conn)
    for key in merged:
        if key in reserved:
            raise ValueError(f"Param {key!r} is reserved: {reserved[key]}.")
        if key.lower() in _CREDENTIAL_PARAM_NAMES:
            raise ValueError(
                f"Param {key!r} looks like a credential. Params are written to the tool-call "
                f"log verbatim — put the value in an environment variable and add "
                f"`param={key}:<ENV_VAR>` to the source spec instead, so only the variable "
                "NAME is ever recorded."
            )

    url = conn.base_url.rstrip("/") + "/" + path_part.lstrip("/")
    pairs: list[tuple[str, str]] = []
    for key, value in merged.items():
        pairs.extend(_param_pairs(key, value, req.param_styles))
    url = _with_params(url, pairs)

    spec = ApiSpec(
        url=url,
        auth_env=conn.auth_env,
        extra_headers=list(conn.extra_headers),
        url_params=list(conn.url_params),
        **options,
    )
    return _validated(spec), path_values


def resolved_option(conn: ApiConnection, req: ApiRequest, key: str, default: Any = None) -> Any:
    """One fetch option resolved across the layers :func:`resolve_request` merges.

    Same precedence — the call's own options, then the catalog's per-endpoint hints, then the
    connection's defaults — for a caller that needs an option's effective value *without* an
    :class:`ApiSpec` in hand (a fan-out resolves one spec per row, not one per call).
    """
    for layer in (req.options, req.hints, conn.defaults):
        if layer.get(key) is not None:
            return layer[key]
    return default


class HostLimiter:
    """Shared pacing for every request of one fetch — so a 429 backs all of them off together.

    Scoped to a single fetch operation rather than the process: a fan-out over 240 URLs is
    exactly the case where independent per-request retries turn one rate limit into 240, while
    a process-wide limiter would let one unlucky call slow unrelated ones (and make tests
    order-dependent). ``min_interval`` paces steady-state requests; ``penalize`` is the
    ``Retry-After`` every worker then honours.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_at:
                    self._next_at = now + self.min_interval
                    return
                wait = self._next_at - now
            time.sleep(min(wait, _MAX_BACKOFF_SECONDS))

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)


def _prepare(spec: ApiSpec) -> tuple[ApiSpec, dict[str, str], list[tuple[str, str]]]:
    """Resolve a spec's credentials from the environment, just before the wire.

    Returns the spec with ``param=<name>:<ENV>`` values injected into its URL (so every
    pagination style inherits them), the request headers, and the ``(ENV name, value)`` pairs
    that must be scrubbed from any error message — a failing URL would otherwise echo a token
    into the tool-call log.
    """
    secrets: list[tuple[str, str]] = []

    def env_value(env_name: str) -> str:
        value = os.environ.get(env_name)
        if not value:
            raise ValueError(
                f"api: source wants auth from environment variable {env_name!r}, "
                "but it is not set (or empty) in the server's environment."
            )
        secrets.append((env_name, value))
        return value

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if spec.auth_env:
        headers["Authorization"] = f"Bearer {env_value(spec.auth_env)}"
    for header_name, env_name in spec.extra_headers:
        headers[header_name] = env_value(env_name)

    if spec.url_params:
        url = spec.url
        for param_name, env_name in spec.url_params:
            url = _with_param(url, param_name, env_value(env_name))
        spec = replace(spec, url=url)
    return spec, headers, secrets


def fetch_snapshot(
    spec: ApiSpec, dest_path: str, limiter: "HostLimiter | None" = None
) -> dict[str, Any]:
    """Fetch *spec* into an NDJSON file at *dest_path*; return the fetch fingerprint.

    The fingerprint (``url``, ``fetched_at``, ``pages``, ``row_count``, ``snapshot``, plus
    ``truncated`` when a cap stopped the fetch early) is what the caller records as source
    provenance. Raises ``ValueError`` with an actionable message on HTTP failure, non-JSON
    responses, a bad ``records`` path, or an API that yields zero records (almost always a
    wrong/missing ``records=`` path — the error lists the response's top-level keys).
    """
    original_url = spec.url
    spec, headers, secrets = _prepare(spec)

    rows = 0
    pages = 0
    truncated = False
    prev_page_sig: tuple[int, str] | None = None
    last_payload: Any = None
    next_url: str | None = _first_url(spec)

    tmp_path = dest_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            while next_url is not None:
                try:
                    payload, resp_headers = _get_json(next_url, headers, limiter)
                except ApiHttpError as exc:
                    if pages > 0 and exc.status == 404:
                        break  # past-the-end page: some APIs 404 instead of returning []
                    raise
                last_payload = payload
                records = _extract_records(payload, spec.records, next_url)
                pages += 1

                # An API that ignores its pagination params serves the same page forever;
                # detect the repeat and stop rather than writing max_pages duplicates.
                sig = _page_signature(records)
                if spec.paginate != "none" and records and sig == prev_page_sig:
                    pages -= 1  # the repeated page contributed nothing
                    break
                prev_page_sig = sig

                for record in records:
                    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    rows += 1
                    if spec.max_rows is not None and rows >= spec.max_rows:
                        truncated = True
                        break
                if truncated:
                    break
                last_record = records[-1] if records else None
                if pages >= spec.max_pages and spec.paginate != "none":
                    # Only report truncation if the API had more to give.
                    truncated = (
                        _next_url(spec, payload, resp_headers, pages, rows, len(records), last_record)
                        is not None
                    )
                    break
                next_url = _next_url(
                    spec, payload, resp_headers, pages, rows, len(records), last_record
                )
    except BaseException as exc:
        _remove_quietly(tmp_path)
        if secrets and isinstance(exc, ValueError):
            # A failing URL or echoed request can carry an injected secret — never let it
            # into an error message (they are logged and shown to the agent).
            raise ValueError(_scrub_secrets(str(exc), secrets)) from exc
        raise

    if rows == 0:
        _remove_quietly(tmp_path)
        keys = sorted(last_payload.keys()) if isinstance(last_payload, dict) else None
        hint = (
            f" The response is an object with keys {keys} — point records= at the right one."
            if keys
            else " The response contained an empty array."
        )
        raise ValueError(f"API {original_url} returned no records.{hint}")

    os.replace(tmp_path, dest_path)
    info: dict[str, Any] = {
        "url": original_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
        "row_count": rows,
        "snapshot": os.path.abspath(dest_path),
    }
    if truncated:
        info["truncated"] = True
    return info


DEFAULT_MAX_URLS = 500
DEFAULT_CONCURRENCY = 4


def fetch_fanout(
    conn: ApiConnection,
    req: ApiRequest,
    bindings: list[dict[str, Any]],
    dest_path: str,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, Any]:
    """Fetch one URL per binding into a single NDJSON snapshot — the list→detail fan-out.

    Each item of *bindings* supplies the ``{placeholder}`` values for one request; every record
    it returns is stamped with ``_key_<placeholder>`` columns so the result joins straight back
    to the prep query that produced the bindings, even when the response omits the id.

    Failure policy is deliberately asymmetric. A per-entity **404 is data** — the entity was
    deleted, so it is skipped and reported in ``info["skipped"]``, never silently dropped.
    401/403 aborts everything (the credential is wrong for every URL, so continuing just burns
    quota). Any other error aborts too: a half-fetched detail table is a footgun, because the
    aggregates an agent computes over it look perfectly valid.
    """
    if not bindings:
        raise ValueError("rows_from produced no rows — nothing to fetch.")
    # The same function the stamps come from (path segments only), so ``key_columns`` can never
    # advertise a ``_key_`` column that no row actually carries.
    order = sorted(set(placeholders_in(req.path)))

    specs: list[tuple[dict[str, str], ApiSpec]] = []
    for binding in bindings:
        bound = replace(req, params={**req.params, **binding})
        spec, path_values = resolve_request(conn, bound)
        if spec.paginate != "none":
            raise ValueError(
                "rows_from fetches one URL per row, so paginate must be 'none' — got "
                f"{spec.paginate!r}. Fetch the list endpoint separately, then fan out over it."
            )
        specs.append((path_values, spec))

    # Credentials resolve once — the headers are identical for every URL; only the injected
    # param=<name>:<ENV> query values have to be written into each spec's own URL.
    limiter = HostLimiter()
    first_spec, headers, secrets = _prepare(specs[0][1])
    prepared = [(specs[0][0], first_spec)]
    prepared += [(values, _prepare(spec)[0]) for values, spec in specs[1:]]

    results: list[list[dict] | None] = [None] * len(prepared)
    skipped: list[dict[str, str]] = []
    skipped_lock = threading.Lock()

    def run(index: int) -> None:
        values, spec = prepared[index]
        try:
            payload, _ = _get_json(spec.url, headers, limiter)
        except ApiHttpError as exc:
            if exc.status == 404:
                with skipped_lock:
                    skipped.append(values)
                results[index] = []
                return
            raise
        records = _extract_records(payload, spec.records, spec.url)
        stamps = {f"_key_{k}": v for k, v in values.items()}
        results[index] = [{**record, **stamps} for record in records]

    try:
        workers = max(1, min(concurrency, len(prepared)))
        if workers == 1:
            for i in range(len(prepared)):
                run(i)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for future in [pool.submit(run, i) for i in range(len(prepared))]:
                    future.result()  # re-raises the first worker failure
    except BaseException as exc:
        if secrets and isinstance(exc, ValueError):
            raise ValueError(_scrub_secrets(str(exc), secrets)) from exc
        raise

    rows = 0
    tmp_path = dest_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for chunk in results:
                for record in chunk or []:
                    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    rows += 1
    except BaseException:
        _remove_quietly(tmp_path)
        raise
    if rows == 0:
        _remove_quietly(tmp_path)
        raise ValueError(
            f"Fan-out over {len(prepared)} URL(s) returned no records"
            + (f" ({len(skipped)} were 404s)." if skipped else ".")
        )
    os.replace(tmp_path, dest_path)

    info: dict[str, Any] = {
        "url": conn.base_url.rstrip("/") + "/" + req.path.lstrip("/"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "urls": len(prepared),
        "row_count": rows,
        "key_columns": [f"_key_{k}" for k in order],
        "snapshot": os.path.abspath(dest_path),
    }
    if skipped:
        # Never a silent cap: the agent must be able to see which entities are missing before
        # it aggregates over the result.
        info["skipped"] = skipped
        info["skipped_count"] = len(skipped)
    return info


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _origin(url: str) -> tuple[str, str]:
    """The (scheme, host:port) a URL addresses, lowercased for comparison."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _same_origin(base: str, candidate: str, what: str) -> str:
    """Return *candidate*, or refuse it when it leaves *base*'s origin.

    The request headers — ``Authorization: Bearer …`` and any ``header=<Name>:<ENV>`` value —
    are resolved once and reused for every page of a fetch. A next-page URL taken from a
    response body (``cursor_path``) or a ``Link:`` header is *server-supplied data*, so
    following one off-origin would hand this source's credentials to whatever host that data
    names. An API paginates within itself; anything else is a different source.
    """
    if _origin(base) != _origin(candidate):
        raise ValueError(
            f"{what} points off this API's own origin "
            f"({urlsplit(base).netloc} -> {urlsplit(candidate).netloc or candidate!r}). "
            "Refusing to follow it: the request carries this source's credentials. Attach "
            "the other host as its own source if you meant to read it."
        )
    return candidate


class _SameOriginRedirectHandler(_urlrequest.HTTPRedirectHandler):
    """Refuse a cross-origin HTTP redirect.

    urllib follows 3xx by default and re-sends the original request's headers, so a redirect
    to another host leaks the credential header just as a cross-host next-page URL would.
    Same rule as :func:`_same_origin`, one layer down.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _same_origin(req.full_url, newurl, f"HTTP {code} redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = _urlrequest.build_opener(_SameOriginRedirectHandler)


def _get_json(
    url: str, headers: dict[str, str], limiter: "HostLimiter | None" = None
) -> tuple[Any, dict[str, str]]:
    """GET *url* and parse the JSON body; retries 429/5xx/network errors with backoff.

    When a *limiter* is given, every attempt passes through it and a 429/5xx backoff is applied
    to the whole fetch rather than to this request alone — so concurrent workers hitting the
    same rate limit wait once, together, instead of each discovering it in turn.
    """
    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS_PER_PAGE):
        try:
            if limiter is not None:
                limiter.acquire()
            req = _urlrequest.Request(url, headers=headers)
            with _OPENER.open(req, timeout=_TIMEOUT_SECONDS) as resp:
                body = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            try:
                return json.loads(body), resp_headers
            except json.JSONDecodeError as exc:
                snippet = body[:200].decode("utf-8", errors="replace")
                raise ValueError(
                    f"API {url} did not return JSON (parse error: {exc}). "
                    f"Response starts: {snippet!r}"
                ) from exc
        except _urlerror.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code < 600:
                last_error = exc
                if attempt < _ATTEMPTS_PER_PAGE - 1:
                    delay = _backoff_delay(attempt, exc.headers.get("Retry-After"))
                    if limiter is not None:
                        limiter.penalize(delay)  # every worker waits, not just this one
                    time.sleep(delay)
                    continue
            else:
                detail = exc.read()[:200].decode("utf-8", errors="replace")
                raise ApiHttpError(
                    f"API request failed: HTTP {exc.code} for {url}. {detail}".strip(),
                    exc.code,
                ) from exc
        except _urlerror.URLError as exc:
            last_error = exc
            if attempt < _ATTEMPTS_PER_PAGE - 1:
                time.sleep(_backoff_delay(attempt, None))
                continue
    raise ValueError(
        f"API request failed after {_ATTEMPTS_PER_PAGE} attempts: {url} ({last_error})"
    ) from last_error


def _backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Exponential backoff (1s, 2s, 4s, ...) raised to a numeric ``Retry-After``, capped."""
    delay = float(2**attempt)
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass  # HTTP-date form — the exponential delay is a fine approximation
    return min(delay, _MAX_BACKOFF_SECONDS)


# --------------------------------------------------------------------------- #
# SQL -> OData $filter translation
# --------------------------------------------------------------------------- #
def sql_to_odata_filter(predicate: str) -> str:
    """Translate a SQL predicate into an OData v4 ``$filter`` expression.

    Supports the operator set both languages share: comparisons (=, !=, >, >=, <, <=),
    AND/OR/NOT, parentheses, IN (expanded to an eq-or-chain for pre-4.01 servers),
    LIKE/ILIKE with an edge-anchored pattern (-> contains/startswith/endswith, ILIKE via
    tolower), IS [NOT] NULL, booleans, and negative numbers. Anything outside that set —
    functions, arithmetic, subqueries, mid-string wildcards — raises a ``ValueError`` telling
    the caller to hand-write that part of the ``$filter`` in the URL instead: a filter must
    translate fully or fail loudly, never silently fetch everything.
    """
    import sqlglot
    from sqlglot.errors import SqlglotError

    try:
        tree = sqlglot.parse_one(predicate, read="duckdb")
    except SqlglotError as exc:
        raise ValueError(f"filter= is not a parseable SQL predicate: {exc}") from exc
    return _odata_expr(tree)


def _odata_expr(node: Any) -> str:
    from sqlglot import exp

    comparisons = {
        exp.EQ: "eq", exp.NEQ: "ne", exp.GT: "gt", exp.GTE: "ge", exp.LT: "lt", exp.LTE: "le",
    }

    def logical_operand(child: Any) -> str:
        text = _odata_expr(child)
        # Parenthesize nested logical operators so SQL grouping survives OData precedence.
        return f"({text})" if isinstance(child, (exp.And, exp.Or)) else text

    for cls, op in comparisons.items():
        if isinstance(node, cls):
            return f"{_odata_expr(node.this)} {op} {_odata_expr(node.expression)}"
    if isinstance(node, (exp.And, exp.Or)):
        op = "and" if isinstance(node, exp.And) else "or"
        return f"{logical_operand(node.this)} {op} {logical_operand(node.expression)}"
    if isinstance(node, exp.Not):
        inner = node.this
        while isinstance(inner, exp.Paren):  # not-with-parens would otherwise double up
            inner = inner.this
        return f"not ({_odata_expr(inner)})"
    if isinstance(node, exp.Paren):
        return f"({_odata_expr(node.this)})"
    if isinstance(node, exp.Is):
        # sqlglot parses IS NOT NULL as Not(Is(...)), handled by the Not branch above.
        return f"{_odata_expr(node.this)} eq {_odata_expr(node.expression)}"
    if isinstance(node, exp.In):
        col = _odata_expr(node.this)
        values = node.expressions
        if not values:
            raise ValueError("filter=: IN needs a literal list, e.g. col IN ('a', 'b').")
        return "(" + " or ".join(f"{col} eq {_odata_expr(v)}" for v in values) + ")"
    if isinstance(node, (exp.Like, exp.ILike)):
        return _odata_like(node)
    if isinstance(node, exp.Column):
        if node.table or node.db:
            raise ValueError(
                f"filter=: qualified column {node.sql()!r} is not translatable — use the bare "
                "OData property name."
            )
        return node.name
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "'" + node.this.replace("'", "''") + "'"
        return node.this
    if isinstance(node, exp.Boolean):
        return "true" if node.this else "false"
    if isinstance(node, exp.Null):
        return "null"
    if isinstance(node, exp.Neg):
        return f"-{_odata_expr(node.this)}"
    raise ValueError(
        f"filter=: cannot translate {node.sql()!r} to OData. Supported: comparisons, "
        "AND/OR/NOT, IN, LIKE with an edge-anchored pattern, IS [NOT] NULL. Hand-write the "
        "$filter in the URL for anything else."
    )


def _odata_like(node: Any) -> str:
    """LIKE/ILIKE with an edge-anchored pattern -> contains/startswith/endswith."""
    from sqlglot import exp

    pattern = node.expression
    if not isinstance(pattern, exp.Literal) or not pattern.is_string:
        raise ValueError("filter=: LIKE needs a literal string pattern.")
    raw = pattern.this
    if "_" in raw:
        raise ValueError("filter=: LIKE '_' wildcards have no OData equivalent.")
    inner = raw.strip("%")
    if "%" in inner:
        raise ValueError(
            f"filter=: LIKE pattern {raw!r} has a mid-string wildcard — OData only has "
            "contains/startswith/endswith."
        )
    fn = {
        (True, True): "contains", (False, True): "startswith", (True, False): "endswith",
    }.get((raw.startswith("%"), raw.endswith("%")), "eq")
    col = _odata_expr(node.this)
    lit = "'" + inner.replace("'", "''") + "'"
    if isinstance(node, exp.ILike):
        col, lit = f"tolower({col})", lit.lower()
    if fn == "eq":
        return f"{col} eq {lit}"
    return f"{fn}({col}, {lit})"


# --------------------------------------------------------------------------- #
# Record extraction
# --------------------------------------------------------------------------- #
def _dig(obj: Any, path: str) -> Any:
    """Follow a dot path into nested dicts; ``None`` when any segment is absent.

    A LITERAL key match wins over dot-splitting at every level, so a dotted key like
    OData's ``@odata.nextLink`` is reachable — ``cursor_path=@odata.nextLink`` finds the
    literal key first and only then falls back to ``["@odata"]["nextLink"]`` nesting.
    """
    if not isinstance(obj, dict) or not path:
        return None
    if path in obj:
        return obj[path]
    head, sep, rest = path.partition(".")
    if sep and head in obj:
        return _dig(obj[head], rest)
    return None


def _extract_records(payload: Any, records_path: str | None, url: str) -> list[dict]:
    """The list of record dicts in one response page (see module docstring for defaulting)."""
    if records_path is not None:
        found = _dig(payload, records_path)
        if found is None:
            keys = sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
            raise ValueError(
                f"records path {records_path!r} not found in the response from {url}. "
                f"Top level: {keys}."
            )
    elif isinstance(payload, list):
        found = payload
    elif isinstance(payload, dict):
        found = next(
            (payload[k] for k in _COMMON_RECORD_KEYS if isinstance(payload.get(k), list)),
            None,
        )
        if found is None:
            list_keys = [k for k, v in payload.items() if isinstance(v, list)]
            found = payload[list_keys[0]] if len(list_keys) == 1 else payload
    else:
        found = payload

    if isinstance(found, dict):
        return [found]  # a single-object response is one record
    if not isinstance(found, list):
        return [{"value": found}]
    return [r if isinstance(r, dict) else {"value": r} for r in found]


def _page_signature(records: list[dict]) -> tuple[int, str]:
    """A cheap identity for a page: its size + first record, for repeat-page detection."""
    head = json.dumps(records[0], sort_keys=True, default=str) if records else ""
    return (len(records), head)


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #
def _with_param(url: str, key: str, value: Any) -> str:
    """Return *url* with query param *key* set to *value* (replacing any existing one).

    Encodes with %20 for spaces (``quote``, not ``quote_plus``) — OData services must see
    ``$filter=Freight%20gt%20500``, and every server accepts %20 where + is ambiguous.
    """
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query.append((key, str(value)))
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


def _first_url(spec: ApiSpec) -> str:
    url = spec.url
    if spec.paginate in ("page", "keyset"):
        if spec.paginate == "page":
            url = _with_param(url, spec.page_param, spec.start)
        if spec.size_param:
            url = _with_param(url, spec.size_param, spec.page_size or _DEFAULT_OFFSET_PAGE_SIZE)
    elif spec.paginate == "offset":
        size = spec.page_size or _DEFAULT_OFFSET_PAGE_SIZE
        url = _with_param(url, spec.offset_param, 0)
        url = _with_param(url, spec.size_param or "limit", size)
    return url


def _next_url(
    spec: ApiSpec,
    payload: Any,
    resp_headers: dict[str, str],
    pages_done: int,
    rows_done: int,
    last_page_len: int,
    last_record: dict | None,
) -> str | None:
    """The URL of the next page, or ``None`` when this style says the fetch is complete."""
    if spec.paginate == "none":
        return None
    if last_page_len == 0:
        return None  # an empty page ends every style
    if spec.paginate == "page":
        # _first_url set page_param=start (and any size_param); bump to the next page number.
        return _with_param(_first_url(spec), spec.page_param, spec.start + pages_done)
    if spec.paginate == "offset":
        size = spec.page_size or _DEFAULT_OFFSET_PAGE_SIZE
        if last_page_len < size:
            return None  # a short page means the API ran out
        url = _with_param(spec.url, spec.offset_param, rows_done)
        return _with_param(url, spec.size_param or "limit", size)
    if spec.paginate == "cursor":
        cursor = _dig(payload, spec.cursor_path or "")
        if cursor in (None, "", False):
            return None
        if isinstance(cursor, str) and cursor.lower().startswith(("http://", "https://")):
            # PokeAPI-style: the cursor IS the next-page URL — but it came out of the response
            # body, so it only gets followed if it stays on this API.
            return _same_origin(spec.url, cursor, f"cursor_path {spec.cursor_path!r}")
        if spec.cursor_param:
            return _with_param(spec.url, spec.cursor_param, cursor)
        if isinstance(cursor, str) and ("/" in cursor or "?" in cursor):
            # A path-like cursor with no cursor_param is a RELATIVE next-page URL (OData
            # permits relative @odata.nextLink values); resolve it against the request URL.
            # urljoin still honours a scheme-relative `//other.host/x`, so check the result.
            return _same_origin(
                spec.url, urljoin(spec.url, cursor), f"cursor_path {spec.cursor_path!r}"
            )
        raise ValueError(
            f"cursor_path {spec.cursor_path!r} yielded {cursor!r}, which is not a URL — "
            "set cursor_param=<query-param-name> so the cursor can be sent back."
        )
    if spec.paginate == "keyset":
        if spec.size_param and last_page_len < (spec.page_size or _DEFAULT_OFFSET_PAGE_SIZE):
            return None  # a short page means the API ran out
        key = _dig(last_record, spec.keyset_field or "")
        if key in (None, "", False):
            return None  # last record has no key to seek from
        return _with_param(_first_url(spec), spec.cursor_param or "", key)
    if spec.paginate == "link":
        match = _LINK_NEXT_RE.search(resp_headers.get("link", ""))
        if not match:
            return None
        # RFC 8288 permits a relative URI-reference in a Link header; resolve, then confine.
        return _same_origin(
            spec.url, urljoin(spec.url, match.group(1)), 'Link header rel="next"'
        )
    return None


def _scrub_secrets(text: str, secrets: list[tuple[str, str]]) -> str:
    """Replace every secret value in *text* with a ``$ENV_NAME`` placeholder."""
    for env_name, value in secrets:
        text = text.replace(value, f"${env_name}")
    return text


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
