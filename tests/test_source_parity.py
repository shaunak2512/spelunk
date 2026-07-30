"""What a source puts ON THE WIRE, what it reports about itself, and whether the two code
paths agree.

Claims API-015 (an unrecognised paging param is never sent — some APIs reject the request
outright), API-023 (the fetch fingerprint is reported as Source.info), CONN-022 (api: and
fetch share ONE implementation, the architectural justification for the connection layer),
and SRC-024 (a glob is a valid file source — used in practice, documented nowhere).
"""
from __future__ import annotations

import json

import duckdb
import pytest

from spelunk.core.duck import DuckSession

PAGING_VOCABULARY = {
    "page", "offset", "limit", "per_page", "page_size", "size", "start", "cursor",
    "starting_after", "skip", "top", "startIndex", "resultsPerPage", "startAt", "maxResults",
}


@pytest.fixture
def session(tmp_path):
    s = DuckSession.open([], session_dir=str(tmp_path / "ws"))
    yield s
    s.close()


class TestNoUninvitedPagingParams:
    """API-015: sending a paging param the endpoint never declared is not harmless.

    The docs give this as the REASON to configure param names, but nothing checked what
    actually goes on the wire.
    """

    def test_a_non_paginating_source_sends_no_paging_params(self, session, api):
        api.handlers["/rows"] = lambda nth, query: (200, [{"id": 1}, {"id": 2}], {})
        session.add_source(f"rows=api:{api.base}/rows")

        assert len(api.calls) == 1
        sent = set(api.calls[0]["query"])
        assert not (sent & PAGING_VOCABULARY), f"uninvited paging params: {sent & PAGING_VOCABULARY}"
        assert sent == set(), f"unexpected query params: {sent}"

    def test_a_strict_api_that_rejects_unknown_params_still_works(self, session, api):
        """The failure this claim exists to prevent, made concrete: a 400 on any unknown key."""

        def strict(nth, query):
            unknown = set(query) - {"known"}
            if unknown:
                return 400, {"error": f"unknown parameters: {sorted(unknown)}"}, {}
            return 200, [{"id": nth}], {}

        api.handlers["/strict"] = strict
        session.add_source(f"strict=api:{api.base}/strict?known=1")
        out = session.query("SELECT COUNT(*) AS n FROM strict", "n")
        assert out["sample"] == [[1]]

    def test_only_the_configured_names_are_sent(self, session, api):
        """With a custom vocabulary, the DEFAULT names must not tag along."""
        rows = [{"id": i} for i in range(5)]

        def paged(nth, query):
            # A real offset endpoint speaking Jira's vocabulary, and NOTHING else.
            offset = int(query.get("startIndex", "0"))
            size = int(query.get("resultsPerPage", "2"))
            return 200, rows[offset:offset + size], {}

        api.handlers["/jira"] = paged
        session.add_source(
            f"jira=api:{api.base}/jira paginate=offset offset_param=startIndex "
            "size_param=resultsPerPage page_size=2"
        )

        for call in api.calls:
            sent = set(call["query"])
            assert sent <= {"startIndex", "resultsPerPage"}, f"extra params sent: {sent}"
            assert "offset" not in sent and "limit" not in sent and "page" not in sent


class TestFetchFingerprint:
    """API-023: url / fetched_at / pages / row_count come back as Source.info.

    The only record of WHEN a snapshot was taken — load-bearing for interpreting any analysis
    built on it, and previously reported by nothing.
    """

    def test_a_single_page_source_reports_its_provenance(self, session, api):
        api.handlers["/rows"] = lambda nth, query: (200, [{"id": 1}, {"id": 2}, {"id": 3}], {})
        session.add_source(f"rows=api:{api.base}/rows")

        source = next(s for s in session.sources if s.name == "rows")
        info = source.info or {}
        assert info.get("url", "").startswith(api.base)
        assert info.get("row_count") == 3
        assert info.get("pages") == 1
        assert info.get("fetched_at"), "no fetch timestamp recorded"

    def test_a_paginated_source_reports_the_page_count(self, session, api):
        pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}], 3: []}
        api.handlers["/paged"] = lambda nth, query: (
            200, pages.get(int(query.get("page", "1")), []), {}
        )
        session.add_source(f"paged=api:{api.base}/paged paginate=page")

        info = next(s for s in session.sources if s.name == "paged").info or {}
        assert info.get("row_count") == 3
        assert info.get("pages") >= 2, f"page count not recorded: {info}"

    def test_the_fingerprint_carries_no_credential(self, session, api, monkeypatch):
        monkeypatch.setenv("FP_TOKEN", "fingerprint-secret-value")
        api.handlers["/rows"] = lambda nth, query: (200, [{"id": 1}], {})
        session.add_source(f"rows=api:{api.base}/rows auth_env=FP_TOKEN")
        info = next(s for s in session.sources if s.name == "rows").info or {}
        assert "fingerprint-secret-value" not in json.dumps(info, default=str)


