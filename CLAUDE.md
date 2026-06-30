# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Spelunk is

A **multi-source DuckDB query + transformation-pipeline MCP server.** Point it at files
(CSV/Parquet/JSON/Excel) and databases (SQLite/PostgreSQL/MySQL), and an agent (Claude Code) can
query across all of them and build step-by-step pipelines through one DuckDB engine.

**Core idea:** a *single DuckDB session* is both the query engine and the workspace. Every source
is `ATTACH`ed (databases) or scanned (files) into one connection, so a single `query` can join a
Parquet file to a Postgres table to a result built two steps ago — all in DuckDB SQL.

> History: Spelunk began as a BIRD text-to-SQL benchmark harness. That eval/agent front-end has
> been removed; the MCP server is the product. (Old design docs in `database-agent-harness-build-spec.md`
> / `TASKS.md` / `briefs/` describe the retired benchmark and are not current.)

## Commands

```powershell
uv sync --extra dev                                   # install
uv run --extra dev python -m pytest -q                # all tests
.\.venv\Scripts\python.exe -m pytest tests\test_duck.py -q   # one file
uv run --extra dev ruff check spelunk/                # lint
.\.venv\Scripts\python.exe -m pytest --co -q          # collection check
```

Python 3.12+ on Windows 11 / PowerShell. No API keys needed (no LLM in the server).

## Architecture

One DuckDB session, wrapped by a thin MCP front-end:

```
spelunk/core/
  duck.py        # DuckSession — THE engine+workspace. open() attaches sources, configures
                 #   memory_limit/temp_directory; methods: query / profile / export / catalog /
                 #   drop / import_remote + list_objects / describe (DuckDB-catalog introspection)
  sources.py     # Source registry: spec -> DuckDB attach/scan SQL (files as VIEWs, DBs ATTACHed
                 #   READ_ONLY). SQLAlchemy fallback Source for SQL Server / exotic auth.
  guard.py       # sqlglot AST safety: assert_read_only(), enforce_limit() — called dialect="duckdb"
  connection.py  # RETAINED, demoted: SQLAlchemy connect(), only for the fallback path
  query.py       # RETAINED, demoted: run_sql(), only used by import_remote's remote pull
  introspect.py  # RETAINED for the fallback path (SQLAlchemy reflection)
  types.py       # FROZEN contracts: TableInfo, TableDescription, ColumnInfo, errors

spelunk/mcp/
  server.py      # FastMCP wrapper: build_server(session) registers 5 tools + 2 resources;
                 #   main() parses --source specs and serves over stdio
```

**`__init__.py` files do not re-export submodules** — import from the submodule directly
(`from spelunk.core.duck import DuckSession`).

## Tool surface

One row-returning tool (`query`) owns every SELECT; inspection lives on the resources + `profile`.

