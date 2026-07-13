"""Spelunk core — the agent-agnostic tools library (no LLM, no protocol).

Public API. Both front-ends (agent, MCP) call only these names.
"""
from __future__ import annotations

from .guard import assert_read_only, enforce_limit
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
