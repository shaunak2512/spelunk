"""Contracts that hold by convention today: frozen types, tool schemas, packaging, docs.

Claims QRY-021 (frozen types), MCP-002 (@_logged preserves each tool's schema), MCP-006
(library callers log nowhere), MCP-007 (the tool log is queryable by Spelunk itself),
MCP-008 (the server instructions describe tools that exist), MCP-010 (packaging/entry point),
MCP-012 (no submodule re-exports), MCP-014 (the checked-in .mcp.json is usable).

These share a failure mode: breaking one leaves the suite green while every consumer breaks.
"""
from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

import pytest

from spelunk.core.duck import DuckSession
from spelunk.core.types import ColumnInfo, TableDescription, TableInfo
from spelunk.mcp.server import build_server

ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


class TestFrozenTypes:
    """QRY-021: TableInfo / TableDescription / ColumnInfo are a frozen contract.

    A golden field set, so a rename or removal is a build break rather than a silent
    breakage of whatever reads db://tables.
    """

    GOLDEN = {
        "TableInfo": {"name", "kind", "row_count", "comment"},
        "ColumnInfo": {"name", "type", "nullable", "primary_key", "comment"},
        "TableDescription": {
            "name", "columns", "primary_key", "foreign_keys", "indexes",
            "sample_rows", "profile", "row_count",
        },
    }

    @pytest.mark.parametrize("model", [TableInfo, ColumnInfo, TableDescription])
    def test_field_set_is_unchanged(self, model):
        assert set(model.model_fields) == self.GOLDEN[model.__name__]

    def test_required_fields_are_unchanged(self):
        """Making a field required (or optional) is just as breaking as renaming it."""
        assert {n for n, f in TableInfo.model_fields.items() if f.is_required()} == {"name"}
        assert {n for n, f in ColumnInfo.model_fields.items() if f.is_required()} == {
            "name", "type"
        }
        assert {n for n, f in TableDescription.model_fields.items() if f.is_required()} == {
            "name", "columns"
        }


class TestToolSchemas:
    """MCP-002: @_logged preserves each function's signature, so FastMCP's schema is unchanged.

    If the decorator stopped using functools.wraps + the original signature, every tool would
    advertise (*args, **kwargs) and agents would lose all parameter guidance — with the whole
    suite still green.
    """

    EXPECTED_PARAMS = {
        "query": {"sql", "name", "steps", "flow"},
        "profile": {"sql", "flow"},
        "export": {"target", "format", "path", "flow"},
        "catalog": {"flow", "source", "object"},
        "drop": {"name", "flow"},
        "lineage": {"name", "flow", "render", "path"},
        "replay": {"flow", "into", "dry_run"},
    }

    def test_every_tool_advertises_its_real_parameters(self, sqlite_file, tmp_path):
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            server = build_server(session)
            tools = {t.name: t for t in _run(server.list_tools())}
            for name, expected in self.EXPECTED_PARAMS.items():
                schema = tools[name].parameters
                advertised = set(schema.get("properties", {}))
                assert advertised == expected, f"{name} advertises {advertised}"
                assert "args" not in advertised and "kwargs" not in advertised
        finally:
            session.close()

    def test_gated_tools_keep_their_parameters_too(self, sqlite_file, tmp_path):
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            server = build_server(session, allow_add_source=True)
            tools = {t.name: t for t in _run(server.list_tools())}
            assert set(tools["add_source"].parameters.get("properties", {})) == {"spec"}
            assert set(tools["remove_source"].parameters.get("properties", {})) == {"name"}
            fetch_params = set(tools["fetch"].parameters.get("properties", {}))
            assert {"source", "path", "name", "params", "rows_from", "steps"} <= fetch_params
        finally:
            session.close()


