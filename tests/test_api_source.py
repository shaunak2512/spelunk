"""Tests for the ``api:`` source kind — spec parsing, fetching/pagination, and session wiring.

Every HTTP test runs against a local threaded mock server (no network), covering all four
pagination styles, retries/backoff, auth headers, and the failure modes. A separate live
smoke-check against real public APIs lives in tests/live_api_check.py (not collected here —
network tests don't belong in the suite).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from spelunk.core import apifetch, sources
from spelunk.core.apifetch import ApiSpec, fetch_snapshot, parse_api_spec
from spelunk.core.duck import DuckSession


# --------------------------------------------------------------------------- #
# Mock API server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parts = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        srv = self.server
        srv.calls.append(
            {
                "path": parts.path,
                "query": query,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        handler = srv.handlers.get(parts.path)
        if handler is None:
            self._send(404, {"error": f"no route {parts.path}"}, {})
            return
        nth = sum(1 for c in srv.calls if c["path"] == parts.path)  # 1-based, per path
        status, payload, extra = handler(nth, query)
        self._send(status, payload, extra)

    def _send(self, status, payload, extra_headers):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, val in extra_headers.items():
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


@pytest.fixture()
def api():
    """A local mock API: set ``api.handlers[path] = fn(nth_call, query) -> (status, payload,
    extra_headers)``; requests are logged to ``api.calls``. ``api.base`` is the URL root."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.calls = []
    srv.handlers = {}
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _rows(n, start=0):
    return [{"id": i, "name": f"row{i}"} for i in range(start, start + n)]


def _read_snapshot(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


# --------------------------------------------------------------------------- #
# Spec parsing
# --------------------------------------------------------------------------- #
class TestParseApiSpec:
    def test_url_only(self):
        spec = parse_api_spec("https://example.com/data")
        assert spec.url == "https://example.com/data"
        assert spec.paginate == "none"

    def test_options(self):
        spec = parse_api_spec(
            "https://x.test/v1/items records=data.items paginate=page page_param=p "
            "start=0 size_param=per_page page_size=50 max_pages=5 max_rows=200 auth_env=TOK"
        )
        assert spec.records == "data.items"
        assert spec.paginate == "page"
        assert spec.page_param == "p"
        assert spec.start == 0
        assert spec.size_param == "per_page"
        assert spec.page_size == 50
        assert spec.max_pages == 5
        assert spec.max_rows == 200
        assert spec.auth_env == "TOK"

    def test_not_http_url(self):
        with pytest.raises(ValueError, match="http"):
            parse_api_spec("ftp://example.com/x")

    def test_unknown_option(self):
        with pytest.raises(ValueError, match="Unknown api: option 'nope'"):
            parse_api_spec("https://x.test/a nope=1")

    def test_malformed_option(self):
        with pytest.raises(ValueError, match="Malformed"):
            parse_api_spec("https://x.test/a paginate")

    def test_non_integer(self):
        with pytest.raises(ValueError, match="max_pages must be an integer"):
            parse_api_spec("https://x.test/a max_pages=lots")

    def test_bad_paginate_style(self):
        with pytest.raises(ValueError, match="paginate style"):
            parse_api_spec("https://x.test/a paginate=scroll")

    def test_cursor_requires_cursor_path(self):
        with pytest.raises(ValueError, match="cursor_path"):
            parse_api_spec("https://x.test/a paginate=cursor")

    def test_detect_kind(self):
        assert sources.detect_kind("api:https://x.test/a") == "api"


# --------------------------------------------------------------------------- #
# Fetching: records extraction + pagination styles
# --------------------------------------------------------------------------- #
class TestFetchBasics:
    def test_plain_array(self, api, tmp_path):
        api.handlers["/posts"] = lambda n, q: (200, _rows(3), {})
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/posts"), dest)
        assert info["row_count"] == 3
        assert info["pages"] == 1
        assert "truncated" not in info
        assert _read_snapshot(dest) == _rows(3)

    def test_records_dot_path(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"data": {"items": _rows(2)}}, {})
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/w", records="data.items"), dest)
        assert info["row_count"] == 2

    def test_records_autodetect_common_key(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"count": 2, "results": _rows(2)}, {})
        dest = str(tmp_path / "s.ndjson")
        assert fetch_snapshot(ApiSpec(url=f"{api.base}/w"), dest)["row_count"] == 2

    def test_records_autodetect_single_list_key(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"total": 2, "things": _rows(2)}, {})
        dest = str(tmp_path / "s.ndjson")
        assert fetch_snapshot(ApiSpec(url=f"{api.base}/w"), dest)["row_count"] == 2

    def test_single_object_response_is_one_record(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"latitude": 1.5, "longitude": 2.5}, {})
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/w"), dest)
        assert info["row_count"] == 1
        assert _read_snapshot(dest) == [{"latitude": 1.5, "longitude": 2.5}]

    def test_scalar_records_are_wrapped(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, ["a", "b"], {})
        dest = str(tmp_path / "s.ndjson")
        fetch_snapshot(ApiSpec(url=f"{api.base}/w"), dest)
        assert _read_snapshot(dest) == [{"value": "a"}, {"value": "b"}]

    def test_bad_records_path(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"stuff": _rows(1)}, {})
        with pytest.raises(ValueError, match=r"records path 'nope'.*'stuff'"):
            fetch_snapshot(ApiSpec(url=f"{api.base}/w", records="nope"), str(tmp_path / "s"))

    def test_zero_records_errors_with_keys(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"results": [], "info": {}}, {})
        dest = str(tmp_path / "s.ndjson")
        with pytest.raises(ValueError, match=r"no records.*'info', 'results'"):
            fetch_snapshot(ApiSpec(url=f"{api.base}/w"), dest)
        assert not (tmp_path / "s.ndjson").exists()
        assert not (tmp_path / "s.ndjson.tmp").exists()


