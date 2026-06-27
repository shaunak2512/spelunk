"""MCP front-end for Spelunk — exposes core introspect + query over FastMCP.

Usage (stdio, for Claude Code via .mcp.json):
    python -m spelunk.mcp.server --dsn sqlite:///path/to.db

No model, no loop — Claude Code is the agent.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pandas as pd
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
            "## Tools\n"
            "- `run_query(sql)` — execute a read-only SELECT query and get back rows as a list of dicts. "
            "Results are capped at 1 000 rows. Writes (INSERT/UPDATE/DELETE/DDL/PRAGMA writes) are "
            "blocked at the AST level and will raise an error.\n"
            "- `export_query(sql, format, path, timeout_s?)` — run a query with no row cap and write "
            "the full result to a file. Supported formats: `csv`, `json`, `parquet` (parquet requires "
            "pyarrow). `path` is an absolute or relative file path; parent directories are created "
            "automatically. Default timeout is 300 s.\n\n"
            "## Recommended workflow\n"
            "1. Read `db://tables` to discover what tables exist.\n"
            "2. Read `db://{table}` for each table relevant to the question — pay attention to "
            "foreign keys to understand join paths and to column profiles for value distributions.\n"
            "3. Draft a SELECT query. Prefer explicit column lists over `SELECT *`.\n"
            "4. Call `run_query` with your SQL. If the result is empty or unexpected, inspect the "
            "sample rows from step 2 and adjust.\n\n"
            "## Constraints\n"
            "- All queries must be read-only SELECT statements (CTEs are fine).\n"
            "- `run_query` is capped at 1 000 rows; use `export_query` for full result sets.\n"
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

    # ------------------------------------------------------------------ #
    # Tool: export a query to a file (no row cap, extended timeout)
    # ------------------------------------------------------------------ #
    @mcp.tool(
        name="export_query",
        description=(
            "Execute a read-only SQL query and export the full result set to a file. "
            "No row limit is enforced — use this for large result sets. "
            "Supported formats: csv, json, parquet (parquet requires pyarrow). "
            "Parent directories are created automatically. "
            "Writes (INSERT/UPDATE/DELETE/DDL) are rejected."
        ),
    )
    def _export_query(sql: str, format: str, path: str, timeout_s: int = 300) -> dict:
        """Run *sql* and write all rows to *path* in *format* (csv/json/parquet)."""
        fmt = format.lower().strip()
        if fmt not in ("csv", "json", "parquet"):
            raise ValueError(f"Unsupported format {fmt!r}. Choose csv, json, or parquet.")

        result = run_sql(engine, sql, max_rows=None, timeout_s=timeout_s)
        df = pd.DataFrame(result.rows, columns=result.columns)

        abs_path = os.path.abspath(path)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if fmt == "csv":
            df.to_csv(abs_path, index=False)
        elif fmt == "json":
            df.to_json(abs_path, orient="records", indent=2, force_ascii=False)
        elif fmt == "parquet":
            import duckdb
            # DuckDB accepts forward slashes on Windows; escape any literal quotes.
            safe_path = abs_path.replace("\\", "/").replace("'", "''")
            with duckdb.connect() as dconn:
                dconn.register("_export", df)
                dconn.execute(f"COPY _export TO '{safe_path}' (FORMAT PARQUET)")

        return {
            "path": abs_path,
            "format": fmt,
            "row_count": result.row_count,
            "columns": result.columns,
            "elapsed_s": round(result.elapsed_s, 3),
        }

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
