"""Spelunk core — the agent-agnostic tools library (no LLM, no protocol).

Public API. Both front-ends (agent, MCP) call only these names.
"""
from __future__ import annotations

from .connection import connect
from .guard import assert_read_only, enforce_limit
from .introspect import describe, list_objects
from .query import run_sql
from .types import (
    ColumnInfo,
    ColumnProfile,
    ForeignKey,
    QueryResult,
    QueryTimeoutError,
    SpelunkError,
    TableDescription,
    TableInfo,
    UnsafeSQLError,
)

__all__ = [
    "connect",
    "list_objects",
    "describe",
    "run_sql",
    "assert_read_only",
    "enforce_limit",
    "TableInfo",
    "TableDescription",
    "ColumnInfo",
    "ColumnProfile",
    "ForeignKey",
    "QueryResult",
    "SpelunkError",
    "UnsafeSQLError",
    "QueryTimeoutError",
]
