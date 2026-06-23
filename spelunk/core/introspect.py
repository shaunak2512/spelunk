"""Schema discovery — the virtual filesystem's `ls` and `cat` (Wave 2)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .types import TableDescription, TableInfo

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def list_objects(engine: "Engine") -> list[TableInfo]:
    """List tables and views (the 'files'). Use a SQLAlchemy ``Inspector``.

    ``row_count`` may be left ``None`` (a cheap listing) or filled if trivially available.
    """
    raise NotImplementedError("Wave 2: SQLAlchemy Inspector get_table_names/get_view_names")


def describe(engine: "Engine", table: str, *, profile: bool = True) -> TableDescription:
    """Describe one table: columns, PK, FKs, and a small sample of rows.

    When ``profile=True``, also populate per-column ``ColumnProfile`` (null_fraction,
    distinct_count, sample_values) via DuckDB ``SUMMARIZE`` or plain aggregate SELECTs.
    Cap ``sample_rows`` small (e.g. 5).
    """
    raise NotImplementedError("Wave 2: Inspector columns/pk/fks + optional profiling")
