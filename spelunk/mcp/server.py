"""MCP front-end for Spelunk — exposes core introspect + query over FastMCP.

Usage (stdio, for Claude Code via .mcp.json):
    python -m spelunk.mcp.server --dsn sqlite:///path/to.db

No model, no loop — Claude Code is the agent.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import text as _sa_text

import pandas as pd
from fastmcp import FastMCP

from spelunk.core import guard
from spelunk.core.introspect import describe, list_objects
from spelunk.core.query import run_sql

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# Dialects that support STDDEV (or equivalent) as a plain aggregate function.
_STDDEV_DIALECTS = {"postgresql", "mysql", "mariadb", "mssql", "oracle", "duckdb"}
# Different engines spell the function differently.
_STDDEV_FN: dict[str, str] = {"mssql": "STDEV", "mysql": "STD", "mariadb": "STD"}
# Dialects without a built-in STDDEV; compute population std via SQRT(ABS(AVG(x²) - AVG(x)²)).
_MANUAL_STDDEV_DIALECTS = {"sqlite"}

# Dialects with PERCENTILE_CONT as an ordered-set aggregate (returns a scalar in
# a plain SELECT).  MySQL/SQL Server expose it only as a window function, which
# can't be mixed cleanly with other aggregates, so we omit them here.
_PERCENTILE_DIALECTS = {"postgresql", "oracle", "duckdb"}


def _quote_col(name: str, dialect: str) -> str:
    """Return a properly-quoted column identifier for the given dialect."""
    if dialect in ("mysql", "mariadb"):
        return f"`{name.replace('`', '``')}`"
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def _detect_numeric(columns: list[str], rows: list[list]) -> set[str]:
    """Classify columns as numeric by inspecting the first non-null sample value."""
    from decimal import Decimal
    numeric: set[str] = set()
    for idx, col in enumerate(columns):
        for row in rows:
            val = row[idx]
            if val is not None:
                if isinstance(val, (int, float, Decimal)) and not isinstance(val, bool):
                    numeric.add(col)
                break  # first non-null value determines the type
    return numeric


def _to_python(v: Any) -> Any:
    """Convert a DB driver value to a JSON-serialisable Python type."""
    if v is None:
        return None
    import math
    from decimal import Decimal
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, (int, float)):
        return v
    try:
        return v.item()  # numpy scalar — export_query still uses pandas
    except AttributeError:
        return str(v)


_DIALECT_LABELS = {
    "sqlite": "SQLite",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mssql": "SQL Server",
}

# Valid workspace result names: SQL-identifier-safe so they can be interpolated
# into CREATE TABLE / quoted references without injection risk. This is also why
# the LLM addresses results by NAME, never by filesystem path.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validate_name(name: str) -> str:
    """Raise ValueError unless *name* is a safe SQL identifier; return it otherwise."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"Invalid result name {name!r}. Use a SQL identifier: letters, digits, "
            "and underscores, starting with a letter or underscore (max 63 chars)."
        )
    return name


