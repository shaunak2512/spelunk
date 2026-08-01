# Spelunk

A **multi-source DuckDB query + transformation-pipeline MCP server.** Point it at files
(CSV/Parquet/JSON/Excel/Avro/YAML), databases (SQLite/PostgreSQL/MySQL), and REST/JSON APIs
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
| `spelunk/mcp/views.py` | MCP Apps rendering for `show` (optional `[ui]` extra). |
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
show(name?, kind?, ...)     # [ui extra] DISPLAY it in the chat: an interactive table, a bar/
                            #   line/area/scatter/pie chart, a profile dashboard, the catalog,
                            #   or the lineage DAG as a diagram. A view, never a new result.
```

Discovery resources: `db://tables` (queryable source objects) and `db://{table}` (columns, PK,
sample, row count). A **flow** is an isolated result namespace (a DuckDB schema); give each
concurrent line of analysis its own flow.

### Charts and tables in the chat

Install the `ui` extra and Spelunk registers `show`, which renders results as
[MCP Apps](https://modelcontextprotocol.io/docs/extensions/apps) — interactive components that
appear inline in Claude Desktop, claude.ai, VS Code Copilot, Goose and other UI-capable hosts:

```bash
uvx --from "spelunk-mcp[ui]" spelunk --source sales=./data/sales.parquet
```

```text
query("SELECT region, sum(revenue) AS revenue FROM sales GROUP BY region", "by_region")
show("by_region", kind="bar")        # -> a real bar chart in the conversation
show("by_region", kind="profile")    # -> per-column stats as a dashboard
show(kind="lineage")                 # -> the pipeline DAG, drawn
```

Two things worth knowing. **A view is not a result** — `show` creates no table and records no
lineage, so there is nothing to clean up afterwards. And **aggregate before you show**: charts
cap at 200 rows and error rather than truncating, because a quietly shortened chart misstates
the data. Group in SQL, render the small thing. Hosts that can't display MCP Apps lose nothing —
`show` also returns a text summary, so the tool still works in a terminal.

To see a view without any chat client, render it to a standalone HTML file — Prefab inlines the
whole renderer, so the page opens offline:

```python
from prefab_ui.app import PrefabApp
from spelunk.core.duck import DuckSession
from spelunk.mcp import views

session = DuckSession.open(["sales=./data/sales.parquet"])
session.query("SELECT region, sum(revenue) AS revenue FROM sales GROUP BY region", "by_region")

cols, rows = session.rows_for_display("by_region")
page = PrefabApp(view=views.result_chart("bar", cols, rows)).html(renderer_mode="bundled")
```

> **Claude Desktop note.** Claude Desktop currently strips `structuredContent` from the tool
> result it forwards to an app view ([ext-apps#696](https://github.com/modelcontextprotocol/ext-apps/issues/696)),
> which leaves every MCP App — not just Spelunk's — stuck on "Waiting for content…". Spelunk ships
> a recovery shim in its renderer that re-fetches through the host's `tools/call` proxy, which is
> unaffected. It is a no-op on hosts that deliver the field correctly (claude.ai, MCP Jam).

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
A `.yaml`/`.yml` source reads through DuckDB's `yaml` **community** extension, which the server
installs on first use — that one needs network access (afterward it is cached locally).
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
