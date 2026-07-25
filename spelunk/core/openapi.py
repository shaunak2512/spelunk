"""Materialize an OpenAPI spec into a queryable endpoint catalog — the ``openapi:`` source kind.

``openapi:<url-or-path>`` loads an OpenAPI 3.x JSON spec and snapshots **one row per
(path, method)** into the workspace, registered as a view — guidance-as-data, in the same
engine as everything else. Each row carries what an agent needs to call the endpoint through
an ``api:`` source: the params, the auth shape (mapped from ``securitySchemes`` onto the
``auth_env=`` / ``header=`` / ``param=`` options), a pagination hint (heuristic over query
param names), a records hint (first array property of the 200-response schema, one level
deep, ``$ref``-resolved), and — for GET endpoints — a ready-to-paste ``suggested_spec``
column: an ``api:`` spec string with ``<SET_ME>`` where the credential env var name goes.

    SELECT path, summary, suggested_spec FROM tmdb_api
    WHERE method = 'GET' AND path ILIKE '%movie%'

Hints are *hints*: pagination and records-path conventions are not formally declared in
OpenAPI, so the agent verifies with one ``add_source`` attempt (whose error messages list the
response's actual keys). JSON only — a YAML spec must be converted first (e.g. ``yq -o=json``);
Swagger 2.0 specs are rejected with a pointer to conversion tooling.
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
            records_hint = _records_of(spec, op)
            rows.append({
                "path": path,
                "method": method.upper(),
                "operation_id": op.get("operationId"),
                "summary": op.get("summary"),
                "tags": op.get("tags") or [],
                "deprecated": bool(op.get("deprecated")),
                "params": params,
                "auth": auth_kind,
                "pagination_hint": pagination_hint,
                "records_hint": records_hint,
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
    """Path-level + operation-level parameters, ``$ref``-resolved, as compact structs."""
    out: list[dict[str, Any]] = []
    for raw in list(shared) + list(own):
        p = _resolve(spec, raw)
        if not isinstance(p, dict) or not p.get("name"):
            continue
        out.append({
            "name": p["name"],
            "location": p.get("in"),
            "required": bool(p.get("required")),
            "type": _type_of(_resolve(spec, p.get("schema"))),
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
    return None, None


def _records_of(spec: dict, op: dict) -> str | None:
    """Dot path to the records array in the 2xx response schema; None when undetectable.

    ``<root>`` means the response is itself the array (no ``records=`` needed). Searches the
    top-level properties (common wrapper names first), then one level of nesting.
    """
    responses = op.get("responses") or {}
    resp = responses.get("200") or next(
        (responses[c] for c in sorted(responses) if str(c).startswith("2")), None
    )
    resp = _resolve(spec, resp)
    if not isinstance(resp, dict):
        return None
    content = resp.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), None)
    schema = _resolve(spec, (media or {}).get("schema"))
    if not isinstance(schema, dict):
        return None
    if _type_of(schema) == "array":
        return "<root>"
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    ordered = [k for k in _COMMON_RECORD_KEYS if k in props]
    ordered += [k for k in props if k not in ordered]
    for key in ordered:
        sub = _resolve(spec, props[key])
        if _type_of(sub) == "array":
            return key
    for key in ordered:  # one nested level: {"payload": {"items": [...]}}
        sub = _resolve(spec, props[key])
        subprops = sub.get("properties") if isinstance(sub, dict) else None
        if isinstance(subprops, dict):
            for inner_key in [k for k in _COMMON_RECORD_KEYS if k in subprops] + list(subprops):
                if _type_of(_resolve(spec, subprops[inner_key])) == "array":
                    return f"{key}.{inner_key}"
    return None


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