class TestPagination:
    def test_page_until_empty(self, api, tmp_path):
        pages = {1: _rows(2, 0), 2: _rows(2, 2), 3: []}

        def handler(n, q):
            return (200, pages[int(q["page"])], {})

        api.handlers["/p"] = handler
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/p", paginate="page"), dest)
        assert info["row_count"] == 4
        assert [c["query"]["page"] for c in api.calls] == ["1", "2", "3"]
        assert [r["id"] for r in _read_snapshot(dest)] == [0, 1, 2, 3]

    def test_page_with_size_param(self, api, tmp_path):
        api.handlers["/p"] = lambda n, q: (200, _rows(1) if n == 1 else [], {})
        spec = ApiSpec(
            url=f"{api.base}/p", paginate="page", size_param="per_page", page_size=25
        )
        fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert api.calls[0]["query"]["per_page"] == "25"

    def test_page_repeat_guard(self, api, tmp_path):
        # An API that ignores its page param serves the same page forever — must stop early.
        api.handlers["/p"] = lambda n, q: (200, _rows(3), {})
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/p", paginate="page"), dest)
        assert info["row_count"] == 3
        assert info["pages"] == 1
        assert len(api.calls) == 2  # the repeat was fetched, detected, and discarded

    def test_offset(self, api, tmp_path):
        total = _rows(250)

        def handler(n, q):
            off, lim = int(q["offset"]), int(q["limit"])
            return (200, total[off : off + lim], {})

        api.handlers["/o"] = handler
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/o", paginate="offset"), dest)
        assert info["row_count"] == 250
        assert info["pages"] == 3
        assert [c["query"]["offset"] for c in api.calls] == ["0", "100", "200"]
        assert [r["id"] for r in _read_snapshot(dest)] == list(range(250))

    def test_cursor_opaque(self, api, tmp_path):
        def handler(n, q):
            cur = q.get("after")
            if cur is None:
                return (200, {"items": _rows(2, 0), "next": "c1"}, {})
            if cur == "c1":
                return (200, {"items": _rows(2, 2), "next": None}, {})
            raise AssertionError(f"unexpected cursor {cur}")

        api.handlers["/c"] = handler
        spec = ApiSpec(
            url=f"{api.base}/c", paginate="cursor", cursor_path="next", cursor_param="after"
        )
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 4
        assert info["pages"] == 2

    def test_cursor_full_url(self, api, tmp_path):
        # PokeAPI style: the response's `next` field is the whole next-page URL.
        def handler(n, q):
            if q.get("page2") is None:
                return (200, {"results": _rows(2, 0), "next": f"{api.base}/c?page2=1"}, {})
            return (200, {"results": _rows(1, 2), "next": None}, {})

        api.handlers["/c"] = handler
        spec = ApiSpec(url=f"{api.base}/c", paginate="cursor", cursor_path="next")
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 3

    def test_cursor_opaque_without_cursor_param_errors(self, api, tmp_path):
        api.handlers["/c"] = lambda n, q: (200, {"items": _rows(1), "next": "tok"}, {})
        spec = ApiSpec(url=f"{api.base}/c", paginate="cursor", cursor_path="next")
        with pytest.raises(ValueError, match="cursor_param"):
            fetch_snapshot(spec, str(tmp_path / "s.ndjson"))

    def test_link_header(self, api, tmp_path):
        def handler(n, q):
            if q.get("p") is None:
                nxt = f'<{api.base}/l?p=2>; rel="next", <{api.base}/l?p=9>; rel="last"'
                return (200, _rows(2, 0), {"Link": nxt})
            return (200, _rows(2, 2), {"Link": f'<{api.base}/l?p=1>; rel="prev"'})

        api.handlers["/l"] = handler
        info = fetch_snapshot(
            ApiSpec(url=f"{api.base}/l", paginate="link"), str(tmp_path / "s.ndjson")
        )
        assert info["row_count"] == 4
        assert info["pages"] == 2

    def test_404_mid_pagination_is_end_of_data(self, api, tmp_path):
        # TVMaze-style: pages past the end 404 instead of returning [] — the fetch must keep
        # the pages it has, not fail. (A 404 on the FIRST page is still an error.)
        api.handlers["/p"] = lambda n, q: (
            (200, _rows(2, 2 * (n - 1)), {}) if n <= 2 else (404, {"error": "no such page"}, {})
        )
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/p", paginate="page"), dest)
        assert info["row_count"] == 4
        assert info["pages"] == 2
        assert "truncated" not in info

    def test_max_pages_truncates(self, api, tmp_path):
        api.handlers["/p"] = lambda n, q: (200, _rows(2, 2 * (int(q["page"]) - 1)), {})
        spec = ApiSpec(url=f"{api.base}/p", paginate="page", max_pages=3)
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["pages"] == 3
        assert info["row_count"] == 6
        assert info["truncated"] is True

    def test_max_rows_truncates(self, api, tmp_path):
        api.handlers["/p"] = lambda n, q: (200, _rows(100), {})
        spec = ApiSpec(url=f"{api.base}/p", max_rows=7)
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 7
        assert info["truncated"] is True


