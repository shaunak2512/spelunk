"""The EXPERIMENTAL `--code-mode` surface (branch feature/code-mode-test).

CodeMode replaces Spelunk's whole tool catalog with FastMCP's meta-tools — `search` /
`get_schema` to discover, `execute` to run agent-written Python whose only capability is
`await call_tool(name, params)`. These tests pin the three things that experiment can get
wrong without anyone noticing:

* the surface really does collapse (and the default surface is untouched);
* a tool reached from inside the sandbox is still a FULL Spelunk tool — it materializes,
  records lineage, and passes through the `@_logged` decorator — because the whole premise is
  that CodeMode changes *how tools are reached*, not what they do;
* `show` is gone, for the reason documented at its registration site.

The sandbox runtime (`pydantic-monty`) is an optional extra, so the `execute` tests skip
without it rather than fail. Everything else — the collapsed surface, the missing-sandbox
error — is asserted unconditionally, since none of it needs the sandbox to run.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from spelunk.core.duck import DuckSession
from spelunk.mcp import server as server_mod
from spelunk.mcp.server import build_server

CODE_MODE_TOOLS = {"search", "get_schema", "execute"}
SPELUNK_TOOLS = {"query", "profile", "export", "catalog", "drop", "lineage", "replay"}

# Independent of the server's own import guard, for the reason test_cli.py spells out: deriving
# the expectation from the implementation's signal lets a broken detection satisfy both sides.
SANDBOX_INSTALLED = importlib.util.find_spec("pydantic_monty") is not None
requires_sandbox = pytest.mark.skipif(
    not SANDBOX_INSTALLED, reason="needs the code-mode extra (pydantic-monty)"
)


@pytest.fixture
def session(tmp_path):
    s = DuckSession.open([], session_dir=str(tmp_path))
    try:
        yield s
    finally:
        s.close()


def _tool_names(server) -> set[str]:
    async def go():
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(go())


def _call(server, tool: str, args: dict):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


# --- the collapsed surface ----------------------------------------------------------------- #


def test_code_mode_collapses_the_tool_surface(session):
    """Clients see the meta-tools and nothing else — no Spelunk tool is directly callable."""
    names = _tool_names(build_server(session, code_mode=True))
    assert names == CODE_MODE_TOOLS
    assert not (names & SPELUNK_TOOLS)


def test_default_surface_is_untouched(session):
    """The flag is opt-in: without it the documented tools are exactly as before."""
    names = _tool_names(build_server(session))
    assert SPELUNK_TOOLS <= names
    assert not (names & CODE_MODE_TOOLS)


def test_gated_tools_are_collapsed_too(session):
    """--allow-add-source still gates registration; CodeMode then hides those tools like the
    rest. They stay reachable from inside `execute`, which is the point — the gate decides
    whether the capability EXISTS, code mode only decides how it is addressed."""
    server = build_server(session, allow_add_source=True, code_mode=True)
    assert _tool_names(server) == CODE_MODE_TOOLS


@pytest.mark.skipif(
    importlib.util.find_spec("prefab_ui") is None, reason="needs the [ui] extra"
)
def test_show_is_suppressed_under_code_mode(session):
    """`show` is registered normally, and deliberately NOT under code mode: `call_tool` unwraps
    a ToolResult to its structured_content, which for `show` IS the Prefab payload — the agent
    would get the render payload and the human would see nothing."""
    assert "show" in _tool_names(build_server(session))
    assert "show" not in _tool_names(build_server(session, code_mode=True))


# --- discovery ----------------------------------------------------------------------------- #


@requires_sandbox
def test_search_finds_query_with_its_schema(session):
    """Two-stage discovery: `search` returns parameter detail inline, so an agent can write the
    call without a second round trip through `get_schema`."""
    result = _call(build_server(session, code_mode=True), "search", {"query": "run a sql select"})
    text = result.content[0].text
    assert "query" in text
    assert "sql" in text and "name" in text  # detail="detailed" renders parameters


# --- execute ------------------------------------------------------------------------------- #


@requires_sandbox
def test_execute_chains_tool_calls_in_one_round_trip(session):
    """The headline capability: several dependent calls, resolved server-side, one result back."""
    # `sample` is a list of ROW LISTS, with names carried separately in `columns` — the agent
    # zips them itself. Asserted positionally here for the same reason the agent must: there
    # are no keys on a sample row.
    code = """
