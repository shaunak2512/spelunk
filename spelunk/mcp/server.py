"""MCP front-end for Spelunk — a multi-source DuckDB query + transformation-pipeline server.

One DuckDB session ([core/duck.py]) is both the query engine and the workspace: files and
attached databases live in it alongside the flow results, so a single ``query`` can join any
of them. No model, no loop — Claude Code is the agent.

Usage (stdio, for Claude Code via .mcp.json)::

    python -m spelunk.mcp.server --source ./data/sales.parquet --source sqlite:///app.db \
        --session-dir .spelunk_session
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from spelunk import __version__
from spelunk.core.duck import DuckSession


class QueryStep(BaseModel):
    """One step of a batch `query` call: a SELECT and the result name it materializes as."""

    sql: str = Field(description="Read-only DuckDB SELECT; may reference earlier steps' names.")
    name: str = Field(description="Result table name this step materializes as (SQL identifier).")

# One JSON line per tool call lands here so agent usage can be analysed offline. Handlers are
# (re)attached by _configure_tool_logging; until then a NullHandler keeps library/test use silent.
_tool_logger = logging.getLogger("spelunk.toolcalls")
_tool_logger.addHandler(logging.NullHandler())
_tool_logger.setLevel(logging.INFO)
_tool_logger.propagate = False

# Args worth recording verbatim (SQL kept full — that's the point of the log); long head samples
# and row payloads are summarised, never dumped. `steps` is a batch of {sql, name} — full SQL kept.
_LOGGED_ARGS = (
    "sql", "name", "flow", "target", "format", "path", "spec", "steps", "into", "dry_run",
)
_LOGGED_RESULT_FIELDS = (
    "name", "flow", "row_count", "format", "path", "dropped_results", "kind",
    "step_count", "completed", "failed_step",
    "root", "source_flow", "target_flow", "dry_run",  # lineage / replay
)
# lineage/replay result lists carry the full SQL of every node — log their size, not their body.
_COUNTED_RESULT_FIELDS = ("columns", "nodes", "edges", "order", "missing", "rebuilt", "plan")

# add_source accepts DSNs that can embed credentials (postgresql://user:pw@host/db); strip the
# userinfo (user:pass@) before the spec is written to the on-disk tool-call log.
_DSN_CREDENTIALS_RE = re.compile(r"//[^/@\s]+@")


def _redact(value: object) -> object:
    """Mask userinfo (user:pass@) in DSN-like strings so credentials never reach the log."""
    if isinstance(value, str):
        return _DSN_CREDENTIALS_RE.sub("//***@", value)
    return value


def _log_arg(key: str, value: object) -> object:
    """Make one logged argument JSON-safe: redact DSN specs, unwrap pydantic step models."""
    if key == "spec":
        return _redact(value)
    if key == "steps" and isinstance(value, list):
        return [s.model_dump() if isinstance(s, BaseModel) else s for s in value]
    return value


def _configure_tool_logging(tool_log: str | None) -> None:
    """Point the tool-call logger at a JSONL file, stderr (``"-"``), or nowhere (``None``).

    Idempotent: clears prior handlers so repeated ``build_server`` calls (e.g. in tests) don't
    stack duplicates. Never logs to stdout — that channel is the stdio MCP transport.
    """
    for handler in list(_tool_logger.handlers):
        _tool_logger.removeHandler(handler)
        handler.close()

    if tool_log is None:
        _tool_logger.addHandler(logging.NullHandler())
        return

    handler: logging.Handler
    if tool_log == "-":
        handler = logging.StreamHandler()  # stderr
    else:
        Path(tool_log).parent.mkdir(parents=True, exist_ok=True)  # create the log dir if missing
        handler = logging.FileHandler(tool_log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _tool_logger.addHandler(handler)


def _summarize_result(result: object) -> dict:
    """Compact, log-safe view of a tool result — counts and identifiers, not full row data."""
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    summary = {k: result[k] for k in _LOGGED_RESULT_FIELDS if k in result}
    for key in _COUNTED_RESULT_FIELDS:
        val = result.get(key)
        if isinstance(val, (list, dict)):
            summary["column_count" if key == "columns" else f"{key}_count"] = len(val)
    return summary


def _logged(fn):
    """Wrap a tool function so each call emits one structured JSON line to ``_tool_logger``.

    Records timestamp, tool name, the salient arguments, outcome (ok/error), a result summary,
    and wall-clock duration. ``functools.wraps`` + the original signature are preserved so
    FastMCP still derives the correct tool schema. The tool name is the function name minus its
    leading underscore (``_query`` → ``query``).
    """
    tool_name = fn.__name__.lstrip("_")
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "args": {
                k: _log_arg(k, v) for k, v in bound.arguments.items() if k in _LOGGED_ARGS
            },
        }
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            record["outcome"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
            _tool_logger.info(json.dumps(record, default=str))
            raise
        record["outcome"] = "ok"
        record["result"] = _summarize_result(result)
        record["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
        _tool_logger.info(json.dumps(record, default=str))
        return result

    return wrapper


def build_server(
    session: DuckSession, tool_log: str | None = None, *, allow_add_source: bool = False
) -> FastMCP:
    """Build a FastMCP instance wired to an open :class:`DuckSession`.

    Registers the ``db://`` discovery resources and the ``query`` / ``profile`` / ``export`` /
    ``catalog`` / ``drop`` / ``lineage`` / ``replay`` tools.

    ``tool_log`` controls per-call logging: a file path writes JSON lines there, ``"-"`` writes
    them to stderr, and ``None`` (the default) is silent.

    ``allow_add_source`` (off by default) additionally registers ``add_source`` / ``remove_source``
    so the agent can attach and detach files and databases at runtime. This lets the agent reach
    any file/DB the host process can — only enable it for a trusted, process-per-agent setup.
    """
    _configure_tool_logging(tool_log)

    source_list = ", ".join(f"{s.name} ({s.kind})" for s in session.sources) or "(none configured)"

    mcp = FastMCP(
        "spelunk",
        version=__version__,
        instructions=(
            "Spelunk is a single DuckDB engine over all your data sources. Files (CSV/Parquet/"
            "JSON/Excel) and attached databases (SQLite/PostgreSQL/MySQL) live in one DuckDB "
            "session together with your saved results, so one query can join across all of "
            f"them. All SQL is DuckDB SQL. Configured sources: {source_list}.\n\n"
            "## Discover\n"
            "- `db://tables` — JSON array of queryable source objects. Attached-database tables "
            "are named `<source>.<table>`, or `<source>.<schema>.<table>` when in a non-default "
            "schema (paste-ready either way); file sources appear as a bare view name.\n"
            "- `db://{table}` — describe one object: columns, types, primary key, a sample, and "
            "a row count. Read this before writing SQL.\n\n"
            "## Query and build\n"
            "- `query(sql, name, flow?)` — run a read-only SELECT over sources AND saved results, "
            "and store the FULL result as table `name` in the flow (no row cap). Returns the "
            "result's columns, true row_count, and a sample — a 5-row head, or EVERY row (with "
            "`complete: true`) when the result is small — so you read a small deliverable without "
            "paging. This is the ONE tool for both "
            "looking and building: every result is named and immediately reusable — reference it "
            "by `name` in your next query. `name` is required; reuse a scratch name (e.g. `tmp`) "
            "for throwaways, or `drop` them. Reference attached DB tables as \"<source>\".\"<table>\", "
            "files and prior results by bare name.\n"
            "- `query(steps=[{sql, name}, ...], flow?)` — the SAME tool in batch mode: an ordered "
            "list of queries executed in one call. Steps may be a dependent chain (later steps "
            "reference earlier steps' names), a bundle of unrelated queries, or a mix — batch "
            "whenever you want more than one result in one round trip, not just for pipelines. "
            "ALWAYS batch instead of firing sequential single calls. Fail-fast: completed steps "
            "stay materialized (with lineage), the failing step reports its error, the rest are "
            "skipped. Every terminal step (one no later step references — the last, plus any "
            "independent query) returns a sample (full rows when small, else a head); "
            "intermediate steps consumed downstream stay compact (row_count + columns).\n"
            "- `profile(sql, flow?)` — per-column stats (null_rate, min/max/mean/std, "
            "p25/p50/p75/p95 for numerics; unique/top/freq for text) over the full result. Use "
            "this instead of writing manual aggregation queries.\n"
            "- `export(target, format, path, flow?)` — write a saved result name OR a full SELECT "
            "to csv/json/parquet (no row cap).\n"
            + (
                "\n## Manage sources\n"
                "- `add_source(spec)` — attach a new data source at runtime. `spec` is a file path "
                "(.csv/.parquet/.json/.xlsx), a SQLite file, or a sqlite:// / postgresql:// / "
                "mysql:// DSN; prefix with `name=` to set the source name (e.g. "
                "`sales=./sales.parquet`). The source becomes queryable in every flow.\n"
                "- `remove_source(name)` — detach a source by its name. Affects this session only.\n"
                if allow_add_source
                else ""
            )
            + "\n## Organize with flows\n"
            "A *flow* is an isolated result namespace (a DuckDB schema; default `\"default\"`). "
            "Give each concurrent line of analysis its own `flow` so results never collide. "
            "Reference a result in another flow as \"<flow>\".\"<name>\".\n"
            "- `catalog()` — list flows and their result counts; `catalog(flow)` — list the "
            "results in a flow with columns and row counts.\n"
            "- `drop(name, flow?)` — drop one result; `drop(flow=...)` with no name — drop a whole "
            "flow.\n\n"
            "## Provenance & replay\n"
            "Every result records the SQL and dependencies that built it, so a flow is a "
            "reproducible pipeline, not a pile of tables.\n"
            "- `lineage(name?, flow?)` — see how results were built: `lineage(name)` gives the "
            "upstream closure that produced one result; `lineage()` gives the whole flow's DAG "
            "(nodes, edges, and a dependency-first order).\n"
            "- `replay(flow?, into?, dry_run?)` — rebuild a flow from its recorded SQL in "
            "dependency order. `replay(flow, into='v2')` rebuilds into a fresh flow "
            "(non-destructive — e.g. after source files change, then diff old vs new); "
            "`replay(flow)` refreshes in place; `dry_run=true` shows the plan first.\n\n"
            "## Notes\n"
            "- All queries are read-only SELECTs (CTEs fine); writes/DDL are rejected at the AST "
            "level. The server materializes your SELECT as a table for you — don't write CREATE/"
            "INSERT yourself.\n"
            "- The sample is the whole result when `complete` is true; otherwise it's a 5-row "
            "preview and the full result is the saved table — `profile` or query it for the whole "
            "set, or `export` it to a file.\n"
            "- Sources are read on demand (DuckDB pushes filters/projections down); filter or "
            "aggregate before materializing rather than copying a whole large table."
        ),
    )

    # --- Resources: source discovery ------------------------------------------------ #
    @mcp.resource("db://tables", name="list_tables", description="List queryable source objects (attached-DB tables + file views).")
    def _list_tables() -> str:
        return json.dumps([obj.model_dump() for obj in session.list_objects()])

    @mcp.resource("db://{table}", name="describe_table", description="Describe one source object: columns, primary key, sample rows, row count.")
    def _describe_table(table: str) -> str:
        return session.describe(table).model_dump_json()

    # --- Tools ----------------------------------------------------------------------- #
    @mcp.tool(
        name="query",
        description=(
            "Run read-only DuckDB SELECTs over sources and saved results, storing each full "
            "result (no row cap) as a named table in the flow for immediate reuse. Two modes: "
            "single (`sql` + `name`) or batch (`steps=[{sql, name}, ...]`). ALWAYS prefer one "
            "batch call over sequential single calls: steps run in list order and a later step "
            "may reference an earlier step's `name` like any saved result. Batch is not only for "
            "pipelines — the steps can be a dependent chain, a bundle of unrelated queries, or a "
            "mix; batch whenever you want more than one result in one round trip. Fail-fast — "
            "completed steps stay materialized, the failing step reports its error, the rest are "
            "skipped. Every terminal step (one no later step references — the last, plus any "
            "independent query) returns a sample — every row with `complete: true` when small, "
            "else a 5-row head; downstream-consumed intermediates return row_count + columns. "
            "Reference attached DB tables as \"<source>\".\"<table>\"; files and prior results by "
            "bare name. Writes/DDL are rejected."
        ),
    )
    @_logged
    def _query(
        sql: str | None = None,
        name: str | None = None,
        steps: list[QueryStep] | None = None,
        flow: str = "default",
    ) -> dict:
        if steps is not None:
            if sql is not None or name is not None:
                raise ValueError(
                    "Pass either sql+name (single query) or steps (batch), not both."
                )
            return session.query_steps([s.model_dump() for s in steps], flow)
        if sql is None or name is None:
            raise ValueError(
                "A single query needs both sql and name; a batch needs "
                "steps=[{sql, name}, ...]."
            )
        return session.query(sql, name, flow)

    @mcp.tool(
        name="profile",
        description=(
            "Run a SELECT and return per-column statistics over the full result, computed in "
            "DuckDB. Numerics: non_null_count, null_rate, min, max, mean, std, p25/p50/p75/p95. "
            "Text: non_null_count, null_rate, unique, top, freq. No row cap."
        ),
    )
    @_logged
    def _profile(sql: str, flow: str = "default") -> dict:
        return session.profile(sql, flow)

    @mcp.tool(
        name="export",
        description=(
            "Write a saved result (by name, e.g. `joined` or \"src\".\"orders\") OR a full SELECT "
            "to a file. Formats: csv, json, parquet. Parent directories are created. No row cap."
        ),
    )
    @_logged
    def _export(target: str, format: str, path: str, flow: str = "default") -> dict:
        return session.export(target, format, path, flow)

    @mcp.tool(
        name="catalog",
        description=(
            "With no argument: list active flows and how many results each holds. With a flow: "
            "list that flow's saved results with their columns, types, and row counts."
        ),
    )
    @_logged
    def _catalog(flow: str | None = None) -> dict:
        return session.catalog(flow)

    @mcp.tool(
        name="drop",
        description=(
            "Delete a saved result (give `name`) or an entire flow and all its results (give "
            "only `flow`). Idempotent; cannot drop reserved schemas."
        ),
    )
    @_logged
    def _drop(name: str | None = None, flow: str = "default") -> dict:
        return session.drop(name, flow)

    @mcp.tool(
        name="lineage",
        description=(
            "Show how results were built: the SQL and dependency edges behind them. With `name`, "
            "return the upstream closure that produced that result (the node plus every result it "
            "transitively depends on, across flows); with no `name`, the whole flow. Returns nodes "
            "(name, kind, sql, deps, sources, created_at), edges, a dependency-first `order`, and "
            "`missing` (deps whose lineage is gone). Read-only."
        ),
    )
    @_logged
    def _lineage(name: str | None = None, flow: str = "default") -> dict:
        return session.lineage(name, flow)

    @mcp.tool(
        name="replay",
        description=(
            "Rebuild a flow's results from their recorded SQL, in dependency order — re-running "
            "each `query`. External inputs (sources, cross-flow "
            "results) must already exist; they are read, not rebuilt. With `into`, rebuild into a "
            "fresh flow (non-destructive — e.g. re-run the pipeline against updated source files, "
            "then compare); without it, refresh in place. `dry_run=true` returns the ordered plan "
            "without executing. Errors on a dependency cycle."
        ),
    )
    @_logged
    def _replay(flow: str = "default", into: str | None = None, dry_run: bool = False) -> dict:
        return session.replay(flow, into, dry_run)

    if allow_add_source:
        @mcp.tool(
            name="add_source",
            description=(
                "Attach a new data source at runtime, then query it like any configured source. "
                "`spec` is a file path (.csv/.parquet/.json/.xlsx), a SQLite file, or a sqlite:// / "
                "postgresql:// / mysql:// DSN; prefix with `name=` to set the source name "
                "(e.g. `sales=./sales.parquet`). Returns the source name, kind, and the objects it "
                "made queryable. The source is visible in every flow of this session."
            ),
        )
        @_logged
        def _add_source(spec: str) -> dict:
            return session.add_source(spec)

        @mcp.tool(
            name="remove_source",
            description=(
                "Detach a data source by its name (as shown by `add_source` or in `db://tables`), "
                "removing it from this session. Idempotent on the underlying detach; errors only if "
                "no source has that name."
            ),
        )
        @_logged
        def _remove_source(name: str) -> dict:
            return session.remove_source(name)

    return mcp


def main() -> None:
    """CLI entry point: build a DuckSession from --source specs, serve over stdio."""
    parser = argparse.ArgumentParser(
        description="Spelunk MCP server — one DuckDB engine over files and databases."
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "A data source, repeatable. A file path (.csv/.parquet/.json/.xlsx), a SQLite file, "
            "or a sqlite:// / postgresql:// / mysql:// DSN. Prefix with name= to set "
            "the source name, e.g. sales=./sales.parquet."
        ),
    )
    parser.add_argument("--dsn", default=None, help="Alias for a single --source (back-compat).")
    parser.add_argument(
        "--session-dir",
        default=".spelunk_session",
        help=(
            "Root directory for durable workspaces (created if missing). Each process gets its "
            "own workspace at <session-dir>/<pid>-<rand>/ so concurrent servers never collide. "
            "Default: ./.spelunk_session."
        ),
    )
    parser.add_argument(
        "--shared-workspace",
        action="store_true",
        help=(
            "Use one shared <session-dir>/workspace.duckdb instead of a per-process subdir. "
            "Results persist across restarts and are visible to a later server on the same dir, "
            "but concurrent servers contend for the single-writer lock (only the first is "
            "durable; the rest fall back to ephemeral)."
        ),
    )
    parser.add_argument(
        "--keep-workspaces",
        type=int,
        default=3,
        metavar="N",
        help=(
            "On startup, keep the N most recent per-process workspaces under --session-dir and "
            "reclaim older ones with no live owner. Empty workspaces (no results, no logged "
            "calls) are reclaimed regardless of N. Default: 3. 0 or less keeps everything. "
            "No-op with --shared-workspace."
        ),
    )
    parser.add_argument(
        "--allow-add-source",
        action="store_true",
        help=(
            "Register the add_source / remove_source tools so the agent can attach and detach "
            "data sources at runtime. This lets the agent read any file/database the server "
            "process can reach — only enable it for a trusted, process-per-agent setup. Off by "
            "default."
        ),
    )
    parser.add_argument("--memory-limit", default=None, help="DuckDB memory_limit, e.g. 4GB.")
    parser.add_argument("--temp-dir", default=None, help="Directory for DuckDB spill files.")
    parser.add_argument("--max-temp-size", default=None, help="Cap on spill size, e.g. 50GB.")
    parser.add_argument(
        "--tool-log",
        default=None,
        metavar="PATH",
        help=(
            "Where to write one JSON line per tool call for usage analysis. A file path appends "
            "there; '-' writes to stderr; 'off' disables. Default: <session-dir>/tool-calls.jsonl "
            "when --session-dir is set, otherwise stderr."
        ),
    )
    args = parser.parse_args()

    specs = list(args.source)
    if args.dsn:
        specs.append(args.dsn)

    session = DuckSession.open(
        specs,
        session_dir=args.session_dir,
        per_process=not args.shared_workspace,
        keep_workspaces=args.keep_workspaces,
        memory_limit=args.memory_limit,
        temp_dir=args.temp_dir,
        max_temp_size=args.max_temp_size,
    )

    # Resolve the tool-log AFTER open(): by default the durable dir is a per-process subdir
    # chosen inside open(), so the default log belongs there, not under the --session-dir root.
    if args.tool_log == "off":
        tool_log: str | None = None
    elif args.tool_log:
        tool_log = args.tool_log
    elif args.session_dir:
        tool_log = str(Path(session.workspace_dir) / "tool-calls.jsonl")
    else:
        tool_log = "-"  # stderr

    server = build_server(session, tool_log=tool_log, allow_add_source=args.allow_add_source)
    try:
        server.run(transport="stdio")
    finally:
        # Clean shutdown (client closed stdin): release the tool-log file handle so the dir is
        # deletable on Windows, then let the session reclaim its own workspace if this run never
        # did any work — reconnect churn then leaves no empty <pid>-<rand> dirs behind.
        _configure_tool_logging(None)
        session.close(reclaim_if_empty=True)


if __name__ == "__main__":
    main()
