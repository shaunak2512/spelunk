"""Tests for API *connections* and the ``fetch`` tool — one API as one source.

An ``openapi:`` source is both a queryable endpoint catalog and a live connection (base URL +
credentials); ``fetch`` calls any endpoint under it and materializes the response as a
flow-scoped result rather than a new source. Covered here: the connection/request split and
param layering, path safety, the batch form, the ``rows_from`` fan-out (key stamping, 404-skip,
caps), and the lineage/replay contract. Everything runs against the local mock server from
``test_api_source`` — no network.
"""
from __future__ import annotations

import json

import pytest

from spelunk.core import apifetch
from spelunk.core.duck import DuckSession

# The `api` fixture (local threaded mock server) comes from conftest.py.


def _spec(base_url: str) -> dict:
    """An OpenAPI spec whose server points at the mock API."""
    return {
        "openapi": "3.0.0",
        "servers": [{"url": base_url}],
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
        },
        "security": [{"bearerAuth": []}],
        "paths": {
            "/movies": {
                "get": {
                    "operationId": "listMovies",
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "integer"}},
                        {"name": "sort_by", "in": "query", "schema": {"type": "string"}},
                        # explode=true -> repeated keys; the catalog is what settles this
                        {"name": "with_tags", "in": "query", "explode": True,
                         "schema": {"type": "array"}},
                        # explode=false -> comma-joined into one key
                        {"name": "with_ids", "in": "query", "explode": False,
                         "schema": {"type": "array"}},
                    ],
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "page": {"type": "integer"},
                            "results": {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "title": {"type": "string"},
                                },
                            }},
                        },
                    }}}}},
                }
            },
            "/movies/{movie_id}": {
                "get": {
                    "parameters": [
                        {"name": "movie_id", "in": "path", "required": True,
                         "schema": {"type": "integer"}}
                    ],
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "budget": {"type": "integer"},
                            "revenue": {"type": "integer"},
                        },
                    }}}}},
                }
            },
            "/studios/{country}/{studio}": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"name": {"type": "string"}},
                }}}}}}
            },
        },
    }


@pytest.fixture()
def session(tmp_path, api):  # noqa: F811
    """A session with the mock API attached as connection ``mock``."""
    spec_file = tmp_path / "mock-api.json"
    spec_file.write_text(json.dumps(_spec(api.base)), encoding="utf-8")
    sess = DuckSession.open([], session_dir=str(tmp_path / "sess"))
    sess.spec_file = str(spec_file)
    try:
        yield sess
    finally:
        sess.close()


def _attach(session, options: str = "") -> dict:
    return session.add_source(f"mock=openapi:{session.spec_file} {options}".strip())


def _movies(nth, query):
    return 200, {"page": 1, "results": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]}, {}


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
class TestConnection:
    def test_attach_exposes_base_url_and_catalog(self, session, api):  # noqa: F811
        out = _attach(session)
        assert out["kind"] == "openapi"
        assert out["info"]["base_url"] == api.base
        assert out["info"]["endpoints"] == 3
        # attaching a connection performs NO endpoint fetch
        assert api.calls == []

    def test_connection_carries_credentials_and_defaults(self, session):
        _attach(session, "auth_env=TOK records=results paginate=page default_param=lang:en")
        conn = next(s for s in session.sources if s.name == "mock").connection
        assert conn.auth_env == "TOK"
        assert conn.defaults == {"records": "results", "paginate": "page"}
        assert conn.params == {"lang": "en"}

    def test_bad_default_rejected_at_attach_not_first_fetch(self, session):
        with pytest.raises(ValueError, match="Unknown paginate style"):
            _attach(session, "paginate=nonsense")

    def test_unknown_option_rejected(self, session):
        with pytest.raises(ValueError, match="Unknown connection option"):
            _attach(session, "bogus=1")

    def test_bool_default_is_coerced_not_left_a_string(self, session):
        """`json=false` must be False — the string "false" is truthy and would flip typing ON."""
        _attach(session, "json=false")
        conn = next(s for s in session.sources if s.name == "mock").connection
        assert conn.defaults == {"json": False}
        req = apifetch.ApiRequest(path="/movies")
        assert apifetch.resolved_option(conn, req, "json", False) is False

    def test_bool_default_true_is_coerced(self, session):
        _attach(session, "json=true")
        conn = next(s for s in session.sources if s.name == "mock").connection
        assert conn.defaults == {"json": True}

    def test_non_boolean_bool_default_rejected_at_attach(self, session):
        with pytest.raises(ValueError, match="must be a boolean"):
            _attach(session, "json=banana")

    def test_non_integer_int_default_rejected_at_attach(self, session):
        with pytest.raises(ValueError, match="must be an integer"):
            _attach(session, "max_pages=lots")


