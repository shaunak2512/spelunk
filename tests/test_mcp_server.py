"""Tests for spelunk.mcp.server — offline, no server spin-up.

Builds a FastMCP instance via build_server(DuckSession) over a SQLite source ('shop') and a
CSV file source ('orders'), then drives the tools/resources in-process via
mcp.call_tool / mcp.read_resource.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from spelunk.core.duck import DuckSession
from spelunk.mcp.server import build_server


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mcp_server(sqlite_file, csv_file):
    session = DuckSession.open([f"shop={sqlite_file}", f"orders={csv_file}"])
    yield build_server(session)
    session.close()


class TestRegistration:
    def test_five_core_tools_registered(self, mcp_server):
        names = {t.name for t in _run(mcp_server.list_tools())}
        assert {"query", "profile", "export", "catalog", "drop"} <= names

    def test_no_import_remote_without_fallback(self, mcp_server):
        names = {t.name for t in _run(mcp_server.list_tools())}
        assert "import_remote" not in names  # no SQL-Server source configured

    def test_resources_registered(self, mcp_server):
        uris = [str(r.uri) for r in _run(mcp_server.list_resources())]
        templates = [t.uri_template for t in _run(mcp_server.list_resource_templates())]
        assert any("tables" in u for u in uris)
        assert any("{table}" in t for t in templates)


class TestResources:
    def test_list_tables_spans_sources(self, mcp_server):
        data = json.loads(_run(mcp_server.read_resource("db://tables")).contents[0].content)
        names = {row["name"] for row in data}
        assert "shop.customers" in names
        assert "orders" in names

    def test_describe_qualified_table(self, mcp_server):
        data = json.loads(_run(mcp_server.read_resource("db://shop.customers")).contents[0].content)
        col_names = [c["name"] for c in data["columns"]]
        assert "id" in col_names and "name" in col_names
        assert "id" in data["primary_key"]
        assert len(data["sample_rows"]) > 0


class TestQueryTool:
    def test_query_materializes_and_samples(self, mcp_server):
        res = _run(mcp_server.call_tool("query", {"sql": "SELECT * FROM \"shop\".\"customers\"", "name": "c"}))
        data = res.structured_content
        assert data["row_count"] == 3
        assert data["name"] == "c"
        assert len(data["sample"]) == 3

    def test_cross_source_join(self, mcp_server):
        sql = (
            'SELECT c.name, o.amount FROM "shop"."customers" c '
            "JOIN orders o ON c.id = o.customer_id"
        )
        data = _run(mcp_server.call_tool("query", {"sql": sql, "name": "joined"})).structured_content
        assert data["row_count"] == 3

    def test_unsafe_write_rejected(self, mcp_server):
        from spelunk.core.types import UnsafeSQLError

        with pytest.raises((UnsafeSQLError, Exception)):
            _run(mcp_server.call_tool("query", {"sql": "DELETE FROM orders", "name": "x"}))


class TestOtherTools:
    def test_profile(self, mcp_server):
        _run(mcp_server.call_tool("query", {"sql": "SELECT * FROM orders", "name": "o"}))
        data = _run(mcp_server.call_tool("profile", {"sql": "SELECT * FROM o"})).structured_content
        assert "amount" in data["columns"]
        assert data["columns"]["amount"]["min"] == 75.0

    def test_catalog_and_drop(self, mcp_server):
        _run(mcp_server.call_tool("query", {"sql": "SELECT 1 AS a", "name": "r1", "flow": "w"}))
        cat = _run(mcp_server.call_tool("catalog", {"flow": "w"})).structured_content
        assert [r["name"] for r in cat["results"]] == ["r1"]
        dropped = _run(mcp_server.call_tool("drop", {"flow": "w"})).structured_content
        assert dropped["dropped_results"] == 1

    def test_export(self, mcp_server, tmp_path):
        _run(mcp_server.call_tool("query", {"sql": "SELECT * FROM orders", "name": "o"}))
        out = str(tmp_path / "o.parquet")
        res = _run(mcp_server.call_tool("export", {"target": "o", "format": "parquet", "path": out})).structured_content
        assert res["row_count"] == 3


class TestToolLogging:
    def test_each_call_logs_one_json_line(self, sqlite_file, csv_file, tmp_path):
        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([f"shop={sqlite_file}", f"orders={csv_file}"])
        server = build_server(session, tool_log=str(log_path))
        try:
            _run(server.call_tool("query", {"sql": "SELECT * FROM orders", "name": "o"}))
            _run(server.call_tool("catalog", {}))
        finally:
            session.close()

        lines = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert [r["tool"] for r in lines] == ["query", "catalog"]
        q = lines[0]
        assert q["outcome"] == "ok"
        assert q["args"]["name"] == "o"
        assert q["result"]["row_count"] == 3
        assert isinstance(q["duration_ms"], (int, float))

    def test_failed_call_logs_error_outcome(self, sqlite_file, tmp_path):
        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([f"shop={sqlite_file}"])
        server = build_server(session, tool_log=str(log_path))
        try:
            with pytest.raises(Exception):
                _run(server.call_tool("query", {"sql": "DELETE FROM x", "name": "x"}))
        finally:
            session.close()

        rec = json.loads(log_path.read_text().splitlines()[-1])
        assert rec["tool"] == "query"
        assert rec["outcome"] == "error"
        assert rec["error"]


class TestFallbackToolGating:
    def test_import_remote_registered_with_fallback(self, sqlite_file):
        # An mssql:// DSN can't actually connect, but build_source defers the connect to a
        # real engine; use monkeypatched fallback instead: a sqlite engine tagged fallback.
        from spelunk.core import sources
        from spelunk.core.connection import connect

        src = sources.Source(name="remote", kind="fallback", locator="x", engine=connect(sqlite_file_dsn(sqlite_file)))
        session = DuckSession.open([f"shop={sqlite_file}"])
        session.sources.append(src)
        try:
            names = {t.name for t in _run(build_server(session).list_tools())}
            assert "import_remote" in names
        finally:
            session.close()


def sqlite_file_dsn(path: str) -> str:
    return f"sqlite:///{path.replace(chr(92), '/')}"