await call_tool("query", {"sql": "select * from range(10) t(n)", "name": "base"})
r = await call_tool("query", {"sql": "select sum(n) as total from base", "name": "agg"})
return r["sample"][0][0]
"""
    result = _call(build_server(session, code_mode=True), "execute", {"code": code})
    assert json.loads(result.content[0].text) == 45


@requires_sandbox
def test_execute_can_branch_on_intermediate_results(session):
    """What `query(steps=[...])` cannot express: the SECOND call depends on a value read from
    the first. This is the actual reason to run the experiment — batch mode already covers
    straight-line chains, so a data-dependent branch is the only new capability on offer."""
    code = """
r = await call_tool("query", {"sql": "select * from range(100) t(n)", "name": "base"})
if r["row_count"] > 50:
    out = await call_tool("query", {"sql": "select count(*) c from base where n > 50", "name": "big"})
else:
    out = await call_tool("query", {"sql": "select count(*) c from base", "name": "small"})
return {"branch": "big" if r["row_count"] > 50 else "small", "c": out["sample"][0][0]}
"""
    result = _call(build_server(session, code_mode=True), "execute", {"code": code})
    assert json.loads(result.content[0].text) == {"branch": "big", "c": 49}


@requires_sandbox
def test_sandbox_results_are_real_results_with_lineage(session):
    """A tool called from the sandbox is the SAME tool: it materializes into the flow and
    records provenance. If this ever fails, code mode has become a second, weaker path to the
    engine rather than a different way to address the one path."""
    code = """
await call_tool("query", {"sql": "select * from range(5) t(n)", "name": "src"})
await call_tool("query", {"sql": "select n * 2 as d from src", "name": "derived"})
return "ok"
"""
    _call(build_server(session, code_mode=True), "execute", {"code": code})

    # Materialized in the real workspace, readable outside the sandbox entirely.
    assert session.query("select count(*) c from derived", "check")["sample"][0][0] == 5

    # And the dependency edge src -> derived was recorded, so `replay` still works.
    graph = session.lineage(name="derived")
    assert "src" in {n["name"] for n in graph["nodes"]}


@requires_sandbox
def test_tool_calls_from_the_sandbox_are_logged(session, tmp_path):
    """The `@_logged` decorator wraps the tool functions, and CodeMode reaches them through
    `ctx.fastmcp.call_tool` — so per-call logging (and its DSN masking) survives code mode.
    Worth pinning: the tool log is the only record of what the agent actually did, and under
    code mode the agent's own transcript shows one opaque `execute` instead of N calls."""
    log = tmp_path / "tool-calls.jsonl"
    server = build_server(session, tool_log=str(log), code_mode=True)
    code = """
await call_tool("query", {"sql": "select 1 as x", "name": "one"})
return "ok"
"""
    _call(server, "execute", {"code": code})

    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(e["tool"] == "query" for e in entries), entries


@requires_sandbox
def test_tool_call_budget_is_enforced(session):
    """CodeMode caps `call_tool` invocations per execute (default 50) so a runaway agent loop
    cannot pin the engine. It surfaces as a failed tool call, not a truncated success — the
    distinction that matters, since a silently short-circuited loop would return partial work
    the agent would read as complete."""
    code = """
for i in range(60):
    await call_tool("query", {"sql": f"select {i} as n", "name": "tmp"})
return "never"
"""
    with pytest.raises(ToolError, match="limit"):
        _call(build_server(session, code_mode=True), "execute", {"code": code})


# --- the missing-sandbox path -------------------------------------------------------------- #


def test_missing_sandbox_fails_at_build_not_at_first_execute(session, monkeypatch):
    """MontySandboxProvider resolves pydantic_monty lazily inside run(), so without this guard a
    sandbox-less server starts clean, collapses its own tool surface, and dies on the agent's
    first `execute` with no fallback path left. Fail while the operator is still watching."""
    monkeypatch.setattr(server_mod, "_code_mode_sandbox_available", lambda: False)
    with pytest.raises(RuntimeError, match="code-mode"):
        build_server(session, code_mode=True)


def test_missing_sandbox_does_not_affect_the_default_server(session, monkeypatch):
    """The guard is scoped to the flag — a normal server never touches the sandbox."""
    monkeypatch.setattr(server_mod, "_code_mode_sandbox_available", lambda: False)
    assert SPELUNK_TOOLS <= _tool_names(build_server(session))
