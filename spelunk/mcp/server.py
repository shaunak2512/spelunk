"""MCP front-end for Spelunk — exposes core introspect + query over FastMCP.

Usage (stdio, for Claude Code via .mcp.json):
    python -m spelunk.mcp.server --dsn sqlite:///path/to.db

No model, no loop — Claude Code is the agent.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from spelunk.core.introspect import describe, list_objects
from spelunk.core.query import run_sql

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_DIALECT_LABELS = {
    "sqlite": "SQLite",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mssql": "SQL Server",
}


def build_server(engine: "Engine") -> FastMCP:
    """Build and return a FastMCP instance wired to *engine*.

    Registers:
    - Resource  ``db://tables``        — lists all tables/views (wraps list_objects).
    - Template  ``db://{table}``       — describes one table (wraps describe).
    - Tool      ``run_query(sql)``     — executes a read-only SQL query (wraps run_sql).
    """
    dialect_name = engine.dialect.name
    dialect_label = _DIALECT_LABELS.get(dialect_name, dialect_name)

    mcp = FastMCP(
        "spelunk",
        instructions=(
            f"Spelunk exposes a connected {dialect_label} database for read-only exploration and querying.\n\n"
            "## Resources\n"
            "- `db://tables` — call this first to get a JSON array of all tables and views "
            "(each entry has `name`, `kind`, and `row_count`).\n"
            "- `db://{table}` — describe a single table by name: columns with types and nullability, "
            "primary key, foreign-key relationships, a few sample rows, and per-column value profiles. "
            "Use this to understand schema details before writing SQL.\n\n"
            "## Tool\n"
            "- `run_query(sql)` — execute a read-only SELECT query and get back rows as a list of dicts. "
            "Results are capped at 1 000 rows. Writes (INSERT/UPDATE/DELETE/DDL/PRAGMA writes) are "
            "blocked at the AST level and will raise an error.\n\n"
            "## Recommended workflow\n"
            "1. Read `db://tables` to discover what tables exist.\n"
            "2. Read `db://{table}` for each table relevant to the question — pay attention to "
            "foreign keys to understand join paths and to column profiles for value distributions.\n"
            "3. Draft a SELECT query. Prefer explicit column lists over `SELECT *`.\n"
            "4. Call `run_query` with your SQL. If the result is empty or unexpected, inspect the "
            "sample rows from step 2 and adjust.\n\n"
            "## Constraints\n"
            "- All queries must be read-only SELECT statements (CTEs are fine).\n"
            "- Row limit is 1 000 per query; use `WHERE`, `LIMIT`, or aggregation to stay within it.\n"
            f"- The connected database is {dialect_label} — write SQL in its dialect."
        ),
    )

    # ------------------------------------------------------------------ #
    # Resource: list all tables
    # ------------------------------------------------------------------ #
    @mcp.resource("db://tables", name="list_tables", description="List all tables and views in the database.")
    def _list_tables() -> str:
        """Return a JSON array of {name, kind, row_count} objects."""
        objects = list_objects(engine)
        return json.dumps([obj.model_dump() for obj in objects])

    # ------------------------------------------------------------------ #
    # Resource template: describe a single table
    # ------------------------------------------------------------------ #
    @mcp.resource(
        "db://{table}",
        name="describe_table",
        description="Describe a table: columns, primary key, foreign keys, sample rows, and column profile.",
    )
    def _describe_table(table: str) -> str:
        """Return a JSON object describing *table*."""
        desc = describe(engine, table)
        return desc.model_dump_json()

    # ------------------------------------------------------------------ #
    # Tool: run a read-only SQL query
    # ------------------------------------------------------------------ #
    @mcp.tool(
        name="run_query",
        description=(
            "Execute a read-only SQL query against the database. "
            "Writes (INSERT/UPDATE/DELETE/DDL) are rejected. "
            "Results are capped at 1000 rows."
        ),
    )
    def _run_query(sql: str) -> dict:
        """Execute *sql* and return a QueryResult-shaped dict."""
        result = run_sql(engine, sql)
        return result.model_dump()

    return mcp


# --------------------------------------------------------------------------- #
# __main__ entry-point (stdio transport for Claude Code via .mcp.json)
# --------------------------------------------------------------------------- #
def main() -> None:
    """CLI entry point: read --dsn, build server, serve over stdio."""
    import argparse

    from spelunk.core.connection import connect

    parser = argparse.ArgumentParser(
        description="Spelunk MCP server — exposes spelunk.core over the MCP protocol."
    )
    parser.add_argument(
        "--dsn",
        required=True,
        help="SQLAlchemy DSN, e.g. sqlite:///path/to.db",
    )
    args = parser.parse_args()

    engine = connect(args.dsn)
    server = build_server(engine)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
