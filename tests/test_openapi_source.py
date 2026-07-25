"""Tests for the ``openapi:`` source kind — spec walking, hint heuristics, and session wiring.

A synthetic OpenAPI 3 spec exercises every mapping path (auth schemes incl. the TMDB
apiKey-named-Authorization quirk, pagination hints, records hints incl. $ref chains and
cycles, suggested_spec assembly). The real-spec check against TMDB's 148-path spec lives in
tests/live_api_check.py (guarded by the spec file's presence).
"""
from __future__ import annotations

import json

import pytest

from spelunk.core import openapi, sources
from spelunk.core.duck import DuckSession

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.x.test/v1"}],
    "security": [{"bearerAuth": []}],
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "keyHeader": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "keyQuery": {"type": "apiKey", "in": "query", "name": "api_key"},
            "tmdbStyle": {"type": "apiKey", "in": "header", "name": "Authorization"},
        },
        "schemas": {
            "Thing": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "ThingList": {
                "type": "object",
                "properties": {"total": {"type": "integer"}, "results": {"type": "array"}},
            },
            "Loop": {"$ref": "#/components/schemas/Loop"},
        },
    },
    "paths": {
        "/things": {
            "parameters": [{"name": "lang", "in": "query", "schema": {"type": "string"}}],
            "get": {
                "operationId": "listThings",
                "summary": "List things",
                "tags": ["things"],
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ThingList"}
                            }
                        }
                    }
                },
            },
            "post": {"operationId": "createThing", "responses": {}},
        },
        "/things/{id}": {
            "get": {
                "operationId": "getThing",
                "security": [],  # explicit override: public
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        }
                    }
                },
            },
        },
        "/plain": {
            "get": {
                "security": [{"keyQuery": []}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"type": "array", "items": {}}}
                        }
                    }
                },
            },
        },
        "/hdr": {
            "get": {
                "security": [{"keyHeader": []}],
                "parameters": [
                    {"name": "offset", "in": "query", "schema": {"type": "integer"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {},
            },
        },
        "/tmdb": {
            "get": {
                "security": [{"tmdbStyle": []}],
                "parameters": [{"name": "starting_after", "in": "query"}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "meta": {"type": "object"},
                                        "payload": {
                                            "type": "object",
                                            "properties": {"items": {"type": "array"}},
                                        },
                                    },
                                }
                            }
                        }
                    }
                },
            },
        },
        "/loop": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Loop"}}
                        }
                    }
                },
            },
        },
    },
}


def _rows_by_key(spec=SPEC):
    return {(r["path"], r["method"]): r for r in openapi.endpoint_rows(spec, "spec.json")}


class TestEndpointRows:
    def test_row_inventory(self):
        rows = _rows_by_key()
        assert len(rows) == 7
        assert ("/things", "POST") in rows
        assert rows[("/things", "POST")]["suggested_spec"] is None  # api: is GET-only

    def test_bearer_page_records_suggestion(self):
        row = _rows_by_key()[("/things", "GET")]
        assert row["auth"] == "bearer"
        assert row["pagination_hint"] == "page"
        assert row["records_hint"] == "results"  # via $ref
        assert row["suggested_spec"] == (
            "api:https://api.x.test/v1/things records=results paginate=page "
            "size_param=limit auth_env=<SET_ME>"
        )
        # path-level + op-level params are merged
        assert [p["name"] for p in row["params"]] == ["lang", "page", "limit"]

    def test_public_override_and_path_params(self):
        row = _rows_by_key()[("/things/{id}", "GET")]
        assert row["auth"] == "none"
        assert row["records_hint"] is None  # response object has no array property
        assert row["suggested_spec"] == "api:https://api.x.test/v1/things/{id}"
        assert row["params"][0] == {
            "name": "id", "location": "path", "required": True, "type": "integer"
        }

    def test_query_key_auth_and_root_array(self):
        row = _rows_by_key()[("/plain", "GET")]
        assert row["auth"] == "query:api_key"
        assert row["records_hint"] == "<root>"
        # top-level array needs no records=; query-key auth becomes param=
        assert row["suggested_spec"] == "api:https://api.x.test/v1/plain param=api_key:<SET_ME>"

    def test_header_key_auth_and_offset(self):
        row = _rows_by_key()[("/hdr", "GET")]
        assert row["auth"] == "header:X-Api-Key"
        assert row["pagination_hint"] == "offset"
        assert row["suggested_spec"] == (
            "api:https://api.x.test/v1/hdr paginate=offset offset_param=offset "
            "size_param=limit header=X-Api-Key:<SET_ME>"
        )

    def test_tmdb_auth_quirk_cursor_hint_nested_records(self):
        row = _rows_by_key()[("/tmdb", "GET")]
        # apiKey-in-header named Authorization is a bearer token in disguise (TMDB-style)
        assert row["auth"] == "bearer"
        # cursor-ish param is hinted but NOT guessed into the suggested spec
        assert row["pagination_hint"] == "cursor-param:starting_after"
        assert "paginate" not in row["suggested_spec"]
        assert row["records_hint"] == "payload.items"  # one nested level

    def test_ref_cycle_is_safe(self):
        row = _rows_by_key()[("/loop", "GET")]
        assert row["records_hint"] is None  # cycle bottoms out, no crash


class TestLoadSpec:
    def test_swagger2_rejected(self, tmp_path):
        p = tmp_path / "old.json"
        p.write_text(json.dumps({"swagger": "2.0", "paths": {}}), encoding="utf-8")
        with pytest.raises(ValueError, match="Swagger 2.0"):
            openapi.load_spec(str(p))

    def test_yaml_rejected_actionably(self, tmp_path):
        p = tmp_path / "spec.yaml"
        p.write_text("openapi: 3.0.0\npaths: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML.*converted"):
            openapi.load_spec(str(p))

    def test_not_a_spec(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"hello": 1}), encoding="utf-8")
        with pytest.raises(ValueError, match="does not look like an OpenAPI 3.x spec"):
            openapi.load_spec(str(p))

    def test_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="Cannot read"):
            openapi.load_spec(str(tmp_path / "nope.json"))

    def test_remote_spec(self, monkeypatch):
        monkeypatch.setattr(openapi, "_get_json", lambda url, headers: (SPEC, {}))
        spec = openapi.load_spec("https://api.x.test/openapi.json")
        assert spec["openapi"] == "3.0.0"


class TestSessionIntegration:
    def test_attach_query_remove(self, tmp_path):
        spec_file = tmp_path / "my-api.json"
        spec_file.write_text(json.dumps(SPEC), encoding="utf-8")
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            out = session.add_source(f"openapi:{spec_file}")
            assert out["kind"] == "openapi"
            assert out["name"] == "my_api"  # derived from the filename
            assert out["info"]["endpoints"] == 7
            r = session.query(
                "SELECT path, suggested_spec FROM my_api "
                "WHERE method = 'GET' AND pagination_hint = 'page'",
                name="paged",
            )
            assert r["row_count"] == 1
            assert r["sample"][0][0] == "/things"
            session.remove_source("my_api")
        finally:
            session.close()

    def test_detect_kind(self):
        assert sources.detect_kind("openapi:./spec.json") == "openapi"
        assert sources.detect_kind("openapi:https://x.test/openapi.json") == "openapi"

    def test_build_without_workspace_rejected(self):
        with pytest.raises(ValueError, match="workspace"):
            sources.build_source("openapi:./spec.json")
