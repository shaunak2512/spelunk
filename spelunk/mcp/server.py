"""MCP front-end for Spelunk — exposes core introspect + query over FastMCP.

Usage (stdio, for Claude Code via .mcp.json):
    python -m spelunk.mcp.server --dsn sqlite:///path/to.db

No model, no loop — Claude Code is the agent.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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

# Schemas the workspace must never create tables in or drop — DuckDB's own.
_RESERVED_SCHEMAS = frozenset({"main", "information_schema", "pg_catalog", "system", "temp"})


def _validate_name(name: str, kind: str = "result name") -> str:
    """Raise ValueError unless *name* is a safe SQL identifier; return it otherwise."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"Invalid {kind} {name!r}. Use a SQL identifier: letters, digits, "
            "and underscores, starting with a letter or underscore (max 63 chars)."
        )
    return name


def build_server(engine: "Engine", session_dir: str | None = None) -> FastMCP:
    """Build and return a FastMCP instance wired to *engine*.

    Registers:
    - Resource  ``db://tables``        — lists all tables/views (wraps list_objects).
    - Template  ``db://{table}``       — describes one table (wraps describe).
    - Tool      ``run_query(sql)``     — executes a read-only SQL query (wraps run_sql).
    - Tools     ``extract`` / ``transform`` / ``peek`` /
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

    # Each flow is a DuckDB schema, so parallel pipelines can't collide on names
    # and a whole pipeline tears down with one DROP SCHEMA. The default flow is the
    # schema "default"; ensure it exists up front so list_flows always shows it.
    default_flow = "default"
    workspace.execute(f'CREATE SCHEMA IF NOT EXISTS "{default_flow}"')

    # A single DuckDBPyConnection isn't safe for concurrent use, so serialize every
    # workspace touch. The slow part — source-DB pulls in save_result — happens
    # OUTSIDE this lock, so parallel flows still overlap where it actually matters.
    ws_lock = threading.Lock()

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
            "re-hitting the source DB. Results are addressed by NAME, never by file path.\n\n"
            "Results live in a FLOW: an independent, named workspace (its own namespace). "
            "Every workspace tool takes an optional `flow` (default `\"default\"`). Use one "
            "flow per concurrent line of analysis so their results never collide — even when "
            "they share the same result name. NOTE: a spelunk flow is just a result "
            "namespace in this database; it is NOT an orchestration or control-flow "
            "'pipeline'. If asked to run several analyses in parallel, give each its own "
            "flow.\n\n"
            "PARALLEL RULE: calls within the same flow are sequential (single DuckDB "
            "connection). Calls across different flows are parallel-safe. Give each "
            "concurrent line of analysis its own flow.\n\n"
            "Worked example — pull once from the source, then query it locally in a flow:\n"
            "  extract('SELECT id, name FROM artist', 'art', flow='artists')\n"
            "  peek('SELECT COUNT(*) FROM art', flow='artists')\n\n"
            "### Tools\n"
            "- `extract(sql, name, flow?)` — pull a SELECT from the SOURCE database "
            "(no row cap) into the flow as table `name`. Use this "
            "to pull a slice once and reuse it.\n"
            "- `peek(sql, flow?)` — inspect cached results (capped at 1000 rows). "
            "Use transform to compute over the full set without truncation. "
            "Sequential within a flow.\n"
            "- `transform(sql, name, flow?)` — materialize a query over cached results "
            "as a new named table (no row cap). Build step-by-step: each result feeds the next. "
            "Sequential within a flow.\n"
            "- `list_results(flow?)` — list saved results in a flow with their columns, "
            "types, and row counts so you know what you can build on.\n"
            "- `export_result(name, format, path, flow?)` — write a saved result to a file "
            "(parquet/csv/json) when you want a durable artifact.\n"
            "- `drop_result(name, flow?)` — delete a single intermediate you no longer need.\n"
            "- `drop_flow(flow)` — delete an entire flow (all its results) in one call.\n"
            "- `list_flows()` — list active flows and how many results each holds.\n\n"
            "### Running several analyses at once\n"
            "Give each line of analysis its own `flow` so their intermediates stay separate "
            "(see PARALLEL RULE above — steps within a flow are sequential). "
            "Within a flow, `peek` / `save_result_from` resolve bare result names "
            "automatically. To combine results across flows, fully-qualify each name as "
            "`\"<flow>\".\"<name>\"` — for example, joining a result from flow `q1` with one "
            "from flow `q2`:\n"
            "  peek('SELECT * FROM \"q1\".\"loans\" a "
            "JOIN \"q2\".\"loans\" b ON a.region = b.region')\n\n"
            "### Lifecycle\n"
            "Flows persist for the life of the server (and across restarts when a session "
            "directory is configured), and remain available across turns until you remove "
            "them. Use `list_flows()` to see what exists, `drop_result` to drop a single "
            "intermediate, and `drop_flow` to tidy up an entire analysis when you're done "
            "with it.\n\n"
            "## Recommended workflow\n"
            "1. Read `db://tables` to discover what tables exist.\n"
            "2. Read `db://{table}` for each table relevant to the question — pay attention to "
            "foreign keys to understand join paths and to column profiles for value distributions.\n"
            "3. Draft a SELECT query. Prefer explicit column lists over `SELECT *`.\n"
            "4. Call `run_query` with your SQL. If the result is empty or unexpected, inspect the "
            "sample rows from step 2 and adjust.\n"
            "5. Call `describe_query` to profile the distribution of any result set — "
            "use it instead of writing manual aggregation queries.\n"
            "6. For multi-step analysis — caching an expensive pull, combining several "
            "source queries, or building intermediate results — switch to the session "
            "workspace: `extract` to cache a source query, then `peek` / "
            "`transform` to build on it locally without re-hitting the source. When "
            "running several independent analyses at once, give each its own `flow` (see "
            "the Session workspace section), and `drop_flow` to clean up when done.\n\n"
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
    # Session workspace: named results in a local DuckDB database.
    # Each flow is a schema, so parallel pipelines stay isolated and tear
    # down independently. Every workspace touch holds ws_lock.
    # ------------------------------------------------------------------ #
    def _ensure_flow(flow: str) -> None:
        """Create the flow's schema if absent (caller holds ws_lock)."""
        workspace.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')

    def _workspace_columns(name: str, flow: str) -> list[dict[str, str]]:
        """Return [{name, type}] for a result in *flow* (caller holds ws_lock)."""
        rows = workspace.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [flow, name],
        ).fetchall()
        return [{"name": c, "type": t} for c, t in rows]

    def _flow_result_names(flow: str) -> list[str]:
        """Return all result names in *flow* (caller holds ws_lock)."""
        rows = workspace.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = ? ORDER BY table_name",
            [flow],
        ).fetchall()
        return [r[0] for r in rows]

    @mcp.tool(
        name="extract",
        description=(
            "Run a read-only SELECT against the SOURCE database (no row cap) and store the "
            "full result in the session workspace as a named table. Reuse it later with "
            "peek / transform without re-querying the source. "
            "`name` must be a SQL identifier. `flow` (default 'default') is an isolated "
            "result namespace — give each concurrent line of analysis its own flow. "
            "Replaces any existing result with the same name in that flow."
        ),
    )
    def _extract(sql: str, name: str, flow: str = default_flow) -> dict:
        """Pull a source-DB query into *flow* as table *name*."""
        _validate_name(name)
        _validate_name(flow, "flow name")
        # run_sql applies the read-only guard and pulls all rows (max_rows=None).
        # This source pull is the slow part and runs OUTSIDE the lock so parallel
        # save_result calls overlap on the source DB.
        result = run_sql(engine, sql, max_rows=None)
        df = pd.DataFrame(result.rows, columns=result.columns)
        tmp = f"_save_src_{uuid4().hex}"  # unique so concurrent saves never clobber
        with ws_lock:
            _ensure_flow(flow)
            workspace.register(tmp, df)
            try:
                workspace.execute(
                    f'CREATE OR REPLACE TABLE "{flow}"."{name}" AS SELECT * FROM {tmp}'
                )
            finally:
                workspace.unregister(tmp)
            columns = _workspace_columns(name, flow)
        return {
            "name": name,
            "flow": flow,
            "row_count": result.row_count,
            "columns": columns,
            "elapsed_s": round(result.elapsed_s or 0.0, 3),
        }

    @mcp.tool(
        name="peek",
        description=(
            "Execute read-only DuckDB SQL over the named results in a flow. "
            "CAPPED AT 1000 ROWS — use transform to compute on the full set without "
            "truncation, or export_result to write it to a file. "
            "Reference results by bare name (e.g. `FROM my_result`), or qualify across "
            "flows as \"<flow>\".\"<name>\". `flow` (default 'default') selects the namespace. "
            "Calls within the same flow are sequential — use separate flows for parallel analysis."
        ),
    )
    def _peek(sql: str, flow: str = default_flow) -> dict:
        """Run a read-only DuckDB query over *flow* and return rows."""
        _validate_name(flow, "flow name")
        guard.assert_read_only(sql, "duckdb")
        sql2 = guard.enforce_limit(sql, "duckdb", 1000)
        with ws_lock:
            _ensure_flow(flow)
            workspace.execute(f"SET search_path = '{flow}'")
            t0 = time.perf_counter()
            try:
                cur = workspace.execute(sql2)
            except duckdb.CatalogException as exc:
                available = _flow_result_names(flow)
                raise ValueError(
                    f"{exc}\nFlow {flow!r} contains: {available or ['(none)']}. "
                    f"Call list_results(flow={flow!r}) to see saved results."
                ) from exc
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [[_to_python(v) for v in row] for row in cur.fetchall()]
            elapsed_s = time.perf_counter() - t0
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "row_cap": 1000,
            "truncated": len(rows) == 1000,
            "elapsed_s": round(elapsed_s, 3),
            "sql_executed": sql2,
        }

    @mcp.tool(
        name="transform",
        description=(
            "Run read-only DuckDB SQL over the named results in a flow and store its full "
            "output (NO row cap) under a new name in that same flow — unlike peek, "
            "which is capped at 1000 rows. This is how you build analyses step by step: "
            "each result feeds the next. `name`/`flow` must be SQL identifiers. "
            "Replaces any existing result with that name in the flow. "
            "Calls within the same flow are sequential — use separate flows for parallel analysis."
        ),
    )
    def _transform(sql: str, name: str, flow: str = default_flow) -> dict:
        """Transform cached results into a new table *name* in the same *flow*."""
        _validate_name(name)
        _validate_name(flow, "flow name")
        guard.assert_read_only(sql, "duckdb")
        with ws_lock:
            _ensure_flow(flow)
            workspace.execute(f"SET search_path = '{flow}'")
            t0 = time.perf_counter()
            try:
                workspace.execute(f'CREATE OR REPLACE TABLE "{flow}"."{name}" AS {sql}')
            except duckdb.CatalogException as exc:
                available = _flow_result_names(flow)
                raise ValueError(
                    f"{exc}\nFlow {flow!r} contains: {available or ['(none)']}. "
                    f"Call list_results(flow={flow!r}) to see saved results."
                ) from exc
            row_count = workspace.execute(
                f'SELECT COUNT(*) FROM "{flow}"."{name}"'
            ).fetchone()[0]
            columns = _workspace_columns(name, flow)
        return {
            "name": name,
            "flow": flow,
            "row_count": int(row_count),
            "columns": columns,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        }

    @mcp.tool(
        name="list_results",
        description=(
            "List the named results in a flow (default 'default'), each with its columns, "
            "types, and row count, so you know what you can query or build on."
        ),
    )
    def _list_results(flow: str = default_flow) -> dict:
        """Return all results in *flow* with schema and row counts."""
        _validate_name(flow, "flow name")
        with ws_lock:
            _ensure_flow(flow)
            tables = workspace.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = ? ORDER BY table_name",
                [flow],
            ).fetchall()
            results = []
            for (tname,) in tables:
                row_count = workspace.execute(
                    f'SELECT COUNT(*) FROM "{flow}"."{tname}"'
                ).fetchone()[0]
                results.append({
                    "name": tname,
                    "row_count": int(row_count),
                    "columns": _workspace_columns(tname, flow),
                })
        return {"flow": flow, "results": results}

    @mcp.tool(
        name="export_result",
        description=(
            "Write a named result from a flow to a file for a durable artifact. "
            "Supported formats: csv, json, parquet. `path` is an absolute or relative file "
            "path; parent directories are created automatically."
        ),
    )
    def _export_result(name: str, format: str, path: str, flow: str = default_flow) -> dict:
        """Export result *name* from *flow* to *path* in *format* via DuckDB COPY."""
        _validate_name(name)
        _validate_name(flow, "flow name")
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
        with ws_lock:
            workspace.execute(
                f'COPY "{flow}"."{name}" TO \'{safe_path}\' {copy_opts[fmt]}'
            )
            row_count = workspace.execute(
                f'SELECT COUNT(*) FROM "{flow}"."{name}"'
            ).fetchone()[0]
        return {
            "path": abs_path,
            "format": fmt,
            "name": name,
            "flow": flow,
            "row_count": int(row_count),
        }

    @mcp.tool(
        name="drop_result",
        description=(
            "Delete a single named result from a flow when you no longer need it. "
            "No error if it doesn't exist. Returns whether a result was actually removed."
        ),
    )
    def _drop_result(name: str, flow: str = default_flow) -> dict:
        """Drop result *name* from *flow* (idempotent)."""
        _validate_name(name)
        _validate_name(flow, "flow name")
        with ws_lock:
            existed = workspace.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = ? AND table_name = ?",
                [flow, name],
            ).fetchone()[0] > 0
            workspace.execute(f'DROP TABLE IF EXISTS "{flow}"."{name}"')
        return {"name": name, "flow": flow, "dropped": bool(existed)}

    @mcp.tool(
        name="drop_flow",
        description=(
            "Delete an entire flow and every result in it in one call — use this to clean "
            "up a finished analysis. Cannot drop DuckDB's reserved schemas."
        ),
    )
    def _drop_flow(flow: str) -> dict:
        """Drop the whole *flow* schema and its results (idempotent)."""
        _validate_name(flow, "flow name")
        if flow in _RESERVED_SCHEMAS:
            raise ValueError(f"Cannot drop reserved schema {flow!r}.")
        with ws_lock:
            dropped = workspace.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ?",
                [flow],
            ).fetchone()[0]
            workspace.execute(f'DROP SCHEMA IF EXISTS "{flow}" CASCADE')
        return {"flow": flow, "dropped_results": int(dropped)}

    @mcp.tool(
        name="list_flows",
        description=(
            "List the active flows in the workspace and how many results each holds. "
            "Use this to see what flows exist and what can be cleaned up."
        ),
    )
    def _list_flows() -> dict:
        """Return every user flow (schema) with its result count."""
        with ws_lock:
            placeholders = ", ".join("?" for _ in _RESERVED_SCHEMAS)
            schemas = workspace.execute(
                "SELECT schema_name FROM information_schema.schemata "
                f"WHERE schema_name NOT IN ({placeholders}) ORDER BY schema_name",
                list(_RESERVED_SCHEMAS),
            ).fetchall()
            flows = []
            for (sname,) in schemas:
                count = workspace.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ?",
                    [sname],
                ).fetchone()[0]
                flows.append({"flow": sname, "result_count": int(count)})
        return {"flows": flows}

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
