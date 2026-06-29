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
import json

from fastmcp import FastMCP

from spelunk.core.duck import DuckSession


def build_server(session: DuckSession) -> FastMCP:
    """Build a FastMCP instance wired to an open :class:`DuckSession`.

    Registers the ``db://`` discovery resources and five tools — ``query``, ``profile``,
    ``export``, ``catalog``, ``drop`` — plus ``import_remote`` when a SQLAlchemy-only source
    (e.g. SQL Server) is configured.
    """
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
    def _profile(sql: str, flow: str = "default") -> dict:
        return session.profile(sql, flow)

    @mcp.tool(
        name="export",
        description=(
            "Write a saved result (by name, e.g. `joined` or \"src\".\"orders\") OR a full SELECT "
            "to a file. Formats: csv, json, parquet. Parent directories are created. No row cap."
        ),
    )
    def _export(target: str, format: str, path: str, flow: str = "default") -> dict:
        return session.export(target, format, path, flow)

    @mcp.tool(
        name="catalog",
        description=(
            "With no argument: list active flows and how many results each holds. With a flow: "
            "list that flow's saved results with their columns, types, and row counts."
        ),
    )
    def _catalog(flow: str | None = None) -> dict:
        return session.catalog(flow)

    @mcp.tool(
        name="drop",
        description=(
            "Delete a saved result (give `name`) or an entire flow and all its results (give "
            "only `flow`). Idempotent; cannot drop reserved schemas."
        ),
    )
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
    args = parser.parse_args()

    specs = list(args.source)
    if args.dsn:
        specs.append(args.dsn)

    session = DuckSession.open(
        specs,
        session_dir=args.session_dir,
        memory_limit=args.memory_limit,
        temp_dir=args.temp_dir,
        max_temp_size=args.max_temp_size,
    )
    server = build_server(session)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
