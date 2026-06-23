"""Guarded query execution (Wave 2)."""
from __future__ import annotations

from typing import TYPE_CHECKING

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

    Pipeline (the contract — reuse ``guard``, don't reimplement):
      1. ``guard.assert_read_only(sql, dialect)``      -> raises ``UnsafeSQLError`` on writes
      2. ``sql = guard.enforce_limit(sql, dialect, max_rows)``
      3. execute with a statement timeout of ``timeout_s`` -> raises ``QueryTimeoutError``
      4. return ``QueryResult`` with ``truncated=True`` when the row cap clipped the result.

    ``dialect`` is derived from the engine (``engine.dialect.name``).
    """
    raise NotImplementedError("Wave 2: guarded execution via guard + SQLAlchemy")