# --------------------------------------------------------------------------- #
# Auth, retries, failure modes
# --------------------------------------------------------------------------- #
class TestAuthAndRetry:
    def test_auth_env_bearer(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("SPELUNK_TEST_TOKEN", "sekret")
        api.handlers["/a"] = lambda n, q: (200, _rows(1), {})
        spec = ApiSpec(url=f"{api.base}/a", auth_env="SPELUNK_TEST_TOKEN")
        fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert api.calls[0]["headers"]["authorization"] == "Bearer sekret"

    def test_auth_env_missing(self, api, tmp_path, monkeypatch):
        monkeypatch.delenv("SPELUNK_NO_SUCH_TOKEN", raising=False)
        spec = ApiSpec(url=f"{api.base}/a", auth_env="SPELUNK_NO_SUCH_TOKEN")
        with pytest.raises(ValueError, match="SPELUNK_NO_SUCH_TOKEN"):
            fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert api.calls == []  # failed before any request

    def test_user_agent_always_sent(self, api, tmp_path):
        api.handlers["/a"] = lambda n, q: (200, _rows(1), {})
        fetch_snapshot(ApiSpec(url=f"{api.base}/a"), str(tmp_path / "s.ndjson"))
        assert "spelunk" in api.calls[0]["headers"]["user-agent"]

    def test_retry_on_500_then_success(self, api, tmp_path, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(apifetch.time, "sleep", sleeps.append)
        api.handlers["/r"] = lambda n, q: (
            (500, {"error": "boom"}, {}) if n == 1 else (200, _rows(2), {})
        )
        info = fetch_snapshot(ApiSpec(url=f"{api.base}/r"), str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 2
        assert len(api.calls) == 2
        assert sleeps == [1.0]

    def test_retry_429_honours_retry_after(self, api, tmp_path, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(apifetch.time, "sleep", sleeps.append)
        api.handlers["/r"] = lambda n, q: (
            (429, {}, {"Retry-After": "7"}) if n == 1 else (200, _rows(1), {})
        )
        fetch_snapshot(ApiSpec(url=f"{api.base}/r"), str(tmp_path / "s.ndjson"))
        assert sleeps == [7.0]

    def test_persistent_500_gives_up(self, api, tmp_path, monkeypatch):
        monkeypatch.setattr(apifetch.time, "sleep", lambda s: None)
        api.handlers["/r"] = lambda n, q: (500, {"error": "boom"}, {})
        with pytest.raises(ValueError, match="after 3 attempts"):
            fetch_snapshot(ApiSpec(url=f"{api.base}/r"), str(tmp_path / "s.ndjson"))
        assert len(api.calls) == 3

    def test_404_no_retry(self, api, tmp_path):
        api.handlers["/r"] = lambda n, q: (404, {"error": "gone"}, {})
        with pytest.raises(ValueError, match="HTTP 404"):
            fetch_snapshot(ApiSpec(url=f"{api.base}/r"), str(tmp_path / "s.ndjson"))
        assert len(api.calls) == 1

    def test_non_json_response(self, api, tmp_path):
        api.handlers["/r"] = lambda n, q: (200, b"<html>hello</html>", {})
        with pytest.raises(ValueError, match="did not return JSON"):
            fetch_snapshot(ApiSpec(url=f"{api.base}/r"), str(tmp_path / "s.ndjson"))

    def test_failed_fetch_leaves_no_files(self, api, tmp_path):
        api.handlers["/r"] = lambda n, q: (404, {}, {})
        with pytest.raises(ValueError):
            fetch_snapshot(ApiSpec(url=f"{api.base}/r"), str(tmp_path / "s.ndjson"))
        assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# Session wiring: attach at open, add/remove/refresh at runtime, lineage
# --------------------------------------------------------------------------- #
class TestSessionIntegration:
    def test_open_with_api_source(self, api, tmp_path):
        api.handlers["/v1/orders"] = lambda n, q: (200, _rows(5), {})
        session = DuckSession.open(
            [f"orders=api:{api.base}/v1/orders"], session_dir=str(tmp_path / "sess")
        )
        try:
            assert [o.name for o in session.list_objects()] == ["orders"]
            out = session.query("SELECT COUNT(*) AS n FROM orders", name="cnt")
            assert out["sample"] == [[5]]
            # The view reads the local snapshot: no re-fetch per query.
            calls_before = len(api.calls)
            session.query("SELECT * FROM orders WHERE id > 2", name="tail")
            assert len(api.calls) == calls_before
            # Lineage records the api view as a source leaf.
            node = session.lineage("tail")["nodes"][0]
            assert node["sources"] == ["orders"]
            # Snapshot lives under the workspace's snapshots dir.
            snap = session.sources[0].info["snapshot"]
            assert snap.startswith(session.workspace_dir)
        finally:
            session.close()

    def test_add_remove_refresh_at_runtime(self, api, tmp_path):
        api.handlers["/data"] = lambda n, q: (200, _rows(2 if n == 1 else 4), {})
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            out = session.add_source(f"d=api:{api.base}/data")
            assert out["kind"] == "api"
            assert out["info"]["row_count"] == 2
            assert session.query("SELECT COUNT(*) AS n FROM d", name="c")["sample"] == [[2]]
            # Refresh = remove + re-add: the snapshot is re-fetched and the view replaced.
            session.remove_source("d")
            out = session.add_source(f"d=api:{api.base}/data")
            assert out["info"]["row_count"] == 4
            assert session.query("SELECT COUNT(*) AS n FROM d", name="c")["sample"] == [[4]]
        finally:
            session.close()

    def test_derived_name_from_url_path(self, api, tmp_path):
        api.handlers["/v2/pokemon"] = lambda n, q: (200, _rows(1), {})
        session = DuckSession.open([], session_dir=str(tmp_path / "sess"))
        try:
            out = session.add_source(f"api:{api.base}/v2/pokemon?limit=3")
            assert out["name"] == "pokemon"
        finally:
            session.close()

    def test_build_source_without_workspace_rejected(self):
        with pytest.raises(ValueError, match="workspace"):
            sources.build_source("api:https://example.com/x")
