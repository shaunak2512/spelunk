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
    max_rows: int | None = 1000,
    timeout_s: int = 30,
) -> QueryResult:
    """Execute a read-only query and return its rows.

    Pipeline (reuses ``guard``, doesn't reimplement safety logic):
      1. ``guard.assert_read_only(sql, dialect)``       -> raises ``UnsafeSQLError`` on writes
      2. if ``max_rows`` is not None:
             ``sql2 = guard.enforce_limit(sql, dialect, max_rows)``
         else:
             ``sql2 = sql``  (no LIMIT injected — for bulk/export use cases)
      3. execute ``sql2`` with ``engine.connect()``     -> fetch rows
      4. return ``QueryResult`` with ``truncated=True`` when the row cap clipped the result.

    ``dialect`` is derived from the engine (``engine.dialect.name``).

    Statement timeout: for PostgreSQL, ``SET LOCAL statement_timeout = <ms>`` is emitted
    before each query.  SQLite has no portable per-statement timeout; the parameter is
    accepted for API consistency but is not enforced on SQLite.
    """
    dialect = engine.dialect.name

    guard.assert_read_only(sql, dialect)

    if max_rows is not None:
        sql2 = guard.enforce_limit(sql, dialect, max_rows)
    else:
        sql2 = sql

    t0 = time.perf_counter()
    with engine.connect() as conn:
        if dialect == "postgresql" and timeout_s:
            conn.execute(_text(f"SET LOCAL statement_timeout = {timeout_s * 1000}"))
        result = conn.execute(_text(sql2))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchall()]
    elapsed_s = time.perf_counter() - t0

    truncated = max_rows is not None and len(rows) == max_rows

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
