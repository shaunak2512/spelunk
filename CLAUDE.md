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
                 #   memory_limit/temp_directory; methods: query / fetch / profile / export /
                 #   catalog / drop / lineage / replay + list_objects / describe. fetch() calls
                 #   one endpoint of an openapi: connection (network I/O OUTSIDE the lock) and
                 #   materializes it like query does, with a kind='fetch' lineage node whose
                 #   "sql" is `FETCH <json>` (source/path/params/rows_from — no secrets).
                 #   query() records
                 #   provenance (SQL + dep edges) into the internal _spelunk_meta.lineage
                 #   table; lineage() reads that DAG, replay() rebuilds a flow from it.
  sources.py     # Source registry: spec -> DuckDB attach/scan SQL (files + lakehouse scans as
                 #   VIEWs, DBs/DuckLake ATTACHed READ_ONLY). Kinds: file (local OR remote
                 #   https://,s3://,gs://,az:// via httpfs/azure ext; ext-backed readers excel/avro),
                 #   sqlite/postgres/mysql, delta:/iceberg: (delta_scan/iceberg_scan VIEWs),
                 #   ducklake:, api: (ONE REST/JSON endpoint fetched at attach into an NDJSON
                 #   snapshot under <workspace>/snapshots/, view over the snapshot — queries never
                 #   re-fetch; refresh = re-attach), openapi: (the WHOLE API — catalog view +
                 #   Source.connection, an ApiConnection the `fetch` tool calls any endpoint of).
                 #   DuckDB-only — a source it can't attach (e.g.
                 #   SQL Server) is rejected, not bridged. DSNs are parsed with stdlib urllib (no
                 #   SQLAlchemy dep).
  apifetch.py    # The api: fetcher (stdlib urllib): spec grammar `api:<url> [key=value ...]` —
                 #   records=<dot.path>, paginate=none|page|offset|cursor|keyset|link|odata
                 #   (cursor follows a full or relative next-URL directly, PokeAPI-style; keyset =
                 #   Stripe-style starting_after from the LAST RECORD's keyset_field, exclusive
                 #   semantics; odata = cursor pre-configured for @odata.nextLink + records=value,
                 #   plus filter="<SQL predicate>"/select=<cols> translated server-side to
                 #   $filter/$select via sqlglot — untranslatable SQL errors loudly, never
                 #   silently fetches everything; options tokenized shell-style so quoted
                 #   values may contain spaces),
                 #   max_pages/max_rows caps. Auth: auth_env=<ENV> (Bearer), header=<Name>:<ENV>
                 #   (any header, e.g. X-Api-Key), param=<name>:<ENV> (query-param keys) — specs
                 #   carry env var NAMES, never values; injected values are scrubbed from error
                 #   messages ($ENV placeholder). Retries 429/5xx with
                 #   backoff + Retry-After; repeat-page guard stops APIs that ignore page params;
                 #   a 404 mid-pagination = end-of-data (TVMaze-style), on page 1 = error;
                 #   fetch fingerprint (url/fetched_at/pages/row_count) returned as Source.info.
                 #   ALSO the connection layer: ApiConnection (base URL + creds + shared
                 #   conventions, built by an openapi: source) + ApiRequest (path/params/options)
                 #   -> resolve_request() binds them into the SAME ApiSpec, so pagination/auth/
                 #   record extraction have one implementation. Param layering: inline query <
                 #   conn.params < call params; pagination-managed and credential-named params
                 #   are RESERVED (error, never a silent override); {placeholder}s consume their
                 #   param and are percent-encoded as single segments (a bound value can't
                 #   redirect the request). fetch_fanout() = one URL per prep-query row through a
                 #   thread pool sharing one HostLimiter (a 429 backs off every worker),
                 #   _key_<placeholder> stamped for the join back, per-entity 404 = data
                 #   (skipped + reported), 401/other = abort. max_urls is a HARD ERROR.
                 #   Live smoke-check: tests/live_api_check.py (manual; hits real public APIs).
  openapi.py     # openapi:<url-or-path> -> queryable ENDPOINT CATALOG (one row per path+method
                 #   — method LOWERCASE, matching the document's own keys): params (with the
                 #   EFFECTIVE style/explode, which decides list-param serialization), auth shape
                 #   mapped onto auth_env=/header=/param=, pagination/records hints,
                 #   response_fields (what the endpoint RETURNS — {name,type} structs flattened 2
                 #   levels: a.b for nested objects, a[].b for arrays of objects — so an agent can
                 #   find an endpoint by the data it carries, not just its URL), and a paste-ready
                 #   suggested_spec api: string for GETs with <SET_ME> where the credential env
                 #   var name goes. records_hint uses an ENVELOPE test (conventional wrapper key,
                 #   else a small object with one array beside scalars) — first-array-wins called
                 #   TMDB's /movie/{id} a list of genres. Guidance-as-data: the agent SQL-queries
                 #   the catalog, fills <SET_ME>, feeds suggested_spec to add_source.
                 #   OpenAPI 3.x JSON only (YAML/Swagger2 rejected with conversion pointers).
  guard.py       # sqlglot AST safety: assert_read_only(), enforce_limit() — called dialect="duckdb"
  types.py       # FROZEN contracts: TableInfo, TableDescription, ColumnInfo, errors

