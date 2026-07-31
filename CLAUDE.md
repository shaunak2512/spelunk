# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Spelunk is

A **multi-source DuckDB query + transformation-pipeline MCP server.** Point it at files
(CSV/Parquet/JSON/Excel/Avro/YAML) and databases (SQLite/PostgreSQL/MySQL), and an agent (Claude
Code) can query across all of them and build step-by-step pipelines through one DuckDB engine.

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
                 #   https://,s3://,gs://,az:// via httpfs/azure ext; ext-backed readers
                 #   excel/avro/yaml — yaml is a *community* extension, so _COMMUNITY_EXTS makes
                 #   its INSTALL carry FROM community),
                 #   sqlite/postgres/mysql, delta:/iceberg: (delta_scan/iceberg_scan VIEWs),
                 #   ducklake:, api: (ONE REST/JSON endpoint fetched at attach into an NDJSON
                 #   snapshot under <workspace>/snapshots/, view over the snapshot — queries never
                 #   re-fetch; refresh = re-attach), openapi: (the WHOLE API — catalog view +
                 #   Source.connection, an ApiConnection the `fetch` tool calls any endpoint of).
                 #   Every JSON snapshot (api: view, openapi: catalog, and fetch results via
                 #   _snapshot_scan) is read with _JSON_SNAPSHOT_OPTS = sample_size=-1 +
                 #   map_inference_threshold=-1: DuckDB's sampled defaults otherwise infer from
                 #   the first 20480 rows (a field first appearing later kills the scan with
                 #   `unknown key`) and type a >200-key object as MAP(VARCHAR,<one type>) (which
                 #   can't hold mixed values, and won't union with the STRUCT a sibling snapshot
                 #   inferred — the `Could not convert string 'x@y.gov' to INT128` trap). Both
                 #   cost nothing measurable on a local file. json=true opts a snapshot out of
                 #   inference entirely (one raw JSON column) for genuinely polymorphic payloads.
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
                 #   max_pages/max_rows caps. EVERY paging param NAME is configurable —
                 #   page_param/offset_param/size_param/page_size/start/cursor_param/cursor_path/
                 #   keyset_field — so page/offset/limit are conventions, not requirements, and an
                 #   API with its own vocabulary (startIndex/resultsPerPage, startAt/maxResults)
                 #   is one source, not N hand-paged ones. Sending an unrecognized paging param
                 #   isn't always harmless: some APIs reject the request outright.
                 #   Auth: auth_env=<ENV> (Bearer), header=<Name>:<ENV>
                 #   (any header, e.g. X-Api-Key), param=<name>:<ENV> (query-param keys) — specs
                 #   carry env var NAMES, never values; injected values are scrubbed from error
                 #   messages ($ENV placeholder). Retries 429/5xx with
                 #   backoff + Retry-After; a SERVER-SUPPLIED next-page URL (a cursor_path value,
                 #   @odata.nextLink, or Link: rel="next") is followed only while it stays on the
                 #   API's OWN ORIGIN — the credential headers resolve once and are reused for every
                 #   page, so an off-origin `next` would hand the token to whatever host the response
                 #   names; relative refs still resolve normally, and cross-origin HTTP redirects are
                 #   refused the same way (urllib re-sends headers across them).
                 #   Repeat-page guard stops APIs that ignore page params;
                 #   a 404 mid-pagination = end-of-data (TVMaze-style), on page 1 = error;
                 #   fetch fingerprint (url/fetched_at/pages/row_count) returned as Source.info.
                 #   ALSO the connection layer: ApiConnection (base URL + creds + shared
                 #   conventions, built by an openapi: source) + ApiRequest (path/params/options)
                 #   -> resolve_request() binds them into the SAME ApiSpec, so pagination/auth/
                 #   record extraction have one implementation. Param layering: inline query <
                 #   conn.params < call params; pagination-managed and credential-named params
                 #   are RESERVED (error, never a silent override); {placeholder}s consume their
                 #   param and are percent-encoded as single segments (a bound value can't
                 #   redirect the request) — PATH SEGMENTS ONLY: one in the query string
                 #   (?language={lang}) is refused, since nothing substitutes it and it would
                 #   otherwise reach the API as the literal %7Blang%7D.
                 #   fetch_fanout() = one URL per prep-query row through a
                 #   thread pool sharing one HostLimiter (a 429 backs off every worker),
                 #   _key_<placeholder> stamped for the join back, per-entity 404 = data
                 #   (skipped + reported), 401/other = abort. max_urls is a HARD ERROR.
                 #   Live smoke-check: tests/live_api_check.py (manual; hits real public APIs).
  openapi.py     # openapi:<url-or-path> -> queryable ENDPOINT CATALOG (one row per path+method
                 #   — method LOWERCASE, matching the document's own keys): params (with the
                 #   EFFECTIVE style/explode, which decides list-param serialization), auth shape
                 #   mapped onto auth_env=/header=/param=, pagination/records hints (when no
                 #   paging CONVENTION matches, pagination_hint still names the paging-shaped
                 #   params the endpoint declares — "unknown; endpoint declares startIndex,
                 #   resultsPerPage — set …" — because the vocabulary list can never be complete
                 #   and going silent is what pushes an agent into hand-paging; named as a hint,
                 #   never guessed into suggested_spec),
                 #   response_fields (what the endpoint RETURNS — {name,type} structs flattened 2
                 #   levels: a.b for nested objects, a[].b for arrays of objects — so an agent can
                 #   find an endpoint by the data it carries, not just its URL), and a paste-ready
                 #   suggested_spec api: string for GETs with <SET_ME> where the credential env
                 #   var name goes. records_hint uses an ENVELOPE test (conventional wrapper key,
                 #   else a small object with one array beside scalars) — first-array-wins called
                 #   TMDB's /movie/{id} a list of genres. Guidance-as-data: the agent SQL-queries
                 #   the catalog, fills <SET_ME>, feeds suggested_spec to add_source.
                 #   OpenAPI 3.x JSON only (YAML/Swagger2 rejected with conversion pointers).
                 #   The connection's base is servers[0].url, resolved against the spec's own URL
                 #   when relative — which only works for a spec fetched over http(s). A LOCAL spec
                 #   with a relative server (Petstore's "/api/v3") has no host to resolve against, so
                 #   the source refuses to attach and names the fix: `base_url=<url>`, an option that
                 #   overrides the declared server (also how you point a spec at staging).
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
  `temp_directory`, so a source larger than `memory_limit` is the normal case, not a failure.
  Measured headroom is roughly 1:1, not orders of magnitude (see WSP-013): a streaming
  aggregate over ~400MB of columns needs ~384MB, and a full sort of the same data still OOMs
  at 512MB whatever the temp settings.
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

