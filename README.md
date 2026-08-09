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
| `spelunk/mcp/vega.py` | MCP Apps rendering for `visual` — Vega-Lite spec guard + hydration, and the app page. |
| `tests` | Acceptance tests. |

## Tools

```
query(sql, name, flow?)     # run a read-only SELECT over sources + results; store the full
                            #   result as table `name` for immediate reuse. The ONE tool for
                            #   looking and building — every result is named and chainable.
profile(sql, flow?)         # per-column stats (null_rate, min/max/mean/std, percentiles, top/freq)
export(target, fmt, path)   # write a saved result name OR a full SELECT to csv/json/parquet
catalog(flow?|source?|object?)  # discovery ladder: no arg -> attached sources + flows;
                            #   source= -> that source's queryable objects; object= -> one
                            #   object's columns/sample; flow= -> a flow's results + charts
drop(name?, flow?)          # drop one result, or a whole flow
lineage(name?, flow?)       # provenance DAG: the SQL + deps that built a result (or a whole flow)
replay(flow?, into?)        # rebuild a flow from its recorded SQL, in dependency order
visual(name, spec, ...)     # DRAW a saved result in the chat as an interactive Vega-Lite
                            #   chart. `spec` is a Vega-Lite spec with NO `data` — the server
                            #   injects the rows. A view, never a new result.
```

**Discovery starts at `catalog()`.** With no argument it answers "what data is here?" — the
attached sources and the flows built so far — and each of `source=` / `object=` / `flow=` drills
into one of them (at most one per call). It is a ladder rather than one dump because the
alternative is an opening call that spends the agent's context on every column of every source
before it knows which two tables it needs. The same information is also served as resources for
hosts that use them: `db://tables` (queryable source objects) and `db://{table}` (columns, PK,
sample, row count) — `catalog(object=…)` delegates to the latter, so they cannot disagree.

A **flow** is an isolated result namespace (a DuckDB schema); give each concurrent line of
analysis its own flow.

### Charts in the chat

`visual` renders a saved result as an [MCP App](https://modelcontextprotocol.io/docs/extensions/apps)
— an interactive chart that appears inline in Claude Desktop, claude.ai, VS Code Copilot, Goose
and other UI-capable hosts. No extra to install: a Vega-Lite spec is JSON and the app page is a
string, so the display surface ships in the core package.

```text
query("SELECT region, sum(revenue) AS revenue FROM sales GROUP BY region", "by_region")
visual("by_region", {"mark": "bar", "encoding": {
    "x": {"field": "region", "type": "nominal"},
    "y": {"field": "revenue", "type": "quantitative"}}})
```

You write a [Vega-Lite](https://vega.github.io/vega-lite/) spec **without a `data` key** — the
server injects the result's rows for you. That is the whole point: instead of picking from a
fixed menu of chart kinds, you get the full grammar (layering, faceting, binning, tooltips,
`params` selections for brushing and cross-filtering) and the data never has to be named twice.

Three things worth knowing:

- **A view is not a result.** `visual` creates no table and records no lineage, so there is
  nothing to clean up afterwards.
- **Author a chart once, then redraw it.** `save_as="revenue"` stores the spec; every look after
  that is `visual(saved="revenue")`. The stored spec holds no data, so a redraw shows whatever
  the result contains *now* — rebuild the pipeline and the chart is current, with nothing to
  re-send. A redraw re-checks the spec against the result's current columns, so a renamed column
  is an error naming the field rather than a chart that quietly misstates the data.
- **Every `field` is checked against the real schema** before anything renders. Vega-Lite draws a
  misspelled field as a blank chart *silently*; Spelunk errors instead and names the columns you
  actually have. Fields your own `transform` creates are fine.
- **Aggregate first.** Capped at 5000 rows, erroring rather than truncating, because a quietly
  shortened chart misstates the data. Group in SQL, draw the small thing.

Hosts that can't display MCP Apps lose nothing — `visual` also returns a text summary (with the
rows themselves when the result is small), so the tool still works in a terminal.

> **Claude Desktop note.** Claude Desktop currently strips `structuredContent` from the tool
> result it forwards to an app view ([ext-apps#696](https://github.com/modelcontextprotocol/ext-apps/issues/696)),
> which leaves every MCP App — not just Spelunk's — stuck with no data. Spelunk's app recovers by
> re-fetching through the host's `tools/call` proxy, which is unaffected. It is a no-op on hosts
> that deliver the field correctly (claude.ai, MCP Jam).

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

### HTTP transport

By default Spelunk speaks MCP over **stdio**, which is what the command-launched wiring above
expects. Pass `--transport http` to serve **streamable HTTP** on a port instead, for a client that
connects to a URL:

```bash
spelunk --transport http --source sales=./data/sales.parquet
# -> http://127.0.0.1:8080/mcp
```

```json
{
  "mcpServers": {
    "spelunk": { "type": "http", "url": "http://127.0.0.1:8080/mcp" }
  }
}
```

`--host` (default `127.0.0.1`, loopback only), `--port` (default `8080`) and `--http-path` (default
`/mcp`) control the bind. The tool and resource surface is identical on both transports.

Each tool call prints one line to the terminal beside FastMCP's own log — the call and its
arguments, then `ok` with a row count, or `ERROR` with the database's message and the SQL that
caused it:

```
INFO   query(name='top_albums' flow='default') ok in 18.3ms — 1240 rows
ERROR  query(flow='default' steps=3) step 2/3 'scores' failed in 4.1ms — Catalog Error: Table with
       name reviws does not exist! Did you mean "reviews"? [1 completed, 1 skipped]
           sql: SELECT artist, AVG(score) FROM reviws GROUP BY 1
```

That second line is the point: a batch is fail-fast but *returns* rather than raising, so without
it a failed step is invisible — uvicorn logs `POST /mcp 200 OK` either way. Use
`--console-log off` to silence it, or `--tool-log` for the machine-readable JSONL of the same calls.

Requests are screened for **DNS rebinding**: an `Origin` header, when present, must name this
endpoint, and on a loopback bind the `Host` header must too — otherwise the request is refused with
**403**. A normal MCP client sends no `Origin` and is unaffected; this stops a web page the user
happens to visit from driving the server on `127.0.0.1`. Use `--allowed-origin` (repeatable) to
permit a browser client on a non-loopback bind.

One caveat worth knowing: stdio in the default per-process mode gives every agent its own server
process and its own workspace, while an HTTP endpoint serves **all** connecting clients from one
session — so flows and results are shared between them. (`--shared-workspace` gives up that
isolation on stdio too, pointing every process at one workspace file.) Combining `--transport http`
with `--allow-add-source` gives any client that can reach the port the ability to attach any file or
DSN the server process can, which is why that combination warns on startup.

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
