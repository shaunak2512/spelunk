"""Tests for the ``api:`` source kind — spec parsing, fetching/pagination, and session wiring.

Every HTTP test runs against a local threaded mock server (no network), covering all four
pagination styles, retries/backoff, auth headers, and the failure modes. A separate live
smoke-check against real public APIs lives in tests/live_api_check.py (not collected here —
network tests don't belong in the suite).
"""
from __future__ import annotations

import json

import pytest

from spelunk.core import apifetch, sources
from spelunk.core.apifetch import ApiSpec, fetch_snapshot, parse_api_spec, sql_to_odata_filter
from spelunk.core.duck import DuckSession

# The mock API server (`api` fixture) lives in conftest.py — it is shared with test_fetch.py.


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

    def test_odata_alias_normalizes_to_cursor(self):
        spec = parse_api_spec("https://x.test/odata/Orders paginate=odata")
        assert spec.paginate == "cursor"
        assert spec.cursor_path == "@odata.nextLink"
        assert spec.records == "value"

    def test_odata_alias_explicit_overrides_win(self):
        # An OData v2 service: d-wrapped results, __next link.
        spec = parse_api_spec(
            "https://x.test/svc paginate=odata records=d.results cursor_path=d.__next"
        )
        assert spec.records == "d.results"
        assert spec.cursor_path == "d.__next"

    def test_filter_select_require_odata(self):
        with pytest.raises(ValueError, match="paginate=odata"):
            parse_api_spec('https://x.test/a filter="a > 1"')
        with pytest.raises(ValueError, match="paginate=odata"):
            parse_api_spec("https://x.test/a select=Id")

    def test_filter_and_select_injected_into_url(self):
        spec = parse_api_spec(
            'https://x.test/Orders paginate=odata select=OrderID,Freight '
            'filter="Freight > 500 AND ShipCountry = \'Germany\'"'
        )
        from urllib.parse import parse_qs, urlsplit

        assert "%20" in spec.url  # spaces are %20-encoded, never '+'
        q = parse_qs(urlsplit(spec.url).query)
        assert q["$filter"] == ["Freight gt 500 and ShipCountry eq 'Germany'"]
        assert q["$select"] == ["OrderID,Freight"]

    def test_select_rejects_garbage(self):
        with pytest.raises(ValueError, match="comma-separated column list"):
            parse_api_spec('https://x.test/a paginate=odata select="Id; DROP TABLE x"')

    def test_keyset_requires_field_and_param(self):
        with pytest.raises(ValueError, match="keyset_field"):
            parse_api_spec("https://x.test/a paginate=keyset cursor_param=after")
        with pytest.raises(ValueError, match="cursor_param"):
            parse_api_spec("https://x.test/a paginate=keyset keyset_field=id")

    def test_header_and_param_options(self):
        spec = parse_api_spec(
            "https://x.test/a header=X-Api-Key:KEY1 header=X-Trace:KEY2 param=api_key:KEY3"
        )
        assert spec.extra_headers == [("X-Api-Key", "KEY1"), ("X-Trace", "KEY2")]
        assert spec.url_params == [("api_key", "KEY3")]

    def test_header_malformed(self):
        with pytest.raises(ValueError, match=r"header= takes <name>:<ENV_VAR>"):
            parse_api_spec("https://x.test/a header=X-Api-Key")

    def test_param_malformed(self):
        with pytest.raises(ValueError, match=r"param= takes <name>:<ENV_VAR>"):
            parse_api_spec("https://x.test/a param=:KEY")

    def test_unknown_option_lists_header_and_param(self):
        with pytest.raises(ValueError, match=r"Valid options: .*header.*param"):
            parse_api_spec("https://x.test/a nope=1")

    def test_detect_kind(self):
        assert sources.detect_kind("api:https://x.test/a") == "api"


