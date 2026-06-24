"""Guarded query execution (Wave 2)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import guard
from .types import QueryResult

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def run_sql(
    engine: "Engine",
    sql: str,
    *,
    max_rows: int = 1000,
    timeout_s: int = 30,
) -> QueryResult:
    """Execute a read-only query and return its rows.

    Pipeline (the contract — reuses ``guard``, doesn't reimplement safety logic):
      1. ``guard.assert_read_only(sql, dialect)``      -> raises ``UnsafeSQLError`` on writes
      2. ``sql2 = guard.enforce_limit(sql, dialect, max_rows)``
      3. execute ``sql2`` with ``engine.connect()``    -> fetch rows
      4. return ``QueryResult`` with ``truncated=True`` when the row cap clipped the result.

    ``dialect`` is derived from the engine (``engine.dialect.name``).

    Statement timeout (``timeout_s``) is best-effort only.  SQLite has no portable
    per-statement timeout mechanism; the parameter is accepted for API consistency but
    is not enforced on SQLite.  For PostgreSQL a ``SET statement_timeout = <ms>`` could
    be emitted before execution, but this is not exercised by the current test suite.
    """
    dialect = engine.dialect.name

    # Step 1: reject any non-read-only statement (raises UnsafeSQLError on writes/DDL).
    guard.assert_read_only(sql, dialect)

    # Step 2: rewrite the query to enforce the row cap.
    sql2 = guard.enforce_limit(sql, dialect, max_rows)

    # Step 3: execute and collect results.
    t0 = time.perf_counter()
    with engine.connect() as conn:
        result = conn.execute(_text(sql2))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchall()]
    elapsed_s = time.perf_counter() - t0

    # Step 4: detect truncation — if we got exactly max_rows back, the result
    # was likely clipped by the injected/clamped LIMIT.
    truncated = len(rows) == max_rows

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_s=elapsed_s,
        sql_executed=sql2,
    )


def _text(sql: str):
    """Wrap a raw SQL string in a SQLAlchemy ``text()`` construct."""
    from sqlalchemy import text
    return text(sql)
