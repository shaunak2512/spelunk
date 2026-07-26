"""Materialize an OpenAPI spec into a queryable endpoint catalog — the ``openapi:`` source kind.

``openapi:<url-or-path>`` loads an OpenAPI 3.x JSON spec and snapshots **one row per
(path, method)** into the workspace, registered as a view — guidance-as-data, in the same
engine as everything else. Each row carries what an agent needs to call the endpoint through
an ``api:`` source:

* ``params`` — path- and operation-level parameters, ``$ref``-resolved, each with its
  ``location``/``required``/``type`` plus the *effective* ``style``/``explode`` (OpenAPI's
  defaults resolved: ``form``/explode for query, ``simple``/no-explode for path and header),
  which is what decides whether a list param serializes as ``a,b`` or ``k=a&k=b``.
* ``auth`` — the auth shape, mapped from ``securitySchemes`` onto the ``auth_env=`` /
  ``header=`` / ``param=`` options.
* ``pagination_hint`` — heuristic over query param names. When no convention is recognized but
  the endpoint does declare paging-shaped params, the hint names them
  (``unknown; endpoint declares startIndex, resultsPerPage — set the matching
  paginate=/offset_param=/size_param= yourself``) instead of reporting nothing: the param names
  are the one thing the document always knows and the naming conventions are endless.
* ``records_hint`` — dot path to the records array in the 200-response schema (one nested
  level, ``$ref``-resolved). NULL for a detail endpoint whose response *is* the record.
* ``response_fields`` — **what the endpoint returns**: the record's declared fields as
  ``{name, type}`` structs, flattened two levels deep (``genres[].name`` for an array of
  objects, ``a.b`` for a nested object). This is what lets an agent pick an endpoint by the
  data it carries rather than by the shape of its URL, and write the SELECT before spending
  a request.
* ``suggested_spec`` — for GET endpoints, a ready-to-paste ``api:`` spec string with
  ``<SET_ME>`` where the credential env var name goes.

``method`` is stored **lowercase** (``'get'``), matching the OpenAPI document's own keys::

    SELECT path, summary, suggested_spec FROM tmdb_api
    WHERE method = 'get' AND path ILIKE '%movie%'

    -- which endpoints return revenue?
    SELECT path FROM tmdb_api
    WHERE 'revenue' IN (SELECT f.name FROM UNNEST(response_fields) AS t(f))

Hints are *hints*: pagination and records-path conventions are not formally declared in
OpenAPI, and ``response_fields`` reports what the spec *claims* — real specs are routinely
incomplete or stale. The materialized table's own schema, after fetching, is the authority.
JSON only — a YAML spec must be converted first (e.g. ``yq -o=json``); Swagger 2.0 specs are
rejected with a pointer to conversion tooling.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from .apifetch import _get_json

# Response-wrapper property names tried first (in order) when hunting the records array.
_COMMON_RECORD_KEYS = ("results", "data", "items", "records", "rows")
# HTTP methods catalogued. api: sources are GET-only, so only GET rows get a suggested_spec,
# but the other methods are still listed — knowing an endpoint exists is guidance too.
_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
_MAX_REF_DEPTH = 20
# How many levels of the record schema response_fields flattens. Level 1 is the record's own
# properties; level 2 is one step into a nested object or an array of objects. The cap is also
# what makes the walk cycle-safe for a self-referential schema (a $ref cycle bottoms out in
# _resolve, but a structural one — Thing.parent: Thing — only terminates on depth).
_MAX_FIELD_DEPTH = 2
# Most properties an object may have and still read as a list *envelope* rather than a record.
# Real envelopes are tiny: {page, results, total_pages, total_results}, {count, next, previous,
# results}, {object, data, has_more, url}.
_ENVELOPE_MAX_PROPS = 5


def load_spec(locator: str) -> dict:
    """Load an OpenAPI 3.x spec from a local path or http(s) URL. JSON only."""
    if locator.lower().startswith(("http://", "https://")):
        payload, _headers = _get_json(locator, {"Accept": "application/json"})
    else:
        path = os.path.abspath(locator)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except OSError as exc:
            raise ValueError(f"Cannot read OpenAPI spec {path!r}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"OpenAPI spec {path!r} is not valid JSON ({exc}). A YAML spec must be "
                "converted to JSON first, e.g. `yq -o=json eval spec.yaml`."
            ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"OpenAPI spec {locator!r} is not a JSON object.")
    if "swagger" in payload:
        raise ValueError(
            f"{locator!r} is a Swagger 2.0 spec; only OpenAPI 3.x is supported. Convert it "
            "first (e.g. `npx swagger2openapi spec.json`)."
        )
    if "openapi" not in payload or not isinstance(payload.get("paths"), dict):
        raise ValueError(
            f"{locator!r} does not look like an OpenAPI 3.x spec (no 'openapi' version or "
            "'paths' object)."
        )
    return payload


def endpoint_rows(spec: dict, locator: str) -> list[dict[str, Any]]:
    """One catalog row per (path, method) in *spec* — see the module docstring for columns."""
    base_url = _base_url(spec, locator)
    rows: list[dict[str, Any]] = []
    for path, path_item in spec["paths"].items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", [])
        for method in _METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            params = _params_of(spec, shared_params, op.get("parameters", []))
            auth_kind, auth_frag = _auth_of(spec, op)
            pagination_hint, paginate_frag = _pagination_of(params)
            records_hint, record_schema = _locate_records(spec, op)
            rows.append({
                "path": path,
                "method": method,  # lowercase, as the OpenAPI document itself keys them
                "operation_id": op.get("operationId"),
                "summary": op.get("summary"),
                "tags": op.get("tags") or [],
                "deprecated": bool(op.get("deprecated")),
                "params": params,
                "auth": auth_kind,
                "pagination_hint": pagination_hint,
                "records_hint": records_hint,
                "response_fields": _flatten_fields(spec, record_schema),
                "suggested_spec": (
                    _suggested_spec(base_url, path, records_hint, paginate_frag, auth_frag)
                    if method == "get"
                    else None
                ),
            })
    return rows


def catalog_info(locator: str, rows: list[dict]) -> dict[str, Any]:
    """The fingerprint recorded on the Source for an ``openapi:`` catalog."""
    return {
        "spec": locator,
        "endpoints": len(rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# Spec walking
# --------------------------------------------------------------------------- #
def _resolve(spec: dict, node: Any, depth: int = 0) -> Any:
    """Follow a local ``$ref`` chain (``#/components/...``); cycle/depth-guarded."""
    while isinstance(node, dict) and "$ref" in node and depth < _MAX_REF_DEPTH:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return node  # external/unknown ref — leave unresolved
        target: Any = spec
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                return node
            target = target[part]
        node = target
        depth += 1
    return node


def _type_of(schema: Any) -> str | None:
    """A schema's type; OpenAPI 3.1 allows a list (e.g. ["array", "null"]) — first non-null."""
    if not isinstance(schema, dict):
        return None
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return t


def _params_of(spec: dict, shared: list, own: list) -> list[dict[str, Any]]:
    """Path-level + operation-level parameters, ``$ref``-resolved, as compact structs.

    ``style``/``explode`` are recorded *effective* — OpenAPI's per-location defaults resolved
    here rather than left NULL — because they are what a caller needs to serialize a list
    param, and a NULL would just mean "look the default up again".
    """
    out: list[dict[str, Any]] = []
    for raw in list(shared) + list(own):
        p = _resolve(spec, raw)
        if not isinstance(p, dict) or not p.get("name"):
            continue
        location = p.get("in")
        # Defaults per OpenAPI 3: query/cookie are form+explode, path/header are simple.
        style = p.get("style") or ("form" if location in ("query", "cookie") else "simple")
        explode = p.get("explode")
        out.append({
            "name": p["name"],
            "location": location,
            "required": bool(p.get("required")),
            "type": _type_of(_resolve(spec, p.get("schema"))),
            "style": style,
            "explode": (style == "form") if explode is None else bool(explode),
        })
    return out


def _auth_of(spec: dict, op: dict) -> tuple[str, str | None]:
    """The endpoint's auth shape and the matching api:-spec fragment (env var as <SET_ME>).

    Operation-level ``security`` overrides the global one; an explicit empty list means the
    endpoint is public. Only the first scheme of the first alternative is mapped — multi-scheme
    requirements are rare and the catalog is guidance, not enforcement.
    """
    security = op.get("security", spec.get("security"))
    if not security:  # None (nothing declared anywhere) or [] (explicitly public)
        return "none", None
    first = next((alt for alt in security if isinstance(alt, dict) and alt), None)
    if first is None:
        return "none", None
    scheme_name = next(iter(first))
    scheme = (spec.get("components", {}).get("securitySchemes", {})).get(scheme_name)
    scheme = _resolve(spec, scheme)
    if not isinstance(scheme, dict):
        return f"unknown:{scheme_name}", None

    stype = scheme.get("type")
    if stype == "http" and scheme.get("scheme") == "bearer":
        return "bearer", "auth_env=<SET_ME>"
    if stype == "http" and scheme.get("scheme") == "basic":
        return "basic", "header=Authorization:<SET_ME>"  # env holds the full "Basic ..." value
    if stype == "apiKey":
        name, where = scheme.get("name", ""), scheme.get("in")
        if where == "header":
            # An apiKey header named Authorization (TMDB-style, often with
            # x-bearer-format: bearer) is a bearer token in disguise.
            if name.lower() == "authorization":
                return "bearer", "auth_env=<SET_ME>"
            return f"header:{name}", f"header={name}:<SET_ME>"
        if where == "query":
            return f"query:{name}", f"param={name}:<SET_ME>"
        return f"apiKey:{where}:{name}", None  # e.g. cookie — not supported by api:
    if stype in ("oauth2", "openIdConnect"):
        return "oauth2", "auth_env=<SET_ME>"  # a minted access token is a bearer token
    return f"unknown:{stype}", None


# Query-param names that reveal the pagination style. Order matters: an API with both
# page and limit params is page-style.
_CURSORISH = ("cursor", "starting_after", "since_id", "after", "page_token", "next_token")
# Substrings that make a query param *look* like it participates in paging. Deliberately broad
# and used only to name candidates in a hint — never to configure a fetch — so a false positive
# costs the agent a glance and a false negative costs it a silent page-1-only fetch.
_PAGINGISH = (
    "page", "offset", "skip", "start", "limit", "size", "count",
    "per", "max", "result", "row", "top", "cursor", "token", "after", "from",
)


def _pagination_of(params: list[dict]) -> tuple[str | None, str | None]:
    """(pagination_hint, api:-spec fragment) from query-param names; fragment only when safe."""
    qnames = {p["name"].lower(): p["name"] for p in params if p.get("location") == "query"}
    size = next(
        (qnames[n] for n in ("limit", "per_page", "page_size", "count", "$top") if n in qnames),
        None,
    )
    if "page" in qnames:
        frag = "paginate=page"
        if size:
            frag += f" size_param={size}"
        return "page", frag
    offset = next((qnames[n] for n in ("offset", "skip", "$skip") if n in qnames), None)
    if offset:
        frag = f"paginate=offset offset_param={offset}"
        if size:
            frag += f" size_param={size}"
        return "offset", frag
    cursor = next((qnames[n] for n in _CURSORISH if n in qnames), None)
    if cursor:
        # Cursor/keyset needs response-side knowledge (cursor_path / keyset_field) the spec
        # doesn't declare reliably — hint it, don't guess it into the suggested spec.
        return f"cursor-param:{cursor}", None
    # No convention matched. The vocabulary above can never be complete — startIndex/
    # resultsPerPage, startAt/maxResults, from/size are all somebody's house style — so rather
    # than going silent exactly where the agent needs the most help, report the paging-shaped
    # params this endpoint actually declares and let it set offset_param=/size_param= itself.
    # Reported, never guessed into suggested_spec: a wrong paging param is not always ignored,
    # and some APIs reject the whole request over one they don't recognize.
    candidates = [orig for low, orig in qnames.items() if any(w in low for w in _PAGINGISH)]
    if candidates:
        return (
            "unknown; endpoint declares "
            + ", ".join(sorted(candidates))
            + " — set the matching paginate=/offset_param=/size_param= yourself",
            None,
        )
    return None, None


def _props_of(spec: dict, schema: Any) -> dict[str, Any]:
    """A schema's properties, with one level of ``allOf`` branches merged in.

    Composition is everywhere in real specs (a list item that is ``allOf: [Base, {...}]``),
    and without the merge those endpoints would report no fields at all. Own properties win
    over inherited ones.
    """
    schema = _resolve(spec, schema)
    if not isinstance(schema, dict):
        return {}
    own = schema.get("properties")
    merged: dict[str, Any] = dict(own) if isinstance(own, dict) else {}
    branches = schema.get("allOf")
    if isinstance(branches, list):
        for branch in branches:
            sub = _resolve(spec, branch)
            sub_props = sub.get("properties") if isinstance(sub, dict) else None
            if isinstance(sub_props, dict):
                for key, value in sub_props.items():
                    merged.setdefault(key, value)
    return merged


def _records_key(spec: dict, props: dict[str, Any]) -> str | None:
    """The property of an object response holding the record array — None if it IS the record.

    A conventional wrapper name always wins. Failing that, an object is only a list *envelope*
    when it is small and holds exactly one array beside plain scalars (``page``,
    ``total_results``, ``next``). Without that test any object containing an array reads as a
    list: TMDB's ``/movie/{movie_id}`` has ``genres`` fourth among 25 properties, and the old
    first-array-wins rule declared a movie to be a list of genres.
    """
    arrays = [k for k, v in props.items() if _type_of(_resolve(spec, v)) == "array"]
    if not arrays:
        return None
    for key in _COMMON_RECORD_KEYS:
        if key in arrays:
            return key
    if len(arrays) == 1 and len(props) <= _ENVELOPE_MAX_PROPS:
        siblings = (props[k] for k in props if k != arrays[0])
        if all(not _props_of(spec, s) and _type_of(_resolve(spec, s)) != "object" for s in siblings):
            return arrays[0]
    return None


def _locate_records(spec: dict, op: dict) -> tuple[str | None, Any]:
    """``(records dot path, the schema of ONE record)`` for an operation's 2xx JSON response.

    The path is None when undetectable; ``<root>`` means the response is itself the array (no
    ``records=`` needed). Searches the top-level properties (common wrapper names first), then
    one level of nesting. When no array is found anywhere the response object *is* the record
    — the detail-endpoint shape — so the path is None but the schema is still returned, which
    is what gives ``/movie/{movie_id}`` its ``response_fields``.
    """
    responses = op.get("responses") or {}
    resp = responses.get("200") or next(
        (responses[c] for c in sorted(responses) if str(c).startswith("2")), None
    )
    resp = _resolve(spec, resp)
    if not isinstance(resp, dict):
        return None, None
    content = resp.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), None)
    schema = _resolve(spec, (media or {}).get("schema"))
    if not isinstance(schema, dict):
        return None, None
    if _type_of(schema) == "array":
        return "<root>", _resolve(spec, schema.get("items"))
    props = _props_of(spec, schema)
    key = _records_key(spec, props)
    if key:
        return key, _resolve(spec, _resolve(spec, props[key]).get("items"))
    # One nested level: {"payload": {"items": [...]}}. Only worth searching when the root is
    # envelope-shaped itself — a fat record object with an incidental nested array is not a list.
    if len(props) <= _ENVELOPE_MAX_PROPS:
        for outer in props:
            subprops = _props_of(spec, props[outer])
            inner = _records_key(spec, subprops) if subprops else None
            if inner:
                items = _resolve(spec, _resolve(spec, subprops[inner]).get("items"))
                return f"{outer}.{inner}", items
    return None, schema