# --------------------------------------------------------------------------- #
# SQL -> OData $filter translation
# --------------------------------------------------------------------------- #
class TestSqlToODataFilter:
    @pytest.mark.parametrize(
        "sql,odata",
        [
            ("Freight > 500", "Freight gt 500"),
            ("a >= 1 AND b < 2", "a ge 1 and b lt 2"),
            ("a = 'x' OR b != 3", "a eq 'x' or b ne 3"),
            ("(a = 1 OR b = 2) AND c = 3", "(a eq 1 or b eq 2) and c eq 3"),
            ("NOT (a = 1)", "not (a eq 1)"),
            ("name = 'O''Brien'", "name eq 'O''Brien'"),
            ("city IN ('Berlin', 'Paris')", "(city eq 'Berlin' or city eq 'Paris')"),
            ("name LIKE '%duck%'", "contains(name, 'duck')"),
            ("name LIKE 'duck%'", "startswith(name, 'duck')"),
            ("name LIKE '%duck'", "endswith(name, 'duck')"),
            ("name LIKE 'duck'", "name eq 'duck'"),
            ("name ILIKE '%Duck%'", "contains(tolower(name), 'duck')"),
            ("a IS NULL", "a eq null"),
            ("a IS NOT NULL", "not (a eq null)"),
            ("active = true", "active eq true"),
            ("delta > -5", "delta gt -5"),
        ],
    )
    def test_translations(self, sql, odata):
        assert sql_to_odata_filter(sql) == odata

    @pytest.mark.parametrize(
        "sql,reason",
        [
            ("name LIKE '%a%b%'", "mid-string wildcard"),
            ("name LIKE 'a_b'", "'_' wildcards"),
            ("LENGTH(name) > 3", "cannot translate"),
            ("t.name = 'x'", "qualified column"),
            ("a = (SELECT 1)", "cannot translate"),
            ("a + 1 > 2", "cannot translate"),
            ("SELECT 1", "cannot translate"),
        ],
    )
    def test_rejections_are_loud(self, sql, reason):
        with pytest.raises(ValueError, match=reason):
            sql_to_odata_filter(sql)


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

    def test_records_autodetect_odata_value_key(self, api, tmp_path):
        api.handlers["/w"] = lambda n, q: (200, {"@odata.context": "x", "value": _rows(3)}, {})
        dest = str(tmp_path / "s.ndjson")
        assert fetch_snapshot(ApiSpec(url=f"{api.base}/w"), dest)["row_count"] == 3

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

    def test_odata_v4_absolute_nextlink(self, api, tmp_path):
        # The literal dotted key "@odata.nextLink" must resolve (literal-first _dig).
        def handler(n, q):
            if q.get("p2") is None:
                return (200, {"value": _rows(2, 0), "@odata.nextLink": f"{api.base}/o?p2=1"}, {})
            return (200, {"value": _rows(2, 2)}, {})

        api.handlers["/o"] = handler
        spec = parse_api_spec(f"{api.base}/o paginate=odata")
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(spec, dest)
        assert info["row_count"] == 4
        assert info["pages"] == 2
        assert [r["id"] for r in _read_snapshot(dest)] == [0, 1, 2, 3]

    def test_odata_relative_nextlink_resolved(self, api, tmp_path):
        # OData permits RELATIVE @odata.nextLink values — resolve against the request URL.
        def handler(n, q):
            if q.get("skiptoken") is None:
                return (200, {"value": _rows(1, 0), "@odata.nextLink": "o?skiptoken=t1"}, {})
            return (200, {"value": _rows(1, 1)}, {})

        api.handlers["/o"] = handler
        spec = parse_api_spec(f"{api.base}/o paginate=odata")
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 2
        assert api.calls[1]["query"]["skiptoken"] == "t1"

    def test_odata_filter_reaches_server_decoded(self, api, tmp_path):
        api.handlers["/Orders"] = lambda n, q: (200, {"value": _rows(2)}, {})
        spec = parse_api_spec(
            f'{api.base}/Orders paginate=odata select=id filter="id > 0 AND name != \'x\'"'
        )
        fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert api.calls[0]["query"]["$filter"] == "id gt 0 and name ne 'x'"
        assert api.calls[0]["query"]["$select"] == "id"

    def test_odata_v2_shape(self, api, tmp_path):
        # v2: {"d": {"results": [...], "__next": url}} — real nesting, explicit overrides.
        def handler(n, q):
            if q.get("page2") is None:
                return (200, {"d": {"results": _rows(2, 0), "__next": f"{api.base}/v2?page2=1"}}, {})
            return (200, {"d": {"results": _rows(1, 2)}}, {})

        api.handlers["/v2"] = handler
        spec = parse_api_spec(
            f"{api.base}/v2 paginate=odata records=d.results cursor_path=d.__next"
        )
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 3

    def test_dig_literal_key_beats_nesting(self, tmp_path, api):
        # A record with BOTH a literal dotted key and equivalent nesting: literal wins.
        payload = {"a.b": [{"x": 1}], "a": {"b": [{"x": 2}]}}
        api.handlers["/d"] = lambda n, q: (200, payload, {})
        dest = str(tmp_path / "s.ndjson")
        fetch_snapshot(ApiSpec(url=f"{api.base}/d", records="a.b"), dest)
        assert _read_snapshot(dest) == [{"x": 1}]

    def test_cursor_opaque_without_cursor_param_errors(self, api, tmp_path):
        api.handlers["/c"] = lambda n, q: (200, {"items": _rows(1), "next": "tok"}, {})
        spec = ApiSpec(url=f"{api.base}/c", paginate="cursor", cursor_path="next")
        with pytest.raises(ValueError, match="cursor_param"):
            fetch_snapshot(spec, str(tmp_path / "s.ndjson"))

    def test_keyset_stripe_style(self, api, tmp_path):
        # Stripe convention: starting_after=<last id>, EXCLUSIVE of that id.
        total = _rows(5)

        def handler(n, q):
            after = int(q["starting_after"]) if "starting_after" in q else -1
            nxt = [r for r in total if r["id"] > after][:2]
            return (200, {"data": nxt, "has_more": bool(nxt)}, {})

        api.handlers["/k"] = handler
        spec = ApiSpec(
            url=f"{api.base}/k",
            records="data",
            paginate="keyset",
            keyset_field="id",
            cursor_param="starting_after",
        )
        dest = str(tmp_path / "s.ndjson")
        info = fetch_snapshot(spec, dest)
        assert [r["id"] for r in _read_snapshot(dest)] == [0, 1, 2, 3, 4]
        assert info["pages"] == 4  # 2+2+1, then the empty page that ends it
        assert [c["query"].get("starting_after") for c in api.calls] == [None, "1", "3", "4"]

    def test_keyset_nested_field_and_short_page_stop(self, api, tmp_path):
        # size_param set -> a short page ends the fetch without the extra empty-page request.
        def handler(n, q):
            batch = _rows(3, 0) if "after" not in q else _rows(2, 3)
            return (200, [{"meta": {"seq": r["id"]}, **r} for r in batch], {})

        api.handlers["/k"] = handler
        spec = ApiSpec(
            url=f"{api.base}/k",
            paginate="keyset",
            keyset_field="meta.seq",
            cursor_param="after",
            size_param="limit",
            page_size=3,
        )
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 5
        assert info["pages"] == 2
        assert api.calls[1]["query"]["after"] == "2"
        assert api.calls[0]["query"]["limit"] == "3"

    def test_keyset_stops_when_field_missing(self, api, tmp_path):
        api.handlers["/k"] = lambda n, q: (200, [{"name": "no-id-here"}], {})
        spec = ApiSpec(
            url=f"{api.base}/k", paginate="keyset", keyset_field="id", cursor_param="after"
        )
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert info["row_count"] == 1
        assert len(api.calls) == 1  # no way to seek onward -> single page

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

    def test_extra_header_from_env(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("SPELUNK_TEST_APIKEY", "k-123")
        api.handlers["/a"] = lambda n, q: (200, _rows(1), {})
        spec = ApiSpec(url=f"{api.base}/a", extra_headers=[("X-Api-Key", "SPELUNK_TEST_APIKEY")])
        fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert api.calls[0]["headers"]["x-api-key"] == "k-123"

    def test_query_param_from_env_persists_across_pages(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("SPELUNK_TEST_APIKEY", "k-456")
        api.handlers["/p"] = lambda n, q: (200, _rows(1) if n == 1 else [], {})
        spec = ApiSpec(
            url=f"{api.base}/p", paginate="page", url_params=[("api_key", "SPELUNK_TEST_APIKEY")]
        )
        info = fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert [c["query"]["api_key"] for c in api.calls] == ["k-456", "k-456"]
        assert info["url"] == f"{api.base}/p"  # fingerprint reports the ORIGINAL url, no secret

    def test_missing_env_for_header(self, api, tmp_path, monkeypatch):
        monkeypatch.delenv("SPELUNK_NO_SUCH_KEY", raising=False)
        spec = ApiSpec(url=f"{api.base}/a", extra_headers=[("X-Api-Key", "SPELUNK_NO_SUCH_KEY")])
        with pytest.raises(ValueError, match="SPELUNK_NO_SUCH_KEY"):
            fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert api.calls == []

    def test_secret_param_scrubbed_from_errors(self, api, tmp_path, monkeypatch):
        # A failing request's error text (which carries the URL) must show $ENV, not the value.
        monkeypatch.setenv("SPELUNK_TEST_APIKEY", "sup3r-sekret")
        api.handlers["/a"] = lambda n, q: (404, {"error": f"bad key {q['api_key']}"}, {})
        spec = ApiSpec(url=f"{api.base}/a", url_params=[("api_key", "SPELUNK_TEST_APIKEY")])
        with pytest.raises(ValueError) as excinfo:
            fetch_snapshot(spec, str(tmp_path / "s.ndjson"))
        assert "sup3r-sekret" not in str(excinfo.value)
        assert "$SPELUNK_TEST_APIKEY" in str(excinfo.value)

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
