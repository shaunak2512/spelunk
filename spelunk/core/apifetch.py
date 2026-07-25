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
* ``page_param`` / ``start`` / ``size_param`` / ``page_size`` / ``offset_param`` /
  ``cursor_param`` / ``cursor_path`` / ``keyset_field`` — style knobs, above.
* ``max_pages=<n>`` — hard cap on requests (default 20). ``max_rows=<n>`` — optional row cap.
* ``auth_env=<ENV_VAR>`` — send ``Authorization: Bearer $ENV_VAR``. The spec carries the env
  var *name*, never the token, so specs stay safe to log and echo.

Safety rails: every page is retried on 429/5xx/network errors with exponential backoff
(honouring ``Retry-After``); a paginating fetch that receives the *same page twice* stops (an
API that ignores its page param would otherwise loop to ``max_pages``); the snapshot is written
to a temp file and moved into place, so a failed fetch never leaves a half-written snapshot.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PAGINATE_STYLES = frozenset({"none", "page", "offset", "cursor", "keyset", "link"})
# Wrapper keys tried, in order, when the response is an object and no records= path was given.
_COMMON_RECORD_KEYS = ("results", "data", "items", "records", "rows")

_DEFAULT_MAX_PAGES = 20
_DEFAULT_OFFSET_PAGE_SIZE = 100
_TIMEOUT_SECONDS = 30.0
_ATTEMPTS_PER_PAGE = 3
_MAX_BACKOFF_SECONDS = 30.0
_USER_AGENT = "spelunk-api-source (github.com/shaunak2512/spelunk)"

_INT_OPTIONS = frozenset({"start", "page_size", "max_pages", "max_rows"})

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


_OPTION_NAMES = frozenset(f.name for f in fields(ApiSpec)) - {"url"}


def parse_api_spec(text: str) -> ApiSpec:
    """Parse the locator body of an ``api:`` spec — ``<url> [key=value ...]``."""
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
    for tok in parts[1:]:
        key, sep, value = tok.partition("=")
        if not sep or not key or not value:
            raise ValueError(
                f"Malformed api: option {tok!r} — options are key=value tokens after the URL."
            )
        if key not in _OPTION_NAMES:
            raise ValueError(
                f"Unknown api: option {key!r}. Valid options: {', '.join(sorted(_OPTION_NAMES))}."
            )
        if key in _INT_OPTIONS:
            try:
                options[key] = int(value)
            except ValueError:
                raise ValueError(f"api: option {key} must be an integer, got {value!r}.") from None
        else:
            options[key] = value
    spec = ApiSpec(url=url, **options)
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
    if spec.max_pages < 1:
        raise ValueError("max_pages must be >= 1.")
    return spec


def fetch_snapshot(spec: ApiSpec, dest_path: str) -> dict[str, Any]:
    """Fetch *spec* into an NDJSON file at *dest_path*; return the fetch fingerprint.

    The fingerprint (``url``, ``fetched_at``, ``pages``, ``row_count``, ``snapshot``, plus
    ``truncated`` when a cap stopped the fetch early) is what the caller records as source
    provenance. Raises ``ValueError`` with an actionable message on HTTP failure, non-JSON
    responses, a bad ``records`` path, or an API that yields zero records (almost always a
    wrong/missing ``records=`` path — the error lists the response's top-level keys).
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if spec.auth_env:
        token = os.environ.get(spec.auth_env)
        if not token:
            raise ValueError(
                f"api: source wants auth from environment variable {spec.auth_env!r}, "
                "but it is not set (or empty) in the server's environment."
            )
        headers["Authorization"] = f"Bearer {token}"

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
                    payload, resp_headers = _get_json(next_url, headers)
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
    except BaseException:
        _remove_quietly(tmp_path)
        raise

    if rows == 0:
        _remove_quietly(tmp_path)
        keys = sorted(last_payload.keys()) if isinstance(last_payload, dict) else None
        hint = (
            f" The response is an object with keys {keys} — point records= at the right one."
            if keys
            else " The response contained an empty array."
        )
        raise ValueError(f"API {spec.url} returned no records.{hint}")

    os.replace(tmp_path, dest_path)
    info: dict[str, Any] = {
        "url": spec.url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
        "row_count": rows,
        "snapshot": os.path.abspath(dest_path),
    }
    if truncated:
        info["truncated"] = True
    return info


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _get_json(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    """GET *url* and parse the JSON body; retries 429/5xx/network errors with backoff."""
    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS_PER_PAGE):
        try:
            req = _urlrequest.Request(url, headers=headers)
            with _urlrequest.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
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
                    time.sleep(_backoff_delay(attempt, exc.headers.get("Retry-After")))
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
# Record extraction
# --------------------------------------------------------------------------- #
def _dig(obj: Any, path: str) -> Any:
    """Follow a dot path into nested dicts; ``None`` when any segment is absent."""
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


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
    """Return *url* with query param *key* set to *value* (replacing any existing one)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query.append((key, str(value)))
    return urlunsplit(parts._replace(query=urlencode(query)))


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
            return cursor  # PokeAPI-style: the cursor IS the next-page URL
        if not spec.cursor_param:
            raise ValueError(
                f"cursor_path {spec.cursor_path!r} yielded {cursor!r}, which is not a URL — "
                "set cursor_param=<query-param-name> so the cursor can be sent back."
            )
        return _with_param(spec.url, spec.cursor_param, cursor)
    if spec.paginate == "keyset":
        if spec.size_param and last_page_len < (spec.page_size or _DEFAULT_OFFSET_PAGE_SIZE):
            return None  # a short page means the API ran out
        key = _dig(last_record, spec.keyset_field or "")
        if key in (None, "", False):
            return None  # last record has no key to seek from
        return _with_param(_first_url(spec), spec.cursor_param or "", key)
    if spec.paginate == "link":
        match = _LINK_NEXT_RE.search(resp_headers.get("link", ""))
        return match.group(1) if match else None
    return None


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