# --------------------------------------------------------------------------- #
# fetch — the single-request path
# --------------------------------------------------------------------------- #
class TestFetch:
    def test_materializes_response_as_a_result(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        _attach(session, "records=results")
        out = session.fetch(source="mock", path="/movies", name="movies")
        assert out["row_count"] == 2
        assert out["complete"] is True
        assert [c["name"] for c in out["columns"]] == ["id", "title"]
        # it is a RESULT, not a source — queryable by name, and no new source appeared
        assert session.query("SELECT title FROM movies ORDER BY id", name="t")["row_count"] == 2
        assert [s.name for s in session.sources] == ["mock"]

    def test_many_endpoints_one_source(self, session, api):  # noqa: F811
        """The whole point: five endpoints cost five results, not five sources."""
        api.handlers["/movies"] = _movies
        api.handlers["/movies/7"] = lambda n, q: (200, {"id": 7, "budget": 10, "revenue": 30}, {})
        _attach(session)
        session.fetch(source="mock", path="/movies", name="list", records="results")
        session.fetch(source="mock", path="/movies/{movie_id}", name="detail",
                      params={"movie_id": 7})
        assert [s.name for s in session.sources] == ["mock"]
        listed = {r["name"] for r in session.catalog("default")["results"]}
        assert {"list", "detail"} <= listed

    def test_credentials_come_from_the_connection(self, session, api, monkeypatch):  # noqa: F811
        monkeypatch.setenv("TOK", "s3cret")
        api.handlers["/movies"] = _movies
        _attach(session, "auth_env=TOK records=results")
        session.fetch(source="mock", path="/movies", name="m")
        assert api.calls[0]["headers"]["authorization"] == "Bearer s3cret"

    def test_unknown_source_lists_connections(self, session):
        _attach(session)
        with pytest.raises(ValueError, match="No API connection named 'nope'"):
            session.fetch(source="nope", path="/movies", name="x")


# --------------------------------------------------------------------------- #
# Params
# --------------------------------------------------------------------------- #
class TestEndpointHints:
    """The catalog supplies records=/paginate= per endpoint, so callers rarely restate them."""

    def test_records_and_pagination_come_from_the_catalog(self, session, api):  # noqa: F811
        api.handlers["/movies"] = lambda n, q: (
            200, {"page": n, "results": [{"id": n, "title": "x"}] if n <= 2 else []}, {}
        )
        _attach(session)  # no records=/paginate= anywhere
        out = session.fetch(source="mock", path="/movies", name="m")
        assert out["row_count"] == 2                       # records=results, inferred
        assert [c["query"].get("page") for c in api.calls] == ["1", "2", "3"]

    def test_endpoint_hint_beats_a_connection_wide_default(self, session, api):  # noqa: F811
        """A detail endpoint must not inherit the connection's paginate=page."""
        api.handlers["/movies/1"] = lambda n, q: (200, {"id": 1, "budget": 5}, {})
        _attach(session, "paginate=page records=results")
        session.fetch(source="mock", path="/movies/{movie_id}", name="d",
                      params={"movie_id": 1})
        assert len(api.calls) == 1                     # one request, not a pagination loop
        assert "page" not in api.calls[0]["query"]

    def test_an_explicit_call_arg_still_wins(self, session, api):  # noqa: F811
        api.handlers["/undocumented"] = lambda n, q: (
            200, {"rows": [{"id": n}]} if n == 1 else {"rows": []}, {}
        )
        _attach(session)
        out = session.fetch(source="mock", path="/undocumented", name="u",
                            records="rows", paginate="page")
        assert out["row_count"] == 1
        assert api.calls[0]["query"]["page"] == "1"


class TestParams:
    def test_call_params_beat_connection_defaults_beat_inline_query(self, session, api):  # noqa: F811, E501
        api.handlers["/movies"] = _movies
        _attach(session, "records=results default_param=lang:en default_param=region:AU")
        session.fetch(source="mock", path="/movies?lang=xx&sort_by=name", name="m",
                      params={"region": "NZ"})
        query = api.calls[0]["query"]
        assert query["lang"] == "en"        # connection default beats the inline query string
        assert query["region"] == "NZ"      # call params beat the connection default
        assert query["sort_by"] == "name"   # inline survives when nothing overrides it

    def test_types_render_as_the_wire_expects(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        _attach(session, "records=results")
        session.fetch(source="mock", path="/movies", name="m",
                      params={"flag": True, "n": 3, "dropped": None})
        query = api.calls[0]["query"]
        assert query["flag"] == "true"      # JSON's booleans, not Python's True
        assert query["n"] == "3"
        assert "dropped" not in query       # None drops the param entirely

    def test_list_serialization_follows_the_catalog(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        _attach(session, "records=results")
        session.fetch(source="mock", path="/movies", name="m",
                      params={"with_tags": [1, 2], "with_ids": [3, 4], "undeclared": [5, 6]})
        raw = api.calls[0]
        assert "with_tags=1&with_tags=2" in raw["query_string"]  # declared explode=true
        assert raw["query"]["with_ids"] == "3,4"                 # declared explode=false
        # a param the spec never mentions falls back to comma-joining rather than guessing
        assert raw["query"]["undeclared"] == "5,6"

    def test_pagination_param_is_reserved(self, session):
        _attach(session, "records=results paginate=page")
        with pytest.raises(ValueError, match="Param 'page' is reserved.*start="):
            session.fetch(source="mock", path="/movies", name="m", params={"page": 3})

    def test_credential_shaped_param_is_refused(self, session):
        _attach(session)
        with pytest.raises(ValueError, match="looks like a credential"):
            session.fetch(source="mock", path="/movies", name="m",
                          params={"api_key": "abc123"})

    def test_connection_credential_param_is_reserved(self, session):
        _attach(session, "param=token:TOK")
        with pytest.raises(ValueError, match="Param 'token' is reserved"):
            session.fetch(source="mock", path="/movies", name="m", params={"token": "x"})

    def test_placeholder_consumes_its_param(self, session, api):  # noqa: F811
        api.handlers["/movies/42"] = lambda n, q: (200, {"id": 42, "budget": 1}, {})
        _attach(session)
        session.fetch(source="mock", path="/movies/{movie_id}", name="d",
                      params={"movie_id": 42})
        assert api.calls[0]["path"] == "/movies/42"
        assert "movie_id" not in api.calls[0]["query"]  # it went into the path, not the query

    def test_unfilled_placeholder_is_actionable(self, session):
        _attach(session)
        with pytest.raises(ValueError, match="unfilled placeholder.*rows_from"):
            session.fetch(source="mock", path="/movies/{movie_id}", name="d")


# --------------------------------------------------------------------------- #
# Path safety — a connection is a host + base-path allowlist entry
# --------------------------------------------------------------------------- #
class TestPathSafety:
    @pytest.mark.parametrize(
        "path", ["https://evil.test/steal", "//evil.test/steal", "http://evil.test/x"]
    )
    def test_absolute_url_refused(self, session, path):
        _attach(session)
        with pytest.raises(ValueError, match="must be relative"):
            session.fetch(source="mock", path=path, name="x")

    def test_dot_dot_refused(self, session):
        _attach(session)
        with pytest.raises(ValueError, match="'\\.\\.' segments"):
            session.fetch(source="mock", path="/movies/../../admin", name="x")

    def test_template_value_cannot_escape_its_segment(self, session, api):  # noqa: F811
        """A bound value with / ? or # must not redirect the request or add params."""
        api.handlers["/movies/a%2F..%2Fadmin%3Fx%3D1"] = lambda n, q: (200, {"id": 1}, {})
        _attach(session)
        session.fetch(source="mock", path="/movies/{movie_id}", name="d",
                      params={"movie_id": "a/../admin?x=1"})
        assert api.calls[0]["path"] == "/movies/a%2F..%2Fadmin%3Fx%3D1"
        assert api.calls[0]["query"] == {}

    def test_query_string_placeholder_refused(self, session, api):  # noqa: F811
        """Nothing substitutes a `{name}` after the `?` — it would go on the wire literally."""
        _attach(session)
        with pytest.raises(ValueError, match="templates query param"):
            session.fetch(source="mock", path="/movies?language={lang}", name="x",
                          params={"lang": "en-US"})
        assert api.calls == []

    def test_query_string_placeholder_error_names_the_query_key(self, session):
        """The fix is params={"language": ...} — the placeholder's own name would be wrong."""
        _attach(session)
        with pytest.raises(ValueError, match=r'params=\{"language"'):
            session.fetch(source="mock", path="/movies/{movie_id}?language={lang}", name="x",
                          params={"movie_id": 1, "lang": "en"})

    def test_uncatalogued_path_is_still_fetched(self, session, api):  # noqa: F811
        """The spec establishes the connection, not the reachable surface — specs go stale."""
        api.handlers["/undocumented"] = lambda n, q: (200, [{"id": 1}], {})
        _attach(session)
        out = session.fetch(source="mock", path="/undocumented", name="u")
        assert out["row_count"] == 1


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #
class TestFetchSteps:
    def test_batch_runs_in_order(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        api.handlers["/movies/1"] = lambda n, q: (200, {"id": 1, "budget": 5}, {})
        _attach(session)
        out = session.fetch_steps([
            {"source": "mock", "path": "/movies", "name": "list", "records": "results"},
            {"source": "mock", "path": "/movies/{movie_id}", "name": "one",
             "params": {"movie_id": 1}},
        ])
        assert out["completed"] == 2
        assert [s["status"] for s in out["steps"]] == ["ok", "ok"]

    def test_failure_is_fail_fast_and_keeps_earlier_steps(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        _attach(session)
        out = session.fetch_steps([
            {"source": "mock", "path": "/movies", "name": "list", "records": "results"},
            {"source": "mock", "path": "/gone", "name": "bad"},
            {"source": "mock", "path": "/movies", "name": "never", "records": "results"},
        ])
        assert out["failed_step"] == 1
        assert [s["status"] for s in out["steps"]] == ["ok", "failed", "skipped"]
        assert session.query("SELECT * FROM list", name="t")["row_count"] == 2


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #
class TestFanOut:
    def _prep(self, session, api, ids=(1, 2, 3)):  # noqa: F811
        for i in ids:
            api.handlers[f"/movies/{i}"] = (
                lambda n, q, i=i: (200, {"id": i, "budget": i * 10, "revenue": i * 30}, {})
            )
        _attach(session)
        session.query(
            "SELECT * FROM (VALUES " + ",".join(f"({i})" for i in ids) + ") AS t(movie_id)",
            name="ids",
        )

    def test_one_request_per_row_stamped_for_the_join_back(self, session, api):  # noqa: F811
        self._prep(session, api)
        out = session.fetch(source="mock", path="/movies/{movie_id}", name="details",
                            rows_from="ids")
        assert out["row_count"] == 3
        assert out["info"]["urls"] == 3
        assert "_key_movie_id" in [c["name"] for c in out["columns"]]
        joined = session.query(
            "SELECT COUNT(*) AS n FROM ids JOIN details ON ids.movie_id = details._key_movie_id",
            name="j",
        )
        assert joined["sample"][0][0] == 3

    def test_rows_are_deduped_and_deterministically_ordered(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(2, 1, 2, 1))
        out = session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")
        # four rows, two distinct -> two requests (workers may issue them in either order)
        assert out["info"]["urls"] == 2
        assert sorted(c["path"] for c in api.calls) == ["/movies/1", "/movies/2"]
        # the SNAPSHOT is what must be deterministic: rows land in bound-column order
        ordered = session.query("SELECT _key_movie_id FROM d", name="k")
        assert [r[0] for r in ordered["sample"]] == ["1", "2"]

    def test_nulls_in_the_bound_column_are_dropped(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(1,))
        session.query(
            "SELECT * FROM (VALUES (1), (NULL)) AS t(movie_id)", name="ids"
        )
        out = session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")
        assert out["info"]["urls"] == 1  # no /movies/None request

    def test_multi_placeholder_binding(self, session, api):  # noqa: F811
        api.handlers["/studios/au/village"] = lambda n, q: (200, {"name": "Village"}, {})
        api.handlers["/studios/nz/weta"] = lambda n, q: (200, {"name": "Weta"}, {})
        _attach(session)
        session.query(
            "SELECT * FROM (VALUES ('au','village'), ('nz','weta')) AS t(country, studio)",
            name="pairs",
        )
        out = session.fetch(source="mock", path="/studios/{country}/{studio}", name="s",
                            rows_from="pairs")
        assert out["row_count"] == 2
        assert {"_key_country", "_key_studio"} <= {c["name"] for c in out["columns"]}

    def test_key_columns_are_exactly_the_stamped_columns(self, session, api):  # noqa: F811
        """`key_columns` is the join contract — it must never name a column no row carries."""
        self._prep(session, api)
        out = session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")
        columns = {c["name"] for c in out["columns"]}
        assert set(out["info"]["key_columns"]) <= columns

    def test_404_is_data_not_an_error(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(1, 2))
        session.query("SELECT * FROM (VALUES (1),(2),(999)) AS t(movie_id)", name="ids")
        out = session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")
        assert out["row_count"] == 2
        assert out["info"]["skipped_count"] == 1
        assert out["info"]["skipped"] == [{"movie_id": "999"}]
        # never a silent gap — the agent is told before it aggregates
        assert any("404" in h for h in out["hints"])

    def test_auth_failure_aborts_the_whole_fanout(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(1,))
        api.handlers["/movies/1"] = lambda n, q: (401, {"error": "nope"}, {})
        with pytest.raises(ValueError, match="401"):
            session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")

    def test_max_urls_errors_rather_than_truncating(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(1, 2, 3))
        with pytest.raises(ValueError, match="over the 2-URL cap.*LIMIT the prep query"):
            session.fetch(source="mock", path="/movies/{movie_id}", name="d",
                          rows_from="ids", max_urls=2)

    def test_missing_column_names_the_fix(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(1,))
        session.query("SELECT 1 AS wrong_name", name="ids")
        with pytest.raises(ValueError, match="Alias the prep query's columns"):
            session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")

    def test_rows_from_without_a_placeholder_is_rejected(self, session, api):  # noqa: F811
        self._prep(session, api, ids=(1,))
        with pytest.raises(ValueError, match="needs at least one"):
            session.fetch(source="mock", path="/movies", name="d", rows_from="ids")

    @pytest.mark.parametrize(
        "hostile",
        [
            'ids" AS SELECT 1; CREATE TABLE pwned AS SELECT 1; --',
            'x"."y',
            'default"."ids',
            "ids; DROP TABLE ids",
            "ids-with-dashes",
        ],
    )
    def test_rows_from_cannot_break_out_of_its_quoted_identifier(  # noqa: F811
        self, session, api, hostile
    ):
        """SEC-016: rows_from is split on '.' straight into two double-quoted identifiers, and
        the workspace connection those run on is NOT read-only — so it goes through the same
        name gate as every other flow/name entry point."""
        self._prep(session, api, ids=(1,))
        with pytest.raises(ValueError, match="Invalid rows_from (flow|result) name"):
            session.fetch(
                source="mock", path="/movies/{movie_id}", name="d", rows_from=hostile
            )
        assert "pwned" not in {o.name for o in session.list_objects()}


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
class TestLineage:
    def test_fetch_node_records_the_request_and_its_edges(self, session, api):  # noqa: F811
        api.handlers["/movies/1"] = lambda n, q: (200, {"id": 1, "budget": 5}, {})
        api.handlers["/movies"] = _movies
        _attach(session)
        session.query("SELECT 1 AS movie_id", name="ids")
        session.fetch(source="mock", path="/movies/{movie_id}", name="d", rows_from="ids")
        session.query("SELECT budget FROM d", name="roi")

        graph = session.lineage(name="roi")
        nodes = {n["name"]: n for n in graph["nodes"]}
        assert nodes["d"]["kind"] == "fetch"
        assert nodes["d"]["sql"].startswith("FETCH ")
        assert json.loads(nodes["d"]["sql"][len("FETCH "):])["path"] == "/movies/{movie_id}"
        assert "mock" in nodes["d"]["sources"]
        # ids -> d -> roi is one connected chain, end to end
        assert graph["order"] == ["default.ids", "default.d", "default.roi"]
        assert {"from": "default.ids", "to": "default.d"} in graph["edges"]

    def test_secrets_never_reach_the_lineage_row(self, session, api, monkeypatch):  # noqa: F811
        monkeypatch.setenv("TOK", "s3cret")
        api.handlers["/movies"] = _movies
        _attach(session, "auth_env=TOK records=results param=sig:TOK")
        session.fetch(source="mock", path="/movies", name="m")
        sql = session.lineage(name="m")["nodes"][0]["sql"]
        assert "s3cret" not in sql

    def test_replay_preserves_fetch_results_instead_of_refetching(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        _attach(session, "records=results")
        session.fetch(source="mock", path="/movies", name="m")
        session.query("SELECT COUNT(*) AS n FROM m", name="counted")
        before = len(api.calls)

        out = session.replay()
        assert [r["name"] for r in out["rebuilt"]] == ["counted"]
        assert [p["name"] for p in out["preserved"]] == ["m"]
        assert len(api.calls) == before  # replay does NOT re-issue the request

    def test_replay_into_a_fresh_flow_copies_the_snapshot(self, session, api):  # noqa: F811
        api.handlers["/movies"] = _movies
        _attach(session, "records=results")
        session.fetch(source="mock", path="/movies", name="m")
        session.query("SELECT COUNT(*) AS n FROM m", name="counted")
        session.replay(into="v2")
        assert session.query('SELECT * FROM "v2"."m"', name="t")["row_count"] == 2


# --------------------------------------------------------------------------- #
# Unit-level checks that need no session
# --------------------------------------------------------------------------- #
class TestResolveRequest:
    def _conn(self):
        return apifetch.ApiConnection(base_url="https://api.test/v3")

    def test_base_path_is_preserved_when_joining(self):
        spec, _ = apifetch.resolve_request(
            self._conn(), apifetch.ApiRequest(path="/movie/popular")
        )
        # NOT urljoin: that would drop /v3 and hit the host root
        assert spec.url == "https://api.test/v3/movie/popular"

    def test_path_values_are_reported_for_stamping(self):
        _spec_, values = apifetch.resolve_request(
            self._conn(), apifetch.ApiRequest(path="/movie/{id}", params={"id": 550})
        )
        assert values == {"id": "550"}

    def test_placeholders_in_ignores_the_query_string(self):
        assert apifetch.placeholders_in("/a/{x}/b?q={y}") == ["x"]


class TestHostLimiter:
    def test_penalty_applies_to_every_caller(self):
        limiter = apifetch.HostLimiter()
        limiter.penalize(0.05)
        import time as _time
        t0 = _time.monotonic()
        limiter.acquire()
        assert _time.monotonic() - t0 >= 0.04
