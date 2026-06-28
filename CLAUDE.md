# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Spelunk is

A vendor-agnostic harness that lets a cheap model match an expensive one on real-schema SQL — measured on BIRD, not claimed. The core thesis: agentic schema exploration (the agent browses and probes before answering) closes the gap between cheap and frontier models.

Full design: [`database-agent-harness-build-spec.md`](database-agent-harness-build-spec.md). Build backlog: [`TASKS.md`](TASKS.md). Orchestration process: [`briefs/ORCHESTRATION.md`](briefs/ORCHESTRATION.md).

## Commands

```powershell
# Install everything
uv sync --extra dev

# Run all tests
uv run pytest

# Run a single test file
.\.venv\Scripts\python.exe -m pytest tests\test_guard.py -q

# Lint
uv run ruff check spelunk/

# Check test collection without running
.\.venv\Scripts\python.exe -m pytest --co -q
```

Environment requires Python 3.14 (`py -3`) on Windows 11/PowerShell. API keys go in `.env` (ANTHROPIC_API_KEY, OPENAI_API_KEY) — needed only for live runs, not tests.

## Architecture

One `core/` library, two thin front-ends:

```
spelunk/core/        # NO LLM, NO protocol — pure Python DB tools
  guard.py           # sqlglot AST safety: assert_read_only(), enforce_limit()
  connection.py      # SQLAlchemy engine with read-only enforcement
  introspect.py      # list_objects(), describe() (+optional profile)
  query.py           # run_sql() — validates → injects LIMIT → executes
  types.py           # FROZEN: TableInfo, TableDescription, QueryResult, errors

spelunk/agent/       # LangGraph ReAct front-end (eval + standalone)
  tools.py           # wrap core fns as LangChain tools
  graph.py           # ReAct loop: step/probe caps, submit_sql terminator, telemetry
  models.py          # load_model(name) via init_chat_model from configs/models.yaml
  rungs.py           # RungConfig loader for R0/R1/R2 ablation configs

spelunk/rag/
  schema_index.py    # numpy cosine retrieval — top-k tables for a question (no vector DB)

spelunk/mcp/
  server.py          # FastMCP: resources + run_query + DuckDB session workspace
                     #   build_server(engine, session_dir?) — optional durable workspace
                     #   Workspace tools: extract / peek / transform / list_results /
                     #     export_result / drop_result / drop_flow / list_flows
                     #   Each flow is a DuckDB schema; flows isolate parallel analyses

spelunk/eval/        # Benchmark pipeline — everything is built against frozen schemas
  schemas.py         # FROZEN: BirdQuestion, RunResult, RESULTS_CSV_COLUMNS
  dataset.py         # download + stratified-sample BIRD → questions.jsonl
  score.py           # execution accuracy (multiset row comparison, stricter than BIRD's set())
  runner.py          # (model × rung × question) matrix → results.csv + telemetry
  report.py          # results.csv → headline + cost charts (matplotlib)

configs/
  models.yaml        # benchmark models: name, provider, model_id, tier, $/Mtok in/out
  rungs.yaml         # 3 ablation rungs: R0 (schema dump), R1 (explore+profile), R2 (explore+RAG)

data/
  bird/              # BIRD dev DBs + gold SQL (gitignored; download separately)
  questions.jsonl    # frozen 150-question eval sample (5 DBs)
```

**Frozen contracts — treat as a barrier-level change:** `spelunk/core/types.py` and `spelunk/eval/schemas.py`. All modules import from these; don't change them inside a parallel agent branch.

**`__init__.py` files intentionally do not re-export submodules** — import from the submodule directly: `from spelunk.eval.dataset import ...`, not `from spelunk.eval import ...`.

**langchain is imported lazily** inside `load_model()` so config and cost-math tests run without network access or langchain installed.

## Ablation rungs

| Rung | schema_mode | profile | rag | Purpose |
|---|---|---|---|---|
| R0_baseline | dump | no | no | Classic text-to-SQL reference (frontier-only) |
| R1_discovery_fs | explore | yes | no | Agent browses schema itself |
| R2_schema_rag | explore | yes | yes | R1 + pre-retrieve top-k tables |

Cheap models run all three rungs; frontier models run R0 only (the reference bar).

## Wave-based orchestration

When implementing modules in parallel, follow [`briefs/ORCHESTRATION.md`](briefs/ORCHESTRATION.md) exactly. Key rules:

- **One agent, one module file** (+ its own test). Never touch other modules.
- **Never edit orchestrator-owned files** inside a parallel agent: `pyproject.toml`, `configs/*.yaml`, any `__init__.py`.
- Each parallel agent works in its own **git worktree** (`git worktree add -b waveN/<mod> ../spelunk-wt/<mod> main`).
- Install only the minimal deps for your module (see the cheat sheet in ORCHESTRATION.md) — not a full `uv sync`.
- Sub-wave dependencies: Wave 2b (`agent/tools`, `rag/schema_index`, `mcp/server`) must NOT start until Wave 2a (`introspect`, `query`) is merged to `main`.
- **Default worker subagents to Sonnet (`claude-sonnet-4-6`)** to save tokens.

## After implementing a feature

After any feature implementation or module completion:

1. Verify your tests are green: `.\.venv\Scripts\python.exe -m pytest tests\test_<mod>.py -q`
2. Confirm collection is clean: `.\.venv\Scripts\python.exe -m pytest --co -q`
3. Confirm only your intended files changed: `git diff --name-only main HEAD` and `git status --short`
4. Commit to your branch with co-author attribution:
   ```
   git commit -m "$(cat <<'EOF'
   <subject line>

   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
   EOF
   )"
   ```
5. **Do not merge or push** — the orchestrator handles barrier merges.

## MCP session workspace

The MCP server exposes a flow-based DuckDB workspace that lets agents cache source-DB results locally and build multi-step analyses without re-hitting the source database.

**Key concepts:**
- A *flow* is an isolated DuckDB schema (default name `"default"`). Give each concurrent line of analysis its own flow to prevent name collisions.
- Calls within the same flow are serialized (single DuckDB connection). Calls across different flows are parallel-safe.
- A *durable* workspace persists `workspace.duckdb` in `--session-dir`; omitting `--session-dir` gives an ephemeral in-memory workspace.
- `.spelunk_session/` is gitignored (holds the durable workspace file).

**Workspace tools:** `extract` (pull from source DB → named table), `peek` (read-only query, capped 1000 rows), `transform` (materialize a DuckDB query as a new table, uncapped), `list_results`, `export_result` (write parquet/csv/json), `drop_result`, `drop_flow`, `list_flows`.

**Starting the server with a durable workspace:**
```powershell
python -m spelunk.mcp.server --dsn sqlite:///path/to.db --session-dir .spelunk_session
```

A `.mcp.json` in the repo root wires Claude Code to a local SQLite DB with session persistence (path is machine-specific; edit before use).

## Current state

All waves 0–3 are merged to `main`. The `feature/duckdb_pipeline` branch adds the DuckDB session workspace to the MCP server. The remaining deferred items (end-to-end smoke run, README writeup) require API keys in `.env` and actual run results. See TASKS.md for the open action item on model prices.
