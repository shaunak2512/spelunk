"""Tests for the ``openapi:`` source kind — spec walking, hint heuristics, and session wiring.

A synthetic OpenAPI 3 spec exercises every mapping path (auth schemes incl. the TMDB
apiKey-named-Authorization quirk, pagination hints, records hints incl. $ref chains and
cycles, suggested_spec assembly); a second one (FIELDS_SPEC) covers response_fields
flattening and the envelope-vs-record test. The real-spec check against TMDB's 148-path spec
lives in tests/live_api_check.py (guarded by the spec file's presence).
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
        # method is stored lowercase, as the OpenAPI document itself keys them
        assert ("/things", "post") in rows
        assert ("/things", "POST") not in rows
        assert rows[("/things", "post")]["suggested_spec"] is None  # api: is GET-only

    def test_bearer_page_records_suggestion(self):
        row = _rows_by_key()[("/things", "get")]
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
        row = _rows_by_key()[("/things/{id}", "get")]
        assert row["auth"] == "none"
        assert row["records_hint"] is None  # response object has no array property
        assert row["suggested_spec"] == "api:https://api.x.test/v1/things/{id}"
        # style/explode are recorded EFFECTIVE — a path param defaults to simple, no explode
        assert row["params"][0] == {
            "name": "id", "location": "path", "required": True, "type": "integer",
            "style": "simple", "explode": False,
        }

    def test_query_param_style_defaults_to_form_explode(self):
        row = _rows_by_key()[("/things", "get")]
        assert [(p["name"], p["style"], p["explode"]) for p in row["params"]] == [
            ("lang", "form", True), ("page", "form", True), ("limit", "form", True)
        ]

    def test_declared_style_overrides_the_default(self):
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://s.test"}],
            "paths": {"/x": {"get": {"parameters": [
                {"name": "ids", "in": "query", "style": "pipeDelimited", "explode": False},
                {"name": "tags", "in": "query", "explode": False},
            ], "responses": {}}}},
        }
        params = openapi.endpoint_rows(spec, "s.json")[0]["params"]
        assert (params[0]["style"], params[0]["explode"]) == ("pipeDelimited", False)
        assert (params[1]["style"], params[1]["explode"]) == ("form", False)

    def test_query_key_auth_and_root_array(self):
        row = _rows_by_key()[("/plain", "get")]
        assert row["auth"] == "query:api_key"
        assert row["records_hint"] == "<root>"
        # top-level array needs no records=; query-key auth becomes param=
        assert row["suggested_spec"] == "api:https://api.x.test/v1/plain param=api_key:<SET_ME>"

    def test_header_key_auth_and_offset(self):
        row = _rows_by_key()[("/hdr", "get")]
        assert row["auth"] == "header:X-Api-Key"
        assert row["pagination_hint"] == "offset"
        assert row["suggested_spec"] == (
            "api:https://api.x.test/v1/hdr paginate=offset offset_param=offset "
            "size_param=limit header=X-Api-Key:<SET_ME>"
        )

    def test_tmdb_auth_quirk_cursor_hint_nested_records(self):
        row = _rows_by_key()[("/tmdb", "get")]
        # apiKey-in-header named Authorization is a bearer token in disguise (TMDB-style)
        assert row["auth"] == "bearer"
        # cursor-ish param is hinted but NOT guessed into the suggested spec
        assert row["pagination_hint"] == "cursor-param:starting_after"
        assert "paginate" not in row["suggested_spec"]
        assert row["records_hint"] == "payload.items"  # one nested level

    def test_ref_cycle_is_safe(self):
        row = _rows_by_key()[("/loop", "get")]
        assert row["records_hint"] is None  # cycle bottoms out, no crash
        assert row["response_fields"] == []

    def test_odata_style_params_hint_offset(self):
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://svc.test"}],
            "paths": {
                "/Orders": {
                    "get": {
                        "parameters": [
                            {"name": "$skip", "in": "query", "schema": {"type": "integer"}},
                            {"name": "$top", "in": "query", "schema": {"type": "integer"}},
                        ],
                        "responses": {},
                    }
                }
            },
        }
        row = openapi.endpoint_rows(spec, "s.json")[0]
        assert row["pagination_hint"] == "offset"
        assert "offset_param=$skip" in row["suggested_spec"]
        assert "size_param=$top" in row["suggested_spec"]

    def test_unrecognized_paging_vocabulary_names_the_candidates(self):
        # No convention matches (startIndex/resultsPerPage is one house style among many, and
        # the list of styles has no end). Reporting nothing here is what pushes an agent into
        # hand-paging N sources, so the hint names the params the document itself declares.
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://svc.test"}],
            "paths": {
                "/cves": {
                    "get": {
                        "parameters": [
                            {"name": "startIndex", "in": "query", "schema": {"type": "integer"}},
                            {
                                "name": "resultsPerPage",
                                "in": "query",
                                "schema": {"type": "integer"},
                            },
                            {"name": "keyword", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {},
                    }
                }
            },
        }
        row = openapi.endpoint_rows(spec, "s.json")[0]
        assert row["pagination_hint"] == (
            "unknown; endpoint declares resultsPerPage, startIndex — set the matching "
            "paginate=/offset_param=/size_param= yourself"
        )
        # Named, never guessed: a paging param the API doesn't recognize can fail the request,
        # so the paste-ready spec stays free of pagination.
        assert "paginate" not in row["suggested_spec"]

    def test_no_paging_params_hints_nothing(self):
        # A detail endpoint must not acquire a pagination hint out of an unrelated param.
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://svc.test"}],
            "paths": {
                "/thing/{id}": {
                    "get": {
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "string"}},
                            {"name": "language", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {},
                    }
                }
            },
        }
        assert openapi.endpoint_rows(spec, "s.json")[0]["pagination_hint"] is None


# A second spec aimed at response_fields: allOf composition, nesting past the depth cap,
# arrays of objects vs arrays of scalars, a bare {} property, and the envelope-vs-record test.
FIELDS_SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://f.test"}],
    "components": {
        "schemas": {
            "Base": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Movie": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {
                        "type": "object",
                        "properties": {
                            "title": {"type": ["string", "null"]},
                            "genre_ids": {"type": "array", "items": {"type": "integer"}},
                            "genres": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        # depth 3 — must stop as `object`, not recurse
                                        "meta": {
                                            "type": "object",
                                            "properties": {"slug": {"type": "string"}},
                                        },
                                    },
                                },
                            },
                            "studio": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "address": {
                                        "type": "object",
                                        "properties": {"city": {"type": "string"}},
                                    },
                                },
                            },
                            "belongs_to": {},  # legal, empty schema — must not crash
                            "tags": {"type": "array"},  # array with no declared items
                        },
                    },
                ]
            },
        }
    },
    "paths": {
        "/movies": {  # envelope: conventional wrapper key
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "results": {
                        "type": "array", "items": {"$ref": "#/components/schemas/Movie"}
                    },
                },
            }}}}}}
        },
        "/movies/{id}": {  # detail: the response object IS the record
            "get": {"responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Movie"}
            }}}}}
        },
        "/shows": {  # envelope with a non-standard wrapper name, but envelope-shaped
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "total": {"type": "integer"},
                    "shows": {"type": "array", "items": {
                        "type": "object", "properties": {"name": {"type": "string"}}
                    }},
                },
            }}}}}}
        },
        "/ids": {  # root array of scalars
            "get": {"responses": {"200": {"content": {"application/json": {
                "schema": {"type": "array", "items": {"type": "integer"}}
            }}}}}
        },
    },
}


def _fields(path: str) -> dict[str, str | None]:
    row = next(
        r for r in openapi.endpoint_rows(FIELDS_SPEC, "f.json") if r["path"] == path
    )
    return {f["name"]: f["type"] for f in row["response_fields"]}


class TestResponseFields:
    def test_list_endpoint_describes_the_record_not_the_envelope(self):
        row = next(r for r in openapi.endpoint_rows(FIELDS_SPEC, "f.json")
                   if r["path"] == "/movies")
        assert row["records_hint"] == "results"
        names = {f["name"] for f in row["response_fields"]}
        assert "page" not in names  # envelope keys are not the record's fields
        assert {"id", "title", "genre_ids"} <= names

    def test_allof_branches_are_merged(self):
        # `id` comes from the $ref'd Base branch, `title` from the inline one
        assert _fields("/movies")["id"] == "integer"
        assert _fields("/movies")["title"] == "string"  # 3.1 ["string","null"] -> string

    def test_array_of_scalars_keeps_one_row(self):
        assert _fields("/movies")["genre_ids"] == "array<integer>"

    def test_array_of_objects_flattens_with_bracket_notation(self):
        f = _fields("/movies")
        assert f["genres[].name"] == "string"
        assert "genres" not in f

    def test_nested_object_flattens_with_dots(self):
        assert _fields("/movies")["studio.name"] == "string"

    def test_depth_cap_stops_without_recursing(self):
        f = _fields("/movies")
        assert f["genres[].meta"] == "object"  # depth 3 would be genres[].meta.slug
        assert f["studio.address"] == "object"
        assert not any(k.endswith(".slug") or k.endswith(".city") for k in f)

    def test_empty_schema_degrades_to_null_type(self):
        assert _fields("/movies")["belongs_to"] is None

    def test_array_without_items_is_array_any(self):
        assert _fields("/movies")["tags"] == "array<any>"

    def test_detail_endpoint_has_no_records_hint_but_full_fields(self):
        row = next(r for r in openapi.endpoint_rows(FIELDS_SPEC, "f.json")
                   if r["path"] == "/movies/{id}")
        # The record object holds arrays (genre_ids, genres, tags) but is NOT a list envelope:
        # the old first-array-wins rule called TMDB's /movie/{id} a list of genres.
        assert row["records_hint"] is None
        assert _fields("/movies/{id}") == _fields("/movies")

    def test_nonstandard_wrapper_still_reads_as_an_envelope(self):
        row = next(r for r in openapi.endpoint_rows(FIELDS_SPEC, "f.json")
                   if r["path"] == "/shows")
        assert row["records_hint"] == "shows"
        assert _fields("/shows") == {"name": "string"}

    def test_root_array_of_scalars_has_no_fields(self):
        row = next(r for r in openapi.endpoint_rows(FIELDS_SPEC, "f.json")
                   if r["path"] == "/ids")
        assert row["records_hint"] == "<root>"
        assert row["response_fields"] == []

    def test_fat_object_with_one_array_is_a_record(self):
        """Six scalars + one array is past the envelope size — a record, not a list."""
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://s.test"}],
            "paths": {"/wide": {"get": {"responses": {"200": {"content": {
                "application/json": {"schema": {"type": "object", "properties": {
                    "a": {"type": "string"}, "b": {"type": "string"},
                    "c": {"type": "string"}, "d": {"type": "string"},
                    "e": {"type": "string"}, "f": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                }}}
            }}}}}},
        }
        row = openapi.endpoint_rows(spec, "s.json")[0]
        assert row["records_hint"] is None
        assert {f["name"] for f in row["response_fields"]} == {
            "a", "b", "c", "d", "e", "f", "labels"
        }


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


class TestBaseUrl:
    """OAS-014: a relative ``servers[0].url`` (Petstore's ``/api/v3``) only resolves when the
    document itself was fetched over http(s). Off disk there is no host to resolve against,
    and returning the relative path verbatim builds a scheme-less connection that fails much
    later, inside urlopen, as "unknown url type"."""

    RELATIVE = {**SPEC, "servers": [{"url": "/api/v3"}]}

    def test_relative_server_resolves_against_a_remote_locator(self):
        assert (
            openapi.base_url_of(self.RELATIVE, "https://petstore.test/openapi.json")
            == "https://petstore.test/api/v3"
        )

    def test_relative_server_on_a_local_file_falls_back_to_the_placeholder(self):
        # Same marker a MISSING server gets: visible in suggested_spec, refused by parse_api_spec.
        assert openapi.base_url_of(self.RELATIVE, "./petstore.json") == "<BASE_URL>"

    def test_attaching_a_local_spec_with_no_host_names_the_fix(self, tmp_path):
        spec_file = tmp_path / "petstore.json"
        spec_file.write_text(json.dumps(self.RELATIVE), encoding="utf-8")
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            with pytest.raises(ValueError, match="base_url=https://"):
                session.add_source(f"openapi:{spec_file}")
        finally:
            session.close()

    def test_base_url_option_supplies_the_missing_host(self, tmp_path):
        spec_file = tmp_path / "petstore.json"
        spec_file.write_text(json.dumps(self.RELATIVE), encoding="utf-8")
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            out = session.add_source(
                f"openapi:{spec_file} base_url=https://petstore.test/api/v3/"
            )
            assert out["info"]["base_url"] == "https://petstore.test/api/v3"
        finally:
            session.close()

    def test_base_url_option_overrides_a_declared_server(self, tmp_path):
        """A spec whose servers[0] points at production, pointed at a staging host instead."""
        spec_file = tmp_path / "api.json"
        spec_file.write_text(json.dumps(SPEC), encoding="utf-8")
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            out = session.add_source(f"openapi:{spec_file} base_url=https://staging.x.test/v1")
            assert out["info"]["base_url"] == "https://staging.x.test/v1"
        finally:
            session.close()


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
                "WHERE method = 'get' AND pagination_hint = 'page'",
                name="paged",
            )
            assert r["row_count"] == 1
            assert r["sample"][0][0] == "/things"
            session.remove_source("my_api")
        finally:
            session.close()

    def test_response_fields_are_queryable_as_a_list_of_structs(self, tmp_path):
        """The point of the column: find an endpoint by the data it returns, in SQL."""
        spec_file = tmp_path / "fields-api.json"
        spec_file.write_text(json.dumps(FIELDS_SPEC), encoding="utf-8")
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            session.add_source(f"openapi:{spec_file}")
            r = session.query(
                "SELECT path FROM fields_api "
                "WHERE method = 'get' "
                "  AND 'studio.name' IN (SELECT f.name FROM UNNEST(response_fields) AS t(f)) "
                "ORDER BY path",
                name="carries_studio",
            )
            assert [row[0] for row in r["sample"]] == ["/movies", "/movies/{id}"]
        finally:
            session.close()

    def test_detect_kind(self):
        assert sources.detect_kind("openapi:./spec.json") == "openapi"
        assert sources.detect_kind("openapi:https://x.test/openapi.json") == "openapi"

    def test_build_without_workspace_rejected(self):
        with pytest.raises(ValueError, match="workspace"):
            sources.build_source("openapi:./spec.json")
