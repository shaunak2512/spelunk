# Spelunk

A **multi-source DuckDB query + transformation-pipeline MCP server.** Point it at files
(CSV/Parquet/JSON/Excel), databases (SQLite/PostgreSQL/MySQL), and REST/JSON APIs
(`api:<url>`, snapshotted at attach with pagination + auth), and an agent like Claude Code
can query across all of them — and build step-by-step pipelines — through one DuckDB engine.

## Architecture in one line

A single DuckDB session is both the query engine and the workspace: every source is `ATTACH`ed
(databases) or scanned (files) into one connection, so one query can join a Parquet file to a
Postgres table to a result you built two steps ago — all in DuckDB SQL.

| Path | Role |
|---|---|
| `spelunk/core/duck.py` | `DuckSession` — the one DuckDB connection: query / profile / export / catalog / drop / lineage / replay + introspection. |
| `spelunk/core/sources.py` | Source registry — maps a spec to a DuckDB attach/scan. DuckDB-only: a source it can't attach (e.g. SQL Server) is rejected. |
| `spelunk/core/guard.py` | sqlglot AST safety: read-only enforcement (`assert_read_only`). |
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
lineage(name?, flow?)       # provenance DAG: the SQL + deps that built a result (or a whole flow)
replay(flow?, into?)        # rebuild a flow from its recorded SQL, in dependency order
```

Discovery resources: `db://tables` (queryable source objects) and `db://{table}` (columns, PK,
sample, row count). A **flow** is an isolated result namespace (a DuckDB schema); give each
concurrent line of analysis its own flow.

## Install & run

The fastest path is [`uvx`](https://docs.astral.sh/uv/) — no clone, no venv. It fetches Spelunk
into an ephemeral environment and runs the `spelunk` command. The package is published as
`spelunk-mcp` (the command is `spelunk`), so pass it via `--from`:

```bash
uvx --from spelunk-mcp spelunk \
  --source sales=./data/sales.parquet \
  --source sqlite:///path/to/app.db \
  --session-dir .spelunk_session          # omit for an ephemeral (non-durable) workspace
```

Want the latest commit instead of the released version? Point `uvx` straight at the repo:

```bash
uvx --from git+https://github.com/shaunak2512/spelunk spelunk --source sales=./data/sales.parquet
```

Either way, wire it into Claude Code with a `.mcp.json`:

```json
{
  "mcpServers": {
    "spelunk": {
      "command": "uvx",
      "args": ["--from", "spelunk-mcp", "spelunk", "--source", "sales=./data/sales.parquet"]
    }
  }
}
```

Prefer a local checkout? `python -m spelunk.mcp.server --source ...` is equivalent to the `spelunk`
command.

Sources auto-detect by extension/scheme; prefix with `name=` to set the catalog/view name. A file
path may be a **glob** — `--source trips=./data/yellow_*.parquet` attaches every matching file as
one view, so a partitioned dump is one source, not N. A glob that matches nothing fails loudly.
Optional resource guards: `--memory-limit 4GB`, `--temp-dir <dir>`, `--max-temp-size 50GB`.
DuckDB is out-of-core, so a source larger than `--memory-limit` is the normal case — scans read on
demand and buffering operators spill to the temp directory. Budget roughly 1:1 rather than orders of
magnitude: a streaming aggregate needs a limit near the size of the columns it touches, and a full
sort needs more than that.

## Dev

```bash
uv sync --extra dev
uv run --extra dev python -m pytest -q
uv run --extra dev ruff check spelunk/
```
