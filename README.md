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
| `spelunk/mcp` | Optional MCP server front-end — reuses `core` verbatim. |
| `spelunk/eval` | BIRD dataset, scoring, reporting + **frozen data schemas**. |
| `tests` | Acceptance tests — the definition of done for each module. |

## Architecture in one line

One `core/` tools library; two thin front-ends over it — your own LangGraph agent (for the benchmark)
and an MCP server (so Claude Code can drive the same tools). The core is **assembled** from
SQLAlchemy + sqlglot + DuckDB, not invented.

## Dev

```bash
uv sync --extra dev
uv run pytest        # core/agent/eval tests; Wave-0 leaves are RED until implemented (TDD baseline)
```

`tests/test_schemas.py` should pass immediately (the frozen data schemas work); the `core` tests are
the red targets that Wave 1/2 agents turn green.
