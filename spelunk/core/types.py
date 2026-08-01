"""Frozen data contracts for the core tools library.

These types ARE the interface every front-end (agent, MCP) and every core function
agree on. Changing them is a barrier-level event — coordinate, don't do it inside a
parallel agent.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Column-type predicates
# --------------------------------------------------------------------------- #
# DuckDB base type names that mark a column as numeric. Matched against the type name with any
# parametrisation stripped (e.g. DECIMAL(18,3) -> DECIMAL) — exact, not substring, so INTERVAL is
# not mistaken for an INT (STDDEV/percentiles fail on interval values).
#
# It lives here, beside `ColumnInfo.type` which it interprets, rather than in `duck.py`: both the
# profiler and the `show` chart builders need it, and `mcp/views.py` is deliberately free of
# DuckDB (importing it from `duck.py` pulled the whole engine into the rendering layer).
NUMERIC_TYPES = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "DECIMAL", "NUMERIC", "REAL", "FLOAT", "DOUBLE",
})


def is_numeric_type(type_name: str) -> bool:
    """True if a DuckDB column type is numeric — exact base-type match (drops any `(...)`)."""
    return type_name.upper().split("(", 1)[0].strip() in NUMERIC_TYPES


# --------------------------------------------------------------------------- #
# Schema description (results of list_objects / describe)
# --------------------------------------------------------------------------- #
class ForeignKey(BaseModel):
    """A single foreign-key edge: this table's `column` -> `ref_table`.`ref_column`."""

    column: str
    ref_table: str
    ref_column: str


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    comment: str | None = None


class ColumnProfile(BaseModel):
    """Cheap profiling stats for one column (only populated when describe(profile=True))."""

    column: str
    null_fraction: float | None = None
    distinct_count: int | None = None
    sample_values: list[Any] = Field(default_factory=list)


class TableInfo(BaseModel):
    """A 'file' in the virtual filesystem: one table or view."""

    name: str
    kind: Literal["table", "view"] = "table"
    row_count: int | None = None
    comment: str | None = None


class IndexInfo(BaseModel):
    """One index on a table."""

    name: str | None = None
    unique: bool = False
    columns: list[str] = Field(default_factory=list)


class TableDescription(BaseModel):
    """The 'cat' of a table: schema + relationships + a sample, optionally profiled."""

    name: str
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    indexes: list[IndexInfo] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    profile: list[ColumnProfile] = Field(default_factory=list)
    row_count: int | None = None


# --------------------------------------------------------------------------- #
# Query execution
# --------------------------------------------------------------------------- #
class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False  # True if auto-LIMIT clipped the result set
    elapsed_s: float | None = None
    sql_executed: str | None = None  # the SQL actually run, post LIMIT-injection


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SpelunkError(Exception):
    """Base class for all Spelunk errors."""


class UnsafeSQLError(SpelunkError):
    """Raised when a statement is not a single read-only query."""


class QueryTimeoutError(SpelunkError):
    """Raised when a query exceeds its statement timeout."""