def build_server(engine: "Engine", session_dir: str | None = None) -> FastMCP:
    """Build and return a FastMCP instance wired to *engine*.

    Registers:
    - Resource  ``db://tables``        — lists all tables/views (wraps list_objects).
    - Template  ``db://{table}``       — describes one table (wraps describe).
    - Tool      ``run_query(sql)``     — executes a read-only SQL query (wraps run_sql).
    - Tools     ``save_result`` / ``query_results`` / ``save_result_from`` /
      ``list_results`` / ``export_result`` — a session-scoped DuckDB workspace for
      caching source-DB results under a name and querying/joining them locally.

    *session_dir*, when given, roots a durable workspace (``workspace.duckdb`` under
    it) so named results survive restarts. When omitted, the workspace is an
    ephemeral in-memory DuckDB that lives only for the server process.
    """
    dialect_name = engine.dialect.name
    dialect_label = _DIALECT_LABELS.get(dialect_name, dialect_name)

    # --- session workspace: a DuckDB database addressed by result name --- #
    # With session_dir -> a durable file; without -> in-memory (ephemeral, and
    # naturally isolated so multiple servers in one process never lock-conflict).
    import duckdb

    if session_dir is not None:
        session_path = os.path.abspath(session_dir)
        os.makedirs(session_path, exist_ok=True)
        workspace = duckdb.connect(os.path.join(session_path, "workspace.duckdb"))
    else:
        workspace = duckdb.connect()

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
            "automatically. Default timeout is 300 s.\n"
            "- `describe_query(sql)` — profile every column in the query result using "
            "SQL aggregates pushed to the database. "
            "Numeric columns return non_null_count, null_rate, min, max, mean, std — "
            "plus p25/p50/p75/p95 on databases with ordered-set PERCENTILE_CONT "
            "(PostgreSQL, Oracle, DuckDB). "
            "Text/object columns return non_null_count, null_rate, unique, "
            "top (most-frequent value), freq (its count). "
            "Use this instead of writing manual aggregation queries.\n\n"
            "## Session workspace (named results)\n"
            "A local DuckDB workspace lets you cache results from the source database "
            "under a name and then query, join, and transform them locally — without "
            "re-hitting the source DB. Results are addressed by NAME, never by file path.\n"
            "- `save_result(sql, name)` — run a SELECT against the SOURCE database (no row "
            "cap) and store the full result in the workspace as table `name`. Use this to "
            "pull a slice once and reuse it.\n"
            "- `query_results(sql)` — run DuckDB SQL over the saved results (e.g. "
            "`SELECT * FROM orders JOIN customers USING (cust_id)`). Read-only, capped at "
            "1000 rows.\n"
            "- `save_result_from(sql, name)` — run DuckDB SQL over the saved results and "
            "store the output under a new `name`. This is how you build step-by-step: each "
            "result feeds the next.\n"
            "- `list_results()` — list saved results with their columns, types, and row "
            "counts so you know what you can build on.\n"
            "- `export_result(name, format, path)` — write a saved result to a file "
            "(parquet/csv/json) when you want a durable artifact.\n\n"
            "## Recommended workflow\n"
            "1. Read `db://tables` to discover what tables exist.\n"
            "2. Read `db://{table}` for each table relevant to the question — pay attention to "
            "foreign keys to understand join paths and to column profiles for value distributions.\n"
            "3. Draft a SELECT query. Prefer explicit column lists over `SELECT *`.\n"
            "4. Call `run_query` with your SQL. If the result is empty or unexpected, inspect the "
            "sample rows from step 2 and adjust.\n"
            "5. Call `describe_query` to profile the distribution of any result set — "
            "use it instead of writing manual aggregation queries.\n\n"
            "## Constraints\n"
            "- All queries must be read-only SELECT statements (CTEs are fine).\n"
            "- `run_query` is capped at 1 000 rows; use `export_query` for full result sets.\n"
            "- `describe_query` aggregates over the full query result set — no row cap.\n"
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

    # ------------------------------------------------------------------ #
    # Tool: profile every column of a query result
    # ------------------------------------------------------------------ #
    @mcp.tool(
        name="describe_query",
        description=(
            "Run a SQL query and return per-column statistics computed in the database. "
            "Numeric columns: non_null_count, null_rate, min, max, mean, std"
            " — plus p25/p50/p75/p95 on databases with ordered-set PERCENTILE_CONT "
            "(PostgreSQL, Oracle, DuckDB). "
            "Text/object columns: non_null_count, null_rate, unique, "
            "top (most-frequent value), freq (its count). "
            "Aggregates run over the full result set of the query — no row cap."
        ),
    )
    def _describe_query(sql: str) -> dict:
        t0 = time.perf_counter()

        # Probe: run with a small LIMIT to discover column names and types.
        # run_sql handles the read-only guard and LIMIT injection.
        probe = run_sql(engine, sql, max_rows=10)
        columns = probe.columns

        if not columns:
            return {"row_count": 0, "elapsed_s": 0.0, "columns": {}}

        numeric_cols = _detect_numeric(columns, probe.rows)
        object_col_list = [c for c in columns if c not in numeric_cols]

        # Build a single aggregate SELECT over the full query result.
        # agg_meta tracks (col, stat) in the same positional order as select_parts
        # so we can unpack the result row by index without relying on aliases.
        select_parts: list[str] = ["COUNT(*)"]
        agg_meta: list[tuple[str, str]] = [("_total", "_total")]

        for col in columns:
            qc = _quote_col(col, dialect_name)

            select_parts.append(
                f"1.0 * (COUNT(*) - COUNT({qc})) / NULLIF(COUNT(*), 0)"
            )
            agg_meta.append((col, "null_rate"))

            select_parts.append(f"COUNT({qc})")
            agg_meta.append((col, "non_null_count"))

            if col in numeric_cols:
                select_parts.append(f"MIN({qc})")
                agg_meta.append((col, "min"))
                select_parts.append(f"MAX({qc})")
                agg_meta.append((col, "max"))
                select_parts.append(f"AVG({qc})")
                agg_meta.append((col, "mean"))

                if dialect_name in _STDDEV_DIALECTS:
                    fn = _STDDEV_FN.get(dialect_name, "STDDEV")
                    select_parts.append(f"{fn}({qc})")
                    agg_meta.append((col, "std"))
                elif dialect_name in _MANUAL_STDDEV_DIALECTS:
                    select_parts.append(
                        f"SQRT(ABS(AVG({qc} * {qc}) - AVG({qc}) * AVG({qc})))"
                    )
                    agg_meta.append((col, "std"))

                if dialect_name in _PERCENTILE_DIALECTS:
                    for pct, label in (
                        (0.25, "p25"), (0.50, "p50"), (0.75, "p75"), (0.95, "p95")
                    ):
                        select_parts.append(
                            f"PERCENTILE_CONT({pct}) WITHIN GROUP (ORDER BY {qc})"
                        )
                        agg_meta.append((col, label))
            else:
                select_parts.append(f"COUNT(DISTINCT {qc})")
                agg_meta.append((col, "unique"))

        agg_sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM ({sql}) AS _spelunk_q"
        )

        with engine.connect() as conn:
            agg_row = list(conn.execute(_sa_text(agg_sql)).fetchone())

            col_stats: dict[str, dict[str, Any]] = {col: {} for col in columns}
            total_rows = int(agg_row[0])

            for i, (col, stat) in enumerate(agg_meta):
                if col == "_total":
                    continue
                val = _to_python(agg_row[i])
                if isinstance(val, float):
                    val = round(val, 6)
                col_stats[col][stat] = val

            # Top/freq for object columns: one lightweight GROUP BY per column.
            for col in object_col_list:
                qc = _quote_col(col, dialect_name)
                top_sql = (
                    f"SELECT {qc}, COUNT(*) AS _spelunk_freq "
                    f"FROM ({sql}) AS _spelunk_top "
                    f"WHERE {qc} IS NOT NULL "
                    f"GROUP BY {qc} "
                    f"ORDER BY _spelunk_freq DESC "
                    f"LIMIT 1"
                )
                top_row = conn.execute(_sa_text(top_sql)).fetchone()
                if top_row:
                    col_stats[col]["top"] = _to_python(top_row[0])
                    col_stats[col]["freq"] = int(top_row[1])
                else:
                    col_stats[col]["top"] = None
                    col_stats[col]["freq"] = 0

        return {
            "row_count": total_rows,
            "elapsed_s": round(time.perf_counter() - t0, 3),
            "columns": col_stats,
        }

    # ------------------------------------------------------------------ #
    # Session workspace: named results cached in a local DuckDB file
    # ------------------------------------------------------------------ #
    def _workspace_columns(name: str) -> list[dict[str, str]]:
        """Return [{name, type}] for a workspace table, in declaration order."""
        rows = workspace.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
            [name],
        ).fetchall()
        return [{"name": c, "type": t} for c, t in rows]

    @mcp.tool(
        name="save_result",
        description=(
            "Run a read-only SELECT against the SOURCE database (no row cap) and store the "
            "full result in the session workspace as a named table. Reuse it later with "
            "query_results / save_result_from without re-querying the source. "
            "`name` must be a SQL identifier (letters, digits, underscores). "
            "Replaces any existing result with the same name."
        ),
    )
    def _save_result(sql: str, name: str) -> dict:
        """Materialize a source-DB query into the workspace as table *name*."""
        _validate_name(name)
        # run_sql applies the read-only guard against the source dialect and pulls
        # all rows (max_rows=None). Reuses the exact bridge export_query already uses.
        result = run_sql(engine, sql, max_rows=None)
        df = pd.DataFrame(result.rows, columns=result.columns)
        workspace.register("_save_src", df)
        try:
            workspace.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _save_src')
        finally:
            workspace.unregister("_save_src")
        return {
            "name": name,
            "row_count": result.row_count,
            "columns": _workspace_columns(name),
            "elapsed_s": round(result.elapsed_s or 0.0, 3),
        }

    @mcp.tool(
        name="query_results",
        description=(
            "Execute read-only DuckDB SQL over the named results in the session workspace "
            "(reference them by name, e.g. `FROM my_result`). Use this to filter, join, or "
            "aggregate cached results locally. Results are capped at 1000 rows — use "
            "save_result_from or export_result for full output."
        ),
    )
    def _query_results(sql: str) -> dict:
        """Run a read-only DuckDB query over the workspace and return rows."""
        guard.assert_read_only(sql, "duckdb")
        sql2 = guard.enforce_limit(sql, "duckdb", 1000)
        t0 = time.perf_counter()
        cur = workspace.execute(sql2)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [[_to_python(v) for v in row] for row in cur.fetchall()]
        elapsed_s = time.perf_counter() - t0
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == 1000,
            "elapsed_s": round(elapsed_s, 3),
            "sql_executed": sql2,
        }

    @mcp.tool(
        name="save_result_from",
        description=(
            "Run read-only DuckDB SQL over the named results in the session workspace and "
            "store its full output (no row cap) under a new name. This is how you build "
            "analyses step by step — each saved result can feed the next. "
            "`name` must be a SQL identifier. Replaces any existing result with that name."
        ),
    )
    def _save_result_from(sql: str, name: str) -> dict:
        """Materialize a workspace query under a new table *name*."""
        _validate_name(name)
        guard.assert_read_only(sql, "duckdb")
        t0 = time.perf_counter()
        workspace.execute(f'CREATE OR REPLACE TABLE "{name}" AS {sql}')
        row_count = workspace.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return {
            "name": name,
            "row_count": int(row_count),
            "columns": _workspace_columns(name),
            "elapsed_s": round(time.perf_counter() - t0, 3),
        }

    @mcp.tool(
        name="list_results",
        description=(
            "List the named results currently in the session workspace, each with its "
            "columns, types, and row count, so you know what you can query or build on."
        ),
    )
    def _list_results() -> dict:
        """Return all workspace tables with schema and row counts."""
        tables = workspace.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        results = []
        for (tname,) in tables:
            row_count = workspace.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
            results.append({
                "name": tname,
                "row_count": int(row_count),
                "columns": _workspace_columns(tname),
            })
        return {"results": results}

    @mcp.tool(
        name="export_result",
        description=(
            "Write a named workspace result to a file for a durable artifact. "
            "Supported formats: csv, json, parquet. `path` is an absolute or relative file "
            "path; parent directories are created automatically."
        ),
    )
    def _export_result(name: str, format: str, path: str) -> dict:
        """Export workspace table *name* to *path* in *format* via DuckDB COPY."""
        _validate_name(name)
        fmt = format.lower().strip()
        copy_opts = {
            "parquet": "(FORMAT PARQUET)",
            "csv": "(FORMAT CSV, HEADER)",
            "json": "(FORMAT JSON)",
        }
        if fmt not in copy_opts:
            raise ValueError(f"Unsupported format {fmt!r}. Choose csv, json, or parquet.")

        abs_path = os.path.abspath(path)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # DuckDB accepts forward slashes on Windows; escape any literal quotes.
        safe_path = abs_path.replace("\\", "/").replace("'", "''")
        workspace.execute(f'COPY "{name}" TO \'{safe_path}\' {copy_opts[fmt]}')
        row_count = workspace.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        return {
            "path": abs_path,
            "format": fmt,
            "name": name,
            "row_count": int(row_count),
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
    parser.add_argument(
        "--session-dir",
        default=None,
        help=(
            "Directory for a durable session workspace (workspace.duckdb holding named "
            "results that survive restarts). Omit for an ephemeral in-memory workspace."
        ),
    )
    args = parser.parse_args()

    engine = connect(args.dsn)
    server = build_server(engine, session_dir=args.session_dir)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
