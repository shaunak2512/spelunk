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


class TestConsoleLogging:
    """The human-readable stderr mirror — what makes `--transport http` legible in a terminal.

    Records are captured off the `fastmcp.spelunk` logger directly rather than through `caplog`:
    FastMCP sets `propagate = False` on the `fastmcp` logger, so nothing reaches pytest's root
    handler and caplog would see an empty list whether the feature worked or not.
    """

    @staticmethod
    def _sink():
        import logging

        records: list[logging.LogRecord] = []

        class _Sink(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Sink()
        logging.getLogger("fastmcp.spelunk").addHandler(handler)
        return records, handler

    @staticmethod
    def _detach(handler):
        import logging

        logging.getLogger("fastmcp.spelunk").removeHandler(handler)

    def test_silent_unless_enabled(self, sqlite_file):
        records, handler = self._sink()
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            srv = build_server(session)  # console_log defaults off, like tool_log
            _run(srv.call_tool("query", {"sql": "SELECT 1 AS x", "name": "x"}))
        finally:
            session.close()
            self._detach(handler)
        assert records == []

    def test_success_logs_one_info_line(self, sqlite_file):
        records, handler = self._sink()
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            srv = build_server(session, console_log=True)
            _run(srv.call_tool("query", {"sql": 'SELECT * FROM "shop"."customers"', "name": "c"}))
        finally:
            session.close()
            self._detach(handler)

        assert len(records) == 1
        assert records[0].levelname == "INFO"
        msg = records[0].getMessage()
        assert "query(name='c' flow='default')" in msg and "ok in" in msg and "rows" in msg

    def test_raised_error_logs_message_and_sql(self, sqlite_file):
        from fastmcp.exceptions import ToolError

        records, handler = self._sink()
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            srv = build_server(session, console_log=True)
            with pytest.raises(ToolError):
                _run(srv.call_tool("query", {"sql": "SELECT * FROM nope", "name": "x"}))
        finally:
            session.close()
            self._detach(handler)

        assert [r.levelname for r in records] == ["ERROR"]
        msg = records[0].getMessage()
        assert "failed in" in msg
        assert "nope" in msg  # the DuckDB message
        assert "sql: SELECT * FROM nope" in msg  # ...and the query that caused it

    def test_failed_batch_step_logs_error(self, sqlite_file):
        """The case that would otherwise be invisible: a batch fails fast but RETURNS normally,
        so nothing raises and neither uvicorn nor FastMCP reports anything gone wrong."""
        records, handler = self._sink()
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            srv = build_server(session, console_log=True)
            _run(srv.call_tool("query", {"steps": [
                {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"},
                {"sql": "SELECT * FROM missing_table", "name": "bad"},
                {"sql": "SELECT 1 AS z", "name": "never"},
            ]}))
        finally:
            session.close()
            self._detach(handler)

        assert [r.levelname for r in records] == ["ERROR"]
        msg = records[0].getMessage()
        assert "step 2/3 'bad' failed" in msg
        assert "missing_table" in msg
        assert "1 completed, 1 skipped" in msg

    def test_credentials_redacted_in_batch_step_error(self, tmp_path):
        """Same masking rule as the JSONL sink — an error path must not print what the arg path
        withholds. `fetch`/`add_source` errors quote the DSN straight back."""
        from spelunk.mcp.server import _console_report

        records, handler = self._sink()
        from spelunk.mcp import server as server_mod

        server_mod._configure_console_logging(True)
        try:
            _console_report(
                {"tool": "add_source", "args": {}, "outcome": "ok", "duration_ms": 1.0},
                {"failed_step": 0, "completed": 0, "steps": [
                    {"name": "s", "status": "failed",
                     "error": "could not connect: postgresql://bob:hunter2@db/x password=hunter2"},
                ]},
            )
        finally:
            server_mod._configure_console_logging(False)
            self._detach(handler)

        msg = records[0].getMessage()
        assert "hunter2" not in msg and "//***@" in msg and "password=***" in msg

    def test_auto_stands_down_when_tool_log_is_stderr(self):
        from spelunk.mcp.server import _resolve_console_log

        assert _resolve_console_log("auto", "/tmp/tool-calls.jsonl") is True
        assert _resolve_console_log("auto", None) is True
        assert _resolve_console_log("auto", "-") is False  # would double every call on stderr
        assert _resolve_console_log("on", "-") is True
        assert _resolve_console_log("off", "/tmp/tool-calls.jsonl") is False


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
        # Which exception type surfaces is FastMCP's business and it has changed: up to 3.4.2 the
        # raw pydantic ValidationError escaped; from 3.4.3 FastMCP wraps it in its own
        # ValidationError. `fastmcp` is pinned only as `>=`, so both are live in the wild. What
        # the claim actually asserts is that the step is REJECTED and the message names the
        # missing field — assert exactly that, against either type.
        from pydantic import ValidationError as PydanticValidationError

        try:
            from fastmcp.exceptions import ValidationError as FastMCPValidationError
        except ImportError:  # pragma: no cover - older FastMCP without its own type
            FastMCPValidationError = PydanticValidationError

        session = DuckSession.open([f"shop={sqlite_file}"])
        srv = build_server(session, require_descriptions=True)
        try:
            # `description` is a required field of the step model, so a step that omits it fails
            # FastMCP's argument-schema validation before the tool body ever runs.
            with pytest.raises(
                (PydanticValidationError, FastMCPValidationError), match="description"
            ):
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
        # fetch reaches the network on the agent's behalf, so it shares the same gate
        assert "fetch" not in names

    def test_tools_present_with_flag(self, sqlite_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        try:
            names = {t.name for t in _run(build_server(session, allow_add_source=True).list_tools())}
            assert {"add_source", "remove_source", "fetch"} <= names
        finally:
            session.close()

    def test_fetch_rejects_mixing_single_and_batch(self, sqlite_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        server = build_server(session, allow_add_source=True)
        try:
            with pytest.raises(Exception, match="not both"):
                _run(server.call_tool("fetch", {
                    "source": "x", "path": "/a", "name": "n", "steps": [{"path": "/b"}],
                }))
        finally:
            session.close()

    def test_fetch_needs_a_connection_source(self, sqlite_file):
        session = DuckSession.open([f"shop={sqlite_file}"])
        server = build_server(session, allow_add_source=True)
        try:
            with pytest.raises(Exception, match="No API connection"):
                _run(server.call_tool(
                    "fetch", {"source": "shop", "path": "/movies", "name": "m"}
                ))
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


class TestEnvFile:
    def test_load_env_file(self, tmp_path, monkeypatch):
        from spelunk.mcp.server import _load_env_file

        monkeypatch.delenv("SPELUNK_EF_NEW", raising=False)
        monkeypatch.setenv("SPELUNK_EF_KEPT", "original")
        p = tmp_path / ".env"
        p.write_text(
            "# comment\n\nSPELUNK_EF_NEW='v-1'\nSPELUNK_EF_KEPT=overridden\nBAD LINE\n",
            encoding="utf-8",
        )
        _load_env_file(str(p))
        import os

        assert os.environ["SPELUNK_EF_NEW"] == "v-1"  # quotes stripped
        assert os.environ["SPELUNK_EF_KEPT"] == "original"  # existing env wins

    def test_missing_file_warns_not_raises(self, tmp_path, capsys):
        from spelunk.mcp.server import _load_env_file

        _load_env_file(str(tmp_path / "absent.env"))
        assert "--env-file" in capsys.readouterr().err