spelunk/mcp/
  server.py      # FastMCP wrapper: build_server(session) registers 7 tools + 2 resources
                 #   (+3 behind --allow-add-source: add_source/remove_source/fetch);
                 #   main() parses --source specs and serves over stdio
```

**`__init__.py` files do not re-export submodules** — import from the submodule directly
(`from spelunk.core.duck import DuckSession`).

## Tool surface

One row-returning tool (`query`) owns every SELECT; inspection lives on the resources + `profile`.

| Tool | Purpose |
|---|---|
| `query(sql, name, flow?)` | Run a read-only SELECT over sources + saved results; **materialize the full result** as table `name` (required). Returns columns, true row_count, and a sample — a 5-row head, or **every row** (with `complete: true`) when the result is small on both axes (row_count ≤ 50 and row_count×cols ≤ 1000), so an agent reads a small deliverable without paging it out into junk tables. The one tool for looking *and* building — results are named and immediately reusable. **Batch mode:** `query(steps=[{sql,name},...], flow?)` (mutually exclusive with `sql`/`name`) runs an ordered list in one call; later steps *may* reference earlier steps' names; semantics identical to N sequential calls (same guard, same lineage rows). Not only for pipelines — steps can be a dependent chain, unrelated queries, or a mix. Fail-fast — completed steps stay materialized, the failing step reports its error, the rest are skipped. Every **terminal** step (one no later step references — the last, plus any independent query) returns a sample (full rows when small, else a head); downstream-consumed intermediates stay compact. A hint after 3 consecutive single-query calls nudges agents toward the batch. |
| `profile(sql, flow?)` | Per-column stats (null_rate, min/max/mean/std, p25/p50/p75/p95; unique/top/freq) — no row cap. |
| `export(target, format, path, flow?)` | Write a saved result name **or** a full SELECT to csv/json/parquet. |
| `catalog(flow?)` | No arg → list flows + counts; with a flow → its results. |
| `drop(name?, flow?)` | Drop one result, or a whole flow (name omitted). |
| `lineage(name?, flow?, render?, path?)` | Provenance graph: with `name`, the upstream closure (transitive, cross-flow) that built a result; without, the whole flow's DAG. Returns nodes (SQL, deps, sources, kind), edges, a dependency-first `order`, and `missing` deps. `render="mermaid"` (or `"dot"`) adds a deterministic, ready-to-display diagram string (key = the format name) built server-side from the same nodes/edges — no agent parsing; Mermaid pastes into markdown/artifacts, DOT runs through `dot -Tsvg`. `path` writes it to a file (implies `render="mermaid"`, echoes `rendered_to`). Read-only. |
| `replay(flow?, into?, dry_run?)` | Rebuild a flow from its recorded SQL in dependency order (re-run each `query`). `into` → non-destructive rebuild into a fresh flow; omitted → in-place refresh; `dry_run` → plan only. Errors on a dependency cycle. Sources + cross-flow results are read, not rebuilt. |
| `add_source(spec)` / `remove_source(name)` | **Only registered with `--allow-add-source`** — attach/detach a file or DB at runtime (`spec` is the same grammar as `--source`). Connection-global: a source is visible in **every flow**, not flow-scoped (DuckDB `ATTACH` can't be per-schema). Isolation comes from the process-per-agent model. |
| `fetch(source, path, name, params?, rows_from?, …)` | **Only registered with `--allow-add-source`** (agent-initiated network reach is one capability, one gate) — call ONE endpoint of an attached `openapi:` **connection** and materialize the response as result `name`. Same return shape as `query`. **One API is one source:** attach it once, then fetch as many endpoints as you like — each response is a flow-scoped *result* (droppable, in `lineage`), never a new source. `params` is a JSON object; a `{placeholder}` in `path` consumes the param of that name as a path segment. `rows_from=<result>` binds remaining placeholders to that result's columns and fetches **one URL per distinct row** (list→detail fan-out), stamping `_key_<placeholder>` for the join back. `steps=[…]` batches several fetches per round trip (fail-fast, like `query`). |

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
  `_materialize_query` ends with `_checkpoint()` (a `CHECKPOINT "<workspace>"`), so every `query`
  — and every successful step of a `query(steps=[...])` batch — folds the WAL into
  `workspace.duckdb` immediately instead of letting it linger until DuckDB's size threshold. It's
  best-effort (a checkpoint that no-ops/aborts is swallowed — the data is already durable in the
  WAL) and targets the workspace catalog by name so the read-only attached sources are untouched.
- **Lineage & replay:** every `query` result upserts a row into the internal
  `_spelunk_meta.lineage` table (a reserved schema, hidden from `catalog`/`drop`): its SQL, `kind`,
  and dependency edges. Deps are found by parsing the SQL (sqlglot) and intersecting table refs with
  the live `(flow, name)` result set — a ref that names an existing result is a dep, anything else is
  an external *source* leaf. `CREATE OR REPLACE` re-derives the row so the store always reflects the
  *current* definition (which permits logical cycles → `replay` topo-sorts and rejects them). This
  makes a flow a reproducible pipeline: `lineage` shows the DAG, `replay(into=...)` rebuilds it
  against (possibly changed) sources. Durable — survives `--shared-workspace` reopen.
- **A fetched endpoint is a result, not a source.** That reframing is what makes one API one
  source: `add_source` an `openapi:` connection once, then `fetch` any endpoint of it. Fetch
  results get a `kind='fetch'` lineage node (deps/sources passed to `_record_lineage` explicitly
  — there is no SQL to parse them out of), so `ids → details → roi` renders end-to-end. **`replay`
  preserves fetch results instead of re-fetching** — they are inputs, like a source: the rows are
  a pinned snapshot, and silently re-issuing N requests inside an operation whose whole purpose is
  deterministic rebuilding would import network latency, rate limits, and a changed upstream. They
  are reported under `preserved` (and copied when rebuilding `into` a fresh flow); refresh =
  `fetch` again.
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

**`--allow-add-source` (off by default):** registers the `add_source` / `remove_source` / `fetch`
tools so the agent can attach and detach sources at runtime and call endpoints of an attached API.
This lets the agent read **any** file or database the server process can reach (including DSNs with
embedded credentials), so only enable it for a trusted setup — and it's sound precisely *because* of
the process-per-agent default: each agent's server is its own process, so an add/remove touches only
that agent's isolated connection and never another's. `fetch` shares this one gate rather than
getting its own flag: agent-initiated network reach is one capability an operator grants or doesn't.
`fetch` is additionally confined to its connection's host + base path (absolute URLs, `..`, and
traversal through a bound `{placeholder}` are all refused), so it reaches strictly *less* than
`add_source` already does.

**Per-process workspace (the default):** `--session-dir` is a *root* (default `./.spelunk_session`,
created if missing, gitignored) and **each server process gets its own durable workspace** at
`<session-dir>/<pid>-<rand>/workspace.duckdb` — so many concurrent servers never collide and never
contend for a single-writer lock. The tool-log defaults alongside it
(`<session-dir>/<pid>-<rand>/tool-calls.jsonl`, dir auto-created). Each run gets a fresh subdir —
isolation, not a shared store across runs. `DuckSession.workspace_dir` is the resolved per-process
dir.

**Workspace GC:** to stop per-process subdirs accumulating, `open()` sweeps on startup — it keeps
the `--keep-workspaces N` most recent (default 3, including the one just created) and reclaims older
subdirs that have no *live* owner. **Empty workspaces are reclaimed even inside the keep window**: a
dir whose DB has no tables outside the reserved schemas and no artifacts beyond a 0-byte tool log
(MCP reconnect churn spawns servers that never handle a call) is garbage, so keep-N doesn't protect
it. Liveness is the DuckDB file lock: the sweep probes each candidate
with a read-write `connect` (a live server holds the single-writer lock → skip; a crashed/exited one
opens → delete). Complementing the sweep, `main()` calls `session.close(reclaim_if_empty=True)` on
clean shutdown (stdin closed), so a no-work server deletes its own dir immediately — after releasing
the tool-log handler first (an open handle blocks `rmtree` on Windows). This is cross-process only — two sessions in one process share DuckDB's cached
instance, so a sweep can't detect an in-process holder (irrelevant in production: each server is its
own process). Dirs younger than a 60s grace window are never touched (a sibling may be mid-startup,
lock not yet held). `--keep-workspaces 0` (or `<=0`) disables the sweep **entirely** — empty-dir
reclamation included; only the clean-shutdown `close(reclaim_if_empty=True)` still fires. The
tool-log lives inside the workspace dir, so it's reclaimed with it — route `--tool-log` elsewhere to
retain history.

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
