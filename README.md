# Spelunk

A lean, **vendor-agnostic** harness that lets a *cheap* model match an *expensive* one on real-schema
SQL — **measured on BIRD, not claimed.**

Full design: [`database-agent-harness-build-spec.md`](database-agent-harness-build-spec.md).
Build backlog (wave-ordered): [`TASKS.md`](TASKS.md).

## Layout

| Path | Role |
|---|---|
| `spelunk/core` | Agent-agnostic tools library — `connect`, `list_objects`, `describe`, `run_sql`, guards. **No LLM, no protocol.** |
| `spelunk/agent` | LangGraph exploration agent (the eval / standalone front-end). |
| `spelunk/rag` | Schema retrieval (top-k tables for a question). |
| `spelunk/mcp` | Optional MCP server front-end — reuses `core` verbatim. Includes a flow-based DuckDB session workspace for multi-step analysis. |
| `spelunk/eval` | BIRD dataset, scoring, reporting + **frozen data schemas**. |
| `tests` | Acceptance tests — the definition of done for each module. |

## Architecture in one line

One `core/` tools library; two thin front-ends over it — your own LangGraph agent (for the benchmark)
and an MCP server (so Claude Code can drive the same tools). The core is **assembled** from
SQLAlchemy + sqlglot + DuckDB, not invented.

## MCP session workspace

The MCP server ships a flow-based DuckDB workspace that lets an agent cache source-database results
locally and build multi-step analyses without re-querying the source.

```
extract(sql, name, flow?)   # pull a source-DB SELECT (no row cap) into a named local table
peek(sql, flow?)            # read-only DuckDB query over cached results (capped 1000 rows)
transform(sql, name, flow?) # materialize a DuckDB query as a new table (no row cap)
list_results(flow?)         # list saved results with schema + row counts
export_result(name, fmt, path, flow?)  # write to parquet / csv / json
drop_result(name, flow?)    # delete a single intermediate
drop_flow(flow)             # delete an entire analysis namespace
list_flows()                # see all active flows
```

A **flow** is an isolated DuckDB schema. Give each concurrent line of analysis its own flow;
steps within a flow are sequential, steps across flows are parallel-safe.

Start the server with an optional durable workspace:

```bash
python -m spelunk.mcp.server --dsn sqlite:///path/to.db --session-dir .spelunk_session
```

A `.mcp.json` in the repo root provides a ready-made Claude Code configuration (edit the paths for
your machine before use).

## Dev

```bash
uv sync --extra dev
uv run pytest        # core/agent/eval tests; Wave-0 leaves are RED until implemented (TDD baseline)
```

`tests/test_schemas.py` should pass immediately (the frozen data schemas work); the `core` tests are
the red targets that Wave 1/2 agents turn green.