def _flatten_fields(
    spec: dict, schema: Any, prefix: str = "", depth: int = 1
) -> list[dict[str, Any]]:
    """A record schema's declared fields as ``{name, type}``, flattened to _MAX_FIELD_DEPTH.

    A nested object becomes ``parent.child``; an array of objects becomes ``parent[].child``;
    an array of scalars keeps its own row typed ``array<integer>``. At the depth cap an object
    stops as ``object`` / ``array<object>`` rather than recursing. An undeclared or empty
    schema yields a NULL type rather than raising — real specs contain bare ``{}`` properties
    (TMDB's ``belongs_to_collection``), and a catalog that crashes on one is worthless.
    """
    out: list[dict[str, Any]] = []
    for name, raw in _props_of(spec, schema).items():
        sub = _resolve(spec, raw)
        full = f"{prefix}{name}"
        kind = _type_of(sub)
        if kind == "array":
            item = _resolve(spec, sub.get("items")) if isinstance(sub, dict) else None
            item_props = _props_of(spec, item)
            if item_props and depth < _MAX_FIELD_DEPTH:
                out.extend(_flatten_fields(spec, item, f"{full}[].", depth + 1))
            else:
                inner = _type_of(item) or ("object" if item_props else "any")
                out.append({"name": full, "type": f"array<{inner}>"})
            continue
        nested = _props_of(spec, sub)
        if nested and depth < _MAX_FIELD_DEPTH:
            out.extend(_flatten_fields(spec, sub, f"{full}.", depth + 1))
        else:
            out.append({"name": full, "type": "object" if nested else kind})
    return out


def base_url_of(spec: dict, locator: str) -> str:
    """Public alias for the spec's server URL — the base of the API *connection*."""
    return _base_url(spec, locator)


def _base_url(spec: dict, locator: str) -> str:
    """The first server URL, resolved against the spec's own URL when relative."""
    servers = spec.get("servers") or []
    url = servers[0].get("url", "") if servers and isinstance(servers[0], dict) else ""
    if url and not url.lower().startswith(("http://", "https://")):
        if locator.lower().startswith(("http://", "https://")):
            return urljoin(locator, url)
    return url or "<BASE_URL>"


def _suggested_spec(
    base_url: str,
    path: str,
    records_hint: str | None,
    paginate_frag: str | None,
    auth_frag: str | None,
) -> str:
    """A paste-ready ``api:`` spec for a GET endpoint. Path params stay as ``{braces}`` for
    the agent to substitute; ``<SET_ME>`` marks where a credential env var NAME goes."""
    parts = [f"api:{base_url.rstrip('/')}{path}"]
    if records_hint and records_hint != "<root>":
        parts.append(f"records={records_hint}")
    if paginate_frag:
        parts.append(paginate_frag)
    if auth_frag:
        parts.append(auth_frag)
    return " ".join(parts)