`--source` is repeatable and auto-detects by extension/scheme (a `<your-name>=` prefix sets the
catalog/view name — the word before the `=` IS the name, e.g. `sales=`; writing the placeholder
literally as `name=sales <locator>` parses as the name `name` plus an unclassifiable locator, and
`detect_kind` says so rather than blaming the locator). A file locator may be a **glob** —
`trips=./data/yellow_*.parquet` is one view over every match, so a partitioned dump is one source
rather than N; a glob matching nothing is an error, not an empty view. Optional guards: `--memory-limit`, `--temp-dir`, `--max-temp-size`. `--dsn` is a back-compat
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
summary, duration_ms) for usage analysis. DSN credentials are masked before anything is written
— in the `spec` arg *and* in the `error` field, in both the URL form (`//user:pass@`) and the
libpq keyword form (`password=…`) that DuckDB reports back on a failed ATTACH. Redacting only
the arg leaks the secret on the error path, which is where a connection failure quotes the whole
DSN back at you — wired by the `@_logged` decorator in `mcp/server.py`,
which preserves each function's signature so FastMCP's schema is unchanged. `--tool-log` controls
the sink: a file path, `-` for stderr, or `off` to disable. Default: `<session-dir>/tool-calls.jsonl`
when `--session-dir` is set, else stderr — never stdout (that's the stdio MCP transport). Library
callers of `build_server(session)` log nowhere unless passed `tool_log=`. The JSONL is queryable by
Spelunk itself via `read_json_auto(...)`.

## Claim register (`evals/`)

The docs are the spec: `evals/claims/*.yaml` turns every falsifiable assertion in README.md /
CLAUDE.md / the tool descriptions into a numbered claim with the observation that would prove it
false, its falsification mode (invariant / behavioral / quantitative / affordance), and the tests
that would fail if it were false. `python evals/coverage.py [--strict|--unverified]` validates
the register and reports coverage; `--strict` fails on an unresolvable `covered_by` node id, a
`verified` claim with no test, or a non-verified claim with no stated gap.

When you change behaviour, update the claim — a `refuted` status means either the code or the doc
is wrong, and the register forces the choice instead of letting it drift. `affordance` claims
(what an *agent* does when handed this surface) cannot be closed by unit tests at all; they need
an agent harness with an ablation arm, and they are why one is worth building.

Tests needing a live Postgres/MySQL (`tests/test_multi_source.py`) resolve a server in order:
`SPELUNK_TEST_POSTGRES_DSN` / `SPELUNK_TEST_MYSQL_DSN`, else a throwaway Docker container the
fixture starts and kills, else skip. Docker running = the full suite has zero skips.

Traps worth knowing before writing tests here:
- **Drain both pipes.** `tests/test_cli.py` drives `main()` as a real subprocess over stdio;
  read stdout **and** stderr concurrently, or a ~4KB Windows pipe buffer fills on an error path
  and the server blocks mid-response, looking exactly like a hang.
- **`DuckDBPyConnection.execute` is read-only** — it cannot be monkeypatched. Swap `_con` for a
  forwarding proxy instead (`tests/test_workspace.py::_ConnectionProxy`).
- **`_materialize_query` takes `_lock` internally**, so instrumenting it samples *outside* the
  critical section and reports overlaps that aren't real. Probe concurrency at the connection.
- **Memory-limit tests need calibration, not guesses.** DuckDB's usable floor is close to the
  data size; run any new limit 3x before asserting on it.

## After making a change

1. `uv run --extra dev python -m pytest -q` — green.
2. `.\.venv\Scripts\python.exe -m pytest --co -q` — clean collection.
3. `uv run --extra dev ruff check spelunk/`.
4. Commit with co-author attribution; do not push unless asked.