class TestApiAndFetchAgree:
    """CONN-022: resolve_request binds connection+request into the SAME ApiSpec, so pagination,
    auth and record extraction have ONE implementation.

    That shared implementation is the whole architectural argument for the connection layer,
    and nothing cross-checked the two paths. Same server, same endpoint, both routes.
    """

    def _spec(self, base_url: str) -> dict:
        return {
            "openapi": "3.0.0",
            "servers": [{"url": base_url}],
            "paths": {
                "/items": {
                    "get": {
                        "parameters": [
                            {"name": "page", "in": "query", "schema": {"type": "integer"}}
                        ],
                        "responses": {"200": {"content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {
                                "page": {"type": "integer"},
                                "results": {"type": "array", "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "name": {"type": "string"},
                                    },
                                }},
                            },
                        }}}}},
                    }
                }
            },
        }

    @staticmethod
    def _paged(nth, query):
        pages = {
            1: {"page": 1, "results": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]},
            2: {"page": 2, "results": [{"id": 3, "name": "c"}]},
            3: {"page": 3, "results": []},
        }
        return 200, pages.get(int(query.get("page", "1")), {"results": []}), {}

    def test_both_paths_return_the_same_rows(self, session, api, tmp_path):
        api.handlers["/items"] = self._paged

        # Path A: an api: source, paginated + record-extracted by the fetcher.
        session.add_source(
            f"viaapi=api:{api.base}/items paginate=page records=results"
        )
        via_source = session.query(
            "SELECT id, name FROM viaapi ORDER BY id", "via_source"
        )

        # Path B: the same endpoint through a connection + the fetch tool.
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(self._spec(api.base)), encoding="utf-8")
        session.add_source(f"conn=openapi:{spec_file}")
        session.fetch(
            source="conn", path="/items", name="raw",
            paginate="page", records="results",
        )
        via_fetch = session.query("SELECT id, name FROM raw ORDER BY id", "via_fetch")

        assert via_source["row_count"] == via_fetch["row_count"] == 3
        assert via_source["sample"] == via_fetch["sample"]

    def test_both_paths_send_the_same_paging_params(self, session, api, tmp_path):
        api.handlers["/items"] = self._paged
        session.add_source(f"viaapi=api:{api.base}/items paginate=page records=results")
        source_calls = [dict(c["query"]) for c in api.calls if c["path"] == "/items"]

        api.calls.clear()
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(self._spec(api.base)), encoding="utf-8")
        session.add_source(f"conn=openapi:{spec_file}")
        session.fetch(
            source="conn", path="/items", name="raw", paginate="page", records="results"
        )
        fetch_calls = [dict(c["query"]) for c in api.calls if c["path"] == "/items"]

        assert source_calls == fetch_calls, "the two paths page differently"


class TestGlobSource:
    """SRC-024: a glob is one view over many files. Used in practice (.mcp.json, the taxi
    experiment), documented nowhere, tested nowhere — so nothing pins it."""

    @pytest.fixture
    def parts(self, tmp_path) -> str:
        directory = tmp_path / "parts"
        directory.mkdir()
        con = duckdb.connect()
        for i in range(3):
            path = str(directory / f"part-{i}.parquet").replace("\\", "/")
            con.execute(
                f"COPY (SELECT {i} AS part, i AS n FROM range(4) t(i)) TO '{path}' (FORMAT PARQUET)"
            )
        con.close()
        return str(directory).replace("\\", "/")

    def test_a_glob_attaches_every_matching_file_as_one_view(self, session, parts):
        session.add_source(f"parts={parts}/*.parquet")
        out = session.query(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT part) AS files FROM parts", "n"
        )
        assert out["sample"] == [[12, 3]]

    def test_the_glob_view_is_queryable_like_any_file_source(self, session, parts):
        session.add_source(f"parts={parts}/*.parquet")
        names = {o.name for o in session.list_objects()}
        assert "parts" in names
        grouped = session.query(
            "SELECT part, SUM(n) AS total FROM parts GROUP BY part ORDER BY part", "by_part"
        )
        assert grouped["sample"] == [[0, 6], [1, 6], [2, 6]]

    def test_a_glob_matching_nothing_fails_loudly(self, session, parts):
        with pytest.raises(Exception):
            session.add_source(f"empty={parts}/*.csv")