| Tool | Purpose |
|---|---|
| `query(sql, name, flow?)` | Run a read-only SELECT over sources + saved results; **materialize the full result** as table `name` (required). Returns columns, true row_count, head sample. The one tool for looking *and* building — results are named and immediately reusable. |
| `profile(sql, flow?)` | Per-column stats (null_rate, min/max/mean/std, p25/p50/p75/p95; unique/top/freq) — no row cap. |
| `export(target, format, path, flow?)` | Write a saved result name **or** a full SELECT to csv/json/parquet. |
| `catalog(flow?)` | No arg → list flows + counts; with a flow → its results. |
| `drop(name?, flow?)` | Drop one result, or a whole flow (name omitted). |
| `import_remote(sql, name, flow?)` | **Only registered when a SQL Server / SQLAlchemy-only source is configured** (or `--allow-add-source`, since one can be added at runtime) — DuckDB can't attach it, so pull a SELECT in, then query the table. |
| `add_source(spec)` / `remove_source(name)` | **Only registered with `--allow-add-source`** — attach/detach a file or DB at runtime (`spec` is the same grammar as `--source`). Connection-global: a source is visible in **every flow**, not flow-scoped (DuckDB `ATTACH` can't be per-schema). Isolation comes from the process-per-agent model. |

Resources: `db://tables` (queryable objects — attached-DB tables named `<source>.<table>`, file
views named bare) and `db://{table}` (columns, PK, sample, row count).

## Key concepts

- **Source naming in SQL:** attached databases → `"<source>"."<table>"`; file sources and prior
  results → bare name. `query`'s search_path is `<flow>,main`, so flow results + file views resolve
  bare; cross-flow results use `"<flow>"."<name>"`.
- **Flow** = an isolated result namespace (a DuckDB schema; default `"default"`). Calls within a
  flow are serialized (one DuckDB connection, one lock); across flows they're parallel-safe.
- **Materialize-by-default:** `query` does `CREATE OR REPLACE TABLE` — computed once, cheap to
  reuse, correct for pipelines (a DuckDB *view* re-executes its whole upstream on every reference).
  A nudge fires on an unfiltered `SELECT *` that copies a large source table wholesale.
- **Disk-backed always + out-of-core:** the workspace is a real DuckDB file (under `--session-dir`,
  else a temp dir). Sources are read on demand with pushdown; buffering operators spill to
  `temp_directory`. A source larger than RAM is the normal case, not a failure.
- **Read-only** = `ATTACH (READ_ONLY)` + the sqlglot guard on every query; the server constructs the
  `CREATE TABLE` DDL itself, so agent SQL is SELECT-only.

- **Tools run on worker threads:** FastMCP runs each sync tool off the event loop
  (`anyio.to_thread`). DuckDB lazily imports numpy/pandas on the first result fetch, and that
  C-extension import deadlocks if it first happens on a worker thread under the running asyncio
  loop (Windows) — the tool call then hangs forever. `DuckSession.open()` calls
  `_warm_native_imports()` on the main thread to pre-load them. Don't add other lazy C-extension
  imports to the tool hot path without warming them the same way.

**Frozen contract:** `spelunk/core/types.py` — `TableInfo` / `TableDescription` / `ColumnInfo` are
shared by the resources; treat changes as barrier-level.

## Running the server

```powershell
python -m spelunk.mcp.server --source sales=./data/sales.parquet --source sqlite:///app.db --session-dir .spelunk_session
```

`--source` is repeatable and auto-detects by extension/scheme (`name=` prefix sets the catalog/view
name). Optional guards: `--memory-limit`, `--temp-dir`, `--max-temp-size`. `--dsn` is a back-compat
alias for one `--source`. A `.mcp.json` wires Claude Code to a local source (paths are
machine-specific; edit before use).

**`--allow-add-source` (off by default):** registers the `add_source` / `remove_source` tools so the
agent can attach and detach sources at runtime. This lets the agent read **any** file or database the
server process can reach (including DSNs with embedded credentials), so only enable it for a trusted
setup — and it's sound precisely *because* of the process-per-agent default: each agent's server is
its own process, so an add/remove touches only that agent's isolated connection and never another's.

**Per-process workspace (the default):** `--session-dir` is a *root* (default `./.spelunk_session`,
created if missing, gitignored) and **each server process gets its own durable workspace** at
`<session-dir>/<pid>-<rand>/workspace.duckdb` — so many concurrent servers never collide and never
contend for a single-writer lock. The tool-log defaults alongside it
(`<session-dir>/<pid>-<rand>/tool-calls.jsonl`, dir auto-created). Each run gets a fresh subdir —
isolation, not a shared store across runs. `DuckSession.workspace_dir` is the resolved per-process
dir.

**Workspace GC:** to stop per-process subdirs accumulating, `open()` sweeps on startup — it keeps
the `--keep-workspaces N` most recent (default 3, including the one just created) and reclaims older
subdirs that have no *live* owner. Liveness is the DuckDB file lock: the sweep probes each candidate
with a read-write `connect` (a live server holds the single-writer lock → skip; a crashed/exited one
opens → delete). This is cross-process only — two sessions in one process share DuckDB's cached
instance, so a sweep can't detect an in-process holder (irrelevant in production: each server is its
own process). Dirs younger than a 60s grace window are never touched (a sibling may be mid-startup,
lock not yet held). `--keep-workspaces 0` (or `<=0`) disables the sweep. The tool-log lives inside
the workspace dir, so it's reclaimed with it — route `--tool-log` elsewhere to retain history.

Pass `--shared-workspace` (CLI) / `per_process=False` (`DuckSession.open`) for the old single
`<session-dir>/workspace.duckdb` that one caller can reopen across restarts — at the cost of
single-writer contention (only the first concurrent server is durable, the rest fall back to
ephemeral). `DuckSession.open(session_dir=None)` is still an ephemeral private temp workspace.

**Tool-call logging:** every tool call appends one JSON line (ts, tool, args, outcome, result
summary, duration_ms) for usage analysis — wired by the `@_logged` decorator in `mcp/server.py`,
which preserves each function's signature so FastMCP's schema is unchanged. `--tool-log` controls
the sink: a file path, `-` for stderr, or `off` to disable. Default: `<session-dir>/tool-calls.jsonl`
when `--session-dir` is set, else stderr — never stdout (that's the stdio MCP transport). Library
callers of `build_server(session)` log nowhere unless passed `tool_log=`. The JSONL is queryable by
Spelunk itself via `read_json_auto(...)`.

## After making a change

1. `uv run --extra dev python -m pytest -q` — green.
2. `.\.venv\Scripts\python.exe -m pytest --co -q` — clean collection.
3. `uv run --extra dev ruff check spelunk/`.
4. Commit with co-author attribution; do not push unless asked.