class TestLibraryCallersDoNotLog:
    """MCP-006: build_server(session) logs NOWHERE unless passed tool_log=."""

    def test_no_log_file_is_created(self, sqlite_file, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            server = build_server(session)  # no tool_log
            _run(server.call_tool("query", {"sql": "SELECT 1 AS a", "name": "one"}))
            _run(server.call_tool("catalog", {}))
        finally:
            session.close()

        stray = list(tmp_path.rglob("*.jsonl"))
        assert stray == [], f"a library embedding created log files: {stray}"


class TestToolLogIsQueryableBySpelunk:
    """MCP-007: the JSONL is queryable by Spelunk itself via read_json_auto(...).

    Plausibly fragile: `args` differs in shape per tool, which is exactly the heterogeneous
    JSON that trips DuckDB's inference elsewhere in this codebase.
    """

    def test_a_mixed_tool_log_reads_back_as_a_table(self, sqlite_file, tmp_path):
        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        server = build_server(session, tool_log=str(log_path))
        try:
            # Deliberately heterogeneous: different tools, a batch, and a failure.
            _run(server.call_tool("query", {"sql": "SELECT 1 AS a", "name": "one"}))
            _run(server.call_tool("query", {"steps": [
                {"sql": 'SELECT * FROM "shop"."customers"', "name": "base"},
                {"sql": "SELECT COUNT(*) AS n FROM base", "name": "agg"},
            ]}))
            _run(server.call_tool("catalog", {}))
            _run(server.call_tool("drop", {"name": "one"}))
            try:
                _run(server.call_tool("query", {"sql": "DROP TABLE x", "name": "bad"}))
            except Exception:
                pass
        finally:
            session.close()

        reader = DuckSession.open([], session_dir=str(tmp_path / "ws2"))
        try:
            posix = str(log_path).replace("\\", "/")
            out = reader.query(
                f"SELECT tool, outcome FROM read_json_auto('{posix}') ORDER BY tool", "calls"
            )
            assert out["row_count"] == 5
            tools_logged = {row[0] for row in out["sample"]}
            assert {"query", "catalog", "drop"} <= tools_logged
            # The usage analysis the claim exists for: per-tool call counts.
            agg = reader.query(
                f"SELECT tool, COUNT(*) AS n FROM read_json_auto('{posix}') "
                "GROUP BY tool ORDER BY n DESC",
                "per_tool",
            )
            assert agg["sample"][0] == ["query", 3]
        finally:
            reader.close()


class TestServerInstructions:
    """MCP-008: the instructions block is the agent's only orientation — every tool it names
    must exist, or the agent is being told to call something that isn't there."""

    def test_every_tool_named_in_the_instructions_is_registered(self, sqlite_file, tmp_path):
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            server = build_server(session, allow_add_source=True)
            registered = {t.name for t in _run(server.list_tools())}
            instructions = server.instructions or ""
            # Every `name(` mention in the instructions that looks like a tool call.
            import re

            mentioned = set(re.findall(r"`([a-z_]+)\(", instructions))
            unknown = mentioned - registered
            assert not unknown, f"instructions reference unregistered tools: {unknown}"
            # And the core surface really is described.
            assert {"query", "profile", "export", "catalog", "drop", "lineage", "replay"} <= mentioned
        finally:
            session.close()

    def test_resource_uris_named_in_the_instructions_exist(self, sqlite_file, tmp_path):
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            server = build_server(session)
            instructions = server.instructions or ""
            uris = {str(r.uri) for r in _run(server.list_resources())}
            templates = {t.uri_template for t in _run(server.list_resource_templates())}
            if "db://tables" in instructions:
                assert any("tables" in u for u in uris)
            if "db://{table}" in instructions:
                assert any("{table}" in t for t in templates)
        finally:
            session.close()


class TestPackaging:
    """MCP-010: published as spelunk-mcp, the command is spelunk."""

    def test_entry_point_is_declared_and_importable(self):
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert meta["project"]["name"] == "spelunk-mcp"
        assert meta["project"]["scripts"]["spelunk"] == "spelunk.mcp.server:main"

        module_path, _, attr = meta["project"]["scripts"]["spelunk"].partition(":")
        import importlib

        module = importlib.import_module(module_path)
        assert callable(getattr(module, attr)), "the declared console script target is not callable"

    def test_readme_documents_the_published_name(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "spelunk-mcp" in readme
        assert "uvx" in readme


class TestNoSubmoduleReExports:
    """MCP-012: __init__.py does not re-export submodules — import from the submodule directly."""

    def test_engine_submodules_are_not_available_from_the_package(self):
        import spelunk.core as core

        for submodule in ("duck", "sources", "apifetch", "openapi"):
            assert submodule not in getattr(core, "__all__", []), (
                f"{submodule} is re-exported; the documented import style is now optional"
            )
        assert not hasattr(core, "DuckSession"), (
            "DuckSession is reachable from spelunk.core — CLAUDE.md says import from "
            "spelunk.core.duck directly"
        )

    def test_the_documented_import_path_works(self):
        from spelunk.core.duck import DuckSession as Imported

        assert Imported is DuckSession


class TestNativeImportsAreWarmed:
    """MCP-013: DuckSession.open() warms numpy/pandas on the MAIN thread.

    The failure this prevents is the worst in the register — a first-time C-extension import on
    an anyio worker thread under a running asyncio loop deadlocks on Windows and the tool call
    hangs forever. A test cannot safely reproduce the hang (it would hang the suite), so this
    pins the MECHANISM instead, in a fresh interpreter where the modules are not already loaded.
    """

    def test_open_imports_them_before_any_tool_runs(self, tmp_path):
        import subprocess
        import sys
        import textwrap

        probe = textwrap.dedent(
            """
            import sys
            assert "numpy" not in sys.modules, "numpy was already imported before open()"
            from spelunk.core.duck import DuckSession
            session = DuckSession.open([])
            try:
                loaded = [m for m in ("numpy", "pandas") if m in sys.modules]
                print(",".join(loaded))
            finally:
                session.close()
            """
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=300,
            cwd=str(ROOT),
        )
        assert out.returncode == 0, out.stdout + out.stderr
        warmed = set(out.stdout.strip().split(","))
        assert "numpy" in warmed, (
            "open() did not warm numpy on the main thread — a first import on a worker thread "
            "under the event loop can deadlock (see CLAUDE.md)"
        )

    def test_a_tool_call_on_a_worker_thread_completes(self, sqlite_file, tmp_path):
        """The end-to-end shape of the bug: a sync tool driven off the event loop."""
        import anyio

        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        server = build_server(session)
        try:
            async def drive():
                with anyio.fail_after(120):
                    return await server.call_tool(
                        "query", {"sql": 'SELECT COUNT(*) AS n FROM "shop"."customers"',
                                  "name": "n"}
                    )

            result = _run(drive())
            assert result is not None
        finally:
            session.close()


class TestCheckedInMcpConfig:
    """MCP-014: a broken example config is a first-run failure for a new user."""

    def test_mcp_json_is_well_formed(self):
        path = ROOT / ".mcp.json"
        if not path.exists():
            pytest.skip(".mcp.json is not checked in")
        config = json.loads(path.read_text(encoding="utf-8"))
        servers = config["mcpServers"]
        assert servers, "no servers configured"
        for name, entry in servers.items():
            assert entry.get("command"), f"{name} has no command"
            assert isinstance(entry.get("args", []), list), f"{name} args must be a list"

    def test_configured_source_specs_are_classifiable(self):
        """Each --source in the example must be a spec the server can actually detect."""
        path = ROOT / ".mcp.json"
        if not path.exists():
            pytest.skip(".mcp.json is not checked in")
        from spelunk.core.sources import detect_kind, parse_spec

        config = json.loads(path.read_text(encoding="utf-8"))
        for entry in config["mcpServers"].values():
            args = entry.get("args", [])
            specs = [args[i + 1] for i, a in enumerate(args) if a == "--source" and i + 1 < len(args)]
            for spec in specs:
                _, locator = parse_spec(spec)
                assert detect_kind(locator), f"unclassifiable source spec in .mcp.json: {spec}"
