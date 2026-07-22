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

    def test_no_import_remote_tool(self, mcp_server):
        names = {t.name for t in _run(mcp_server.list_tools())}
        assert "import_remote" not in names  # removed: DuckDB-only, no SQLAlchemy fallback

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
        # guard.assert_read_only raises UnsafeSQLError; FastMCP re-wraps it as ToolError, so we
        # assert that specific type plus the guard's message rather than a bare Exception.
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="not a SELECT"):
            _run(mcp_server.call_tool("query", {"sql": "DELETE FROM orders", "name": "x"}))

    def test_batch_steps_pipeline_in_one_call(self, mcp_server):
        res = _run(mcp_server.call_tool("query", {"steps": [
            {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"},
            {"sql": "SELECT COUNT(*) AS n FROM base", "name": "agg"},
        ]}))
        data = res.structured_content
        assert data["completed"] == 2
        assert [s["status"] for s in data["steps"]] == ["ok", "ok"]
        assert data["steps"][1]["sample"] == [[3]]
        # Batch-built results are ordinary saved results: reusable + lineage-recorded.
        lin = _run(mcp_server.call_tool("lineage", {"name": "agg"})).structured_content
        assert {n["name"] for n in lin["nodes"]} == {"base", "agg"}

    def test_sql_and_steps_are_mutually_exclusive(self, mcp_server):
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="not both"):
            _run(mcp_server.call_tool("query", {
                "sql": "SELECT 1", "name": "x",
                "steps": [{"sql": "SELECT 1", "name": "y"}],
            }))
        with pytest.raises(ToolError, match="single query needs both"):
            _run(mcp_server.call_tool("query", {}))


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

    def test_lineage_and_replay_registered(self, mcp_server):
        names = {t.name for t in _run(mcp_server.list_tools())}
        assert {"lineage", "replay"} <= names

    def test_lineage_then_replay_roundtrip(self, mcp_server):
        _run(mcp_server.call_tool("query", {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"}))
        _run(mcp_server.call_tool("query", {"sql": "SELECT id, name FROM base", "name": "top"}))
        lin = _run(mcp_server.call_tool("lineage", {"name": "top"})).structured_content
        assert {n["name"] for n in lin["nodes"]} == {"base", "top"}
        rep = _run(mcp_server.call_tool("replay", {"into": "copy"})).structured_content
        assert rep["order"] == ["base", "top"]
        cat = _run(mcp_server.call_tool("catalog", {"flow": "copy"})).structured_content
        assert {r["name"] for r in cat["results"]} == {"base", "top"}

    def test_lineage_tool_renders_nothing_by_default(self, mcp_server, tmp_path, monkeypatch):
        """The tool's defaults must stay opt-in — a bare lineage call writes no file."""
        _run(mcp_server.call_tool("query", {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"}))
        cwd = tmp_path / "cwd"  # empty: fixture files live in tmp_path itself
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        lin = _run(mcp_server.call_tool("lineage", {"name": "base"})).structured_content
        assert "mermaid" not in lin and "rendered_to" not in lin
        assert list(cwd.iterdir()) == []

    def test_lineage_render_mermaid_passthrough(self, mcp_server):
        _run(mcp_server.call_tool("query", {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"}))
        _run(mcp_server.call_tool("query", {"sql": "SELECT id FROM base", "name": "top"}))
        lin = _run(
            mcp_server.call_tool("lineage", {"name": "top", "render": "mermaid"})
        ).structured_content
        assert lin["mermaid"].startswith("flowchart TD")


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

    def test_batch_call_logs_steps_and_summary(self, sqlite_file, tmp_path):
        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([f"shop={sqlite_file}"])
        server = build_server(session, tool_log=str(log_path))
        try:
            _run(server.call_tool("query", {"steps": [
                {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"},
                {"sql": "SELECT COUNT(*) AS n FROM base", "name": "agg"},
            ]}))
        finally:
            session.close()

        rec = json.loads(log_path.read_text().splitlines()[-1])
        assert rec["outcome"] == "ok"
        # Full SQL of every step lands in the log (that's the point of it), JSON-clean.
        assert [s["name"] for s in rec["args"]["steps"]] == ["base", "agg"]
        assert rec["args"]["steps"][1]["sql"].startswith("SELECT COUNT")
        assert rec["result"]["step_count"] == 2 and rec["result"]["completed"] == 2

    def test_failed_call_logs_error_outcome(self, sqlite_file, tmp_path):
        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([f"shop={sqlite_file}"])
        server = build_server(session, tool_log=str(log_path))
        try:
            from fastmcp.exceptions import ToolError

            with pytest.raises(ToolError, match="not a SELECT"):
                _run(server.call_tool("query", {"sql": "DELETE FROM x", "name": "x"}))
        finally:
            session.close()

        rec = json.loads(log_path.read_text().splitlines()[-1])
        assert rec["tool"] == "query"
        assert rec["outcome"] == "error"
        assert rec["error"]


class TestDescriptionGating:
    def test_param_absent_without_flag(self, mcp_server):
        q = next(t for t in _run(mcp_server.list_tools()) if t.name == "query")
        assert "description" not in q.parameters["properties"]

    def test_param_present_with_flag(self, sqlite_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            srv = build_server(session, require_descriptions=True)
            q = next(t for t in _run(srv.list_tools()) if t.name == "query")
            assert "description" in q.parameters["properties"]
        finally:
            session.close()

    def test_single_query_stores_description(self, sqlite_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        srv = build_server(session, require_descriptions=True)
        try:
            _run(srv.call_tool("query", {
                "sql": 'SELECT * FROM "shop"."customers"', "name": "c",
                "description": "Every customer in the shop.",
            }))
            lin = _run(srv.call_tool("lineage", {"name": "c"})).structured_content
            assert lin["nodes"][0]["description"] == "Every customer in the shop."
        finally:
            session.close()

    def test_single_query_without_description_rejected(self, sqlite_file):
        from fastmcp.exceptions import ToolError

        session = DuckSession.open([f"shop={sqlite_file}"])
        srv = build_server(session, require_descriptions=True)
        try:
            with pytest.raises(ToolError, match="require-descriptions"):
                _run(srv.call_tool("query", {"sql": "SELECT 1 AS x", "name": "x"}))
        finally:
            session.close()

    def test_batch_step_missing_description_rejected(self, sqlite_file):
        from pydantic import ValidationError

        session = DuckSession.open([f"shop={sqlite_file}"])
        srv = build_server(session, require_descriptions=True)
        try:
            # `description` is a required field of the step model, so a step that omits it fails
            # FastMCP's argument-schema validation before the tool body ever runs.
            with pytest.raises(ValidationError, match="description"):
                _run(srv.call_tool("query", {"steps": [{"sql": "SELECT 1 AS x", "name": "x"}]}))
        finally:
            session.close()

    def test_top_level_description_with_steps_rejected(self, sqlite_file):
        from fastmcp.exceptions import ToolError

        session = DuckSession.open([f"shop={sqlite_file}"])
        srv = build_server(session, require_descriptions=True)
        try:
            # A top-level description has nothing to attach to in batch mode. Say so instead of
            # dropping it — otherwise the step's own description silently wins and the caller
            # never learns theirs was discarded.
            with pytest.raises(ToolError, match="description on each step"):
                _run(srv.call_tool("query", {
                    "description": "Counts the customers.",
                    "steps": [{"sql": "SELECT 1 AS x", "name": "x", "description": "One row."}],
                }))
        finally:
            session.close()

    def test_batch_step_blank_description_rejected(self, sqlite_file):
        from fastmcp.exceptions import ToolError

        session = DuckSession.open([f"shop={sqlite_file}"])
        srv = build_server(session, require_descriptions=True)
        try:
            with pytest.raises(ToolError, match="require-descriptions"):
                _run(srv.call_tool("query", {"steps": [
                    {"sql": "SELECT 1 AS x", "name": "x", "description": "  "},
                ]}))
        finally:
            session.close()


class TestAddSourceGating:
    def test_tools_absent_without_flag(self, mcp_server):
        names = {t.name for t in _run(mcp_server.list_tools())}
        assert "add_source" not in names and "remove_source" not in names

    def test_tools_present_with_flag(self, sqlite_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            names = {t.name for t in _run(build_server(session, allow_add_source=True).list_tools())}
            assert {"add_source", "remove_source"} <= names
        finally:
            session.close()

    def test_add_then_query_then_remove(self, sqlite_file, parquet_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        server = build_server(session, allow_add_source=True)
        try:
            added = _run(server.call_tool("add_source", {"spec": f"regions={parquet_file}"})).structured_content
            assert added["name"] == "regions"
            data = _run(server.call_tool("query", {"sql": "SELECT * FROM regions", "name": "r"})).structured_content
            assert data["row_count"] == 2
            removed = _run(server.call_tool("remove_source", {"name": "regions"})).structured_content
            assert removed["removed"] is True
        finally:
            session.close()
