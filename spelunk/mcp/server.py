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
import time
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

from spelunk.core.duck import DuckSession

# One JSON line per tool call lands here so agent usage can be analysed offline. Handlers are
# (re)attached by _configure_tool_logging; until then a NullHandler keeps library/test use silent.
_tool_logger = logging.getLogger("spelunk.toolcalls")
_tool_logger.addHandler(logging.NullHandler())
_tool_logger.setLevel(logging.INFO)
_tool_logger.propagate = False

# Args worth recording verbatim (SQL kept full — that's the point of the log); long head samples
# and row payloads are summarised, never dumped.
_LOGGED_ARGS = ("sql", "name", "flow", "target", "format", "path")
_LOGGED_RESULT_FIELDS = ("name", "flow", "row_count", "format", "path", "dropped_results")


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
        handler = logging.FileHandler(tool_log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _tool_logger.addHandler(handler)


def _summarize_result(result: object) -> dict:
    """Compact, log-safe view of a tool result — counts and identifiers, not full row data."""
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    summary = {k: result[k] for k in _LOGGED_RESULT_FIELDS if k in result}
    cols = result.get("columns")
    if isinstance(cols, (list, dict)):
        summary["column_count"] = len(cols)
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
            "args": {k: v for k, v in bound.arguments.items() if k in _LOGGED_ARGS},
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


def build_server(session: DuckSession, tool_log: str | None = None) -> FastMCP:
    """Build a FastMCP instance wired to an open :class:`DuckSession`.

    Registers the ``db://`` discovery resources and five tools — ``query``, ``profile``,
    ``export``, ``catalog``, ``drop`` — plus ``import_remote`` when a SQLAlchemy-only source
    (e.g. SQL Server) is configured.

    ``tool_log`` controls per-call logging: a file path writes JSON lines there, ``"-"`` writes
    them to stderr, and ``None`` (the default) is silent.
    """
    _configure_tool_logging(tool_log)

    source_list = ", ".join(f"{s.name} ({s.kind})" for s in session.sources) or "(none configured)"
    has_fallback = bool(session.fallback_sources)

    mcp = FastMCP(
        "spelunk",
        instructions=(
            "Spelunk is a single DuckDB engine over all your data sources. Files (CSV/Parquet/"
            "JSON/Excel) and attached databases (SQLite/PostgreSQL/MySQL) live in one DuckDB "
            "session together with your saved results, so one query can join across all of "
            f"them. All SQL is DuckDB SQL. Configured sources: {source_list}.\n\n"
            "## Discover\n"
            "- `db://tables` — JSON array of queryable source objects. Attached-database tables "
            "are named `<source>.<table>` (paste-ready); file sources appear as a bare view name.\n"
            "- `db://{table}` — describe one object: columns, types, primary key, a sample, and "
            "a row count. Read this before writing SQL.\n\n"
            "## Query and build\n"
            "- `query(sql, name, flow?)` — run a read-only SELECT over sources AND saved results, "
            "and store the FULL result as table `name` in the flow (no row cap). Returns the "
            "result's columns, true row_count, and a head sample. This is the ONE tool for both "
            "looking and building: every result is named and immediately reusable — reference it "
            "by `name` in your next query. `name` is required; reuse a scratch name (e.g. `tmp`) "
            "for throwaways, or `drop` them. Reference attached DB tables as \"<source>\".\"<table>\", "
            "files and prior results by bare name.\n"
            "- `profile(sql, flow?)` — per-column stats (null_rate, min/max/mean/std, "
            "p25/p50/p75/p95 for numerics; unique/top/freq for text) over the full result. Use "
            "this instead of writing manual aggregation queries.\n"
            "- `export(target, format, path, flow?)` — write a saved result name OR a full SELECT "
            "to csv/json/parquet (no row cap).\n"
            + (
                "- `import_remote(sql, name, flow?)` — a SQL Server / SQLAlchemy-only source can't "
                "be attached, so pull a SELECT from it into the flow as table `name`, then query "
                "that table normally.\n"
                if has_fallback
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
            "## Notes\n"
            "- All queries are read-only SELECTs (CTEs fine); writes/DDL are rejected at the AST "
            "level. The server materializes your SELECT as a table for you — don't write CREATE/"
            "INSERT yourself.\n"
            "- The head sample is a preview; the full result is the saved table — `profile` or "
            "query it for the whole set, or `export` it to a file.\n"
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
            "Run a read-only DuckDB SELECT over sources and saved results, and store the full "
            "result (no row cap) as table `name` in the flow for immediate reuse. Returns "
            "columns, true row_count, and a head sample. `name` is required. Reference attached "
            "DB tables as \"<source>\".\"<table>\"; files and prior results by bare name. Writes/DDL "
            "are rejected."
        ),
    )
    @_logged
    def _query(sql: str, name: str, flow: str = "default") -> dict:
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

    if has_fallback:
        @mcp.tool(
            name="import_remote",
            description=(
                "Pull a read-only SELECT from a SQL Server / SQLAlchemy-only source (which DuckDB "
                "cannot attach) into the flow as table `name`, then query it normally. No row cap."
            ),
        )
        @_logged
        def _import_remote(sql: str, name: str, flow: str = "default") -> dict:
            return session.import_remote(sql, name, flow)

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
            "or a sqlite:// / postgresql:// / mysql:// / mssql:// DSN. Prefix with name= to set "
            "the source name, e.g. sales=./sales.parquet."
        ),
    )
    parser.add_argument("--dsn", default=None, help="Alias for a single --source (back-compat).")
    parser.add_argument(
        "--session-dir",
        default=None,
        help="Directory for a durable workspace (results survive restarts). Omit for ephemeral.",
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

    if args.tool_log == "off":
        tool_log: str | None = None
    elif args.tool_log:
        tool_log = args.tool_log
    elif args.session_dir:
        tool_log = str(Path(args.session_dir) / "tool-calls.jsonl")
    else:
        tool_log = "-"  # stderr

    session = DuckSession.open(
        specs,
        session_dir=args.session_dir,
        memory_limit=args.memory_limit,
        temp_dir=args.temp_dir,
        max_temp_size=args.max_temp_size,
    )
    server = build_server(session, tool_log=tool_log)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
