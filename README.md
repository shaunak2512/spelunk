# Spelunk

A **multi-source DuckDB query + transformation-pipeline MCP server.** Point it at files
(CSV/Parquet/JSON/Excel) and databases (SQLite/PostgreSQL/MySQL), and an agent like Claude Code
can query across all of them — and build step-by-step pipelines — through one DuckDB engine.

## Architecture in one line

A single DuckDB session is both the query engine and the workspace: every source is `ATTACH`ed
(databases) or scanned (files) into one connection, so one query can join a Parquet file to a
Postgres table to a result you built two steps ago — all in DuckDB SQL.

| Path | Role |
|---|---|
| `spelunk/core/duck.py` | `DuckSession` — the one DuckDB connection: query / profile / export / catalog / drop + introspection. |
| `spelunk/core/sources.py` | Source registry — maps a spec to a DuckDB attach/scan; SQLAlchemy fallback for SQL Server. |
| `spelunk/core/guard.py` | sqlglot AST safety: read-only enforcement (`assert_read_only`). |
| `spelunk/core/connection.py`, `query.py`, `introspect.py` | Retained SQLAlchemy path, used only by the SQL Server / exotic `import_remote` fallback. |
| `spelunk/mcp/server.py` | Thin FastMCP wrapper over `DuckSession`. |
| `tests` | Acceptance tests. |

## Tools

```
query(sql, name, flow?)     # run a read-only SELECT over sources + results; store the full
                            #   result as table `name` for immediate reuse. The ONE tool for
                            #   looking and building — every result is named and chainable.
profile(sql, flow?)         # per-column stats (null_rate, min/max/mean/std, percentiles, top/freq)
export(target, fmt, path)   # write a saved result name OR a full SELECT to csv/json/parquet
catalog(flow?)              # list flows, or the results in one flow
drop(name?, flow?)          # drop one result, or a whole flow
import_remote(sql, name)    # (only with a SQL Server source) pull a SELECT into the workspace
```

Discovery resources: `db://tables` (queryable source objects) and `db://{table}` (columns, PK,
sample, row count). A **flow** is an isolated result namespace (a DuckDB schema); give each
concurrent line of analysis its own flow.

## Run it

```bash
python -m spelunk.mcp.server \
  --source sales=./data/sales.parquet \
  --source sqlite:///path/to/app.db \
  --session-dir .spelunk_session          # omit for an ephemeral (non-durable) workspace
```

Sources auto-detect by extension/scheme; prefix with `name=` to set the catalog/view name.
Optional resource guards: `--memory-limit 4GB`, `--temp-dir <dir>`, `--max-temp-size 50GB`.
DuckDB is out-of-core, so a source larger than RAM is the normal case — scans read on demand and
buffering operators spill to the temp directory.

A `.mcp.json` in the repo root wires Claude Code to a local source (edit the paths for your
machine before use).

## Dev

```bash
uv sync --extra dev
uv run --extra dev python -m pytest -q
uv run --extra dev ruff check spelunk/
```
