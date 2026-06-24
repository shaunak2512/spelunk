"""LangChain tool wrappers around the Spelunk core functions (Wave 2b).

Exposes ``make_tools(engine, *, profile=True) -> list[BaseTool]`` returning
four tools the agent graph can call:

  - ``list_tables``   — wraps ``core.introspect.list_objects``
  - ``describe_table`` — wraps ``core.introspect.describe``
  - ``run_query``     — wraps ``core.query.run_sql``
  - ``submit_sql``    — terminator: captures/returns the final SQL answer
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from spelunk.core.introspect import list_objects, describe
from spelunk.core.query import run_sql

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from langchain_core.tools import BaseTool


# ---------------------------------------------------------------------------
# Pydantic input schemas (StructuredTool uses these for argument validation)
# ---------------------------------------------------------------------------

class _NoArgs(BaseModel):
    """Empty input schema for tools that take no arguments."""


class _TableNameArgs(BaseModel):
    name: str = Field(..., description="The table (or view) name to describe.")


class _SQLArgs(BaseModel):
    sql: str = Field(..., description="A SQL query string.")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def make_tools(engine: "Engine", *, profile: bool = True) -> list["BaseTool"]:
    """Build and return the four Spelunk LangChain tools wired to *engine*.

    Parameters
    ----------
    engine:
        A SQLAlchemy ``Engine`` pointing at the database to explore.
    profile:
        When ``True``, ``describe_table`` will populate per-column profiling
        stats (null_fraction, distinct_count, sample_values).  Pass ``False``
        to skip profiling for faster responses on large tables.

    Returns
    -------
    list[BaseTool]
        Four ``StructuredTool`` instances ready to be passed to a LangChain
        agent/graph.
    """

    # ------------------------------------------------------------------
    # list_tables — no arguments
    # ------------------------------------------------------------------
    def _list_tables() -> str:
        """List all tables and views in the database.

        Returns a JSON array of objects with ``name``, ``kind``, and
        optional ``row_count`` / ``comment`` fields.
        """
        objects = list_objects(engine)
        return json.dumps([obj.model_dump() for obj in objects])

    list_tables_tool = StructuredTool(
        name="list_tables",
        description=(
            "List all tables and views in the database. "
            "Returns a JSON array with name, kind (table/view), and optional row_count."
        ),
        func=_list_tables,
        args_schema=_NoArgs,
    )

    # ------------------------------------------------------------------
    # describe_table — one argument: name
    # ------------------------------------------------------------------
    def _describe_table(name: str) -> str:
        """Return schema, relationships, and sample rows for *name*.

        Result is a JSON object matching ``TableDescription`` with keys:
        ``name``, ``columns``, ``primary_key``, ``foreign_keys``,
        ``sample_rows``, ``profile``, ``row_count``.
        """
        td = describe(engine, name, profile=profile)
        return td.model_dump_json()

    describe_table_tool = StructuredTool(
        name="describe_table",
        description=(
            "Describe a single table or view: columns (name, type, nullable, pk), "
            "primary key, foreign keys, a sample of rows, and optional column profiles. "
            "Pass the exact table name as returned by list_tables."
        ),
        func=_describe_table,
        args_schema=_TableNameArgs,
    )

    # ------------------------------------------------------------------
    # run_query — one argument: sql
    # ------------------------------------------------------------------
    def _run_query(sql: str) -> str:
        """Execute a read-only SQL query and return the results.

        The query is guarded: only SELECT statements are allowed; writes
        (INSERT/UPDATE/DELETE/DDL) raise ``UnsafeSQLError``.  A row cap
        (1 000 by default) is injected to prevent runaway scans.

        Returns a JSON object matching ``QueryResult`` with keys:
        ``columns``, ``rows``, ``row_count``, ``truncated``,
        ``elapsed_s``, ``sql_executed``.
        """
        qr = run_sql(engine, sql)
        return qr.model_dump_json()

    run_query_tool = StructuredTool(
        name="run_query",
        description=(
            "Execute a read-only SQL SELECT query and return its result set as JSON. "
            "Only SELECT statements are permitted; any write or DDL statement is rejected. "
            "Results are capped at 1 000 rows (truncated=true when clipped)."
        ),
        func=_run_query,
        args_schema=_SQLArgs,
    )

    # ------------------------------------------------------------------
    # submit_sql — one argument: sql (terminator)
    # ------------------------------------------------------------------
    def _submit_sql(sql: str) -> str:
        """Record the final SQL answer and return it for the graph to capture.

        Wave 3 will wire this as the loop-termination signal; here it simply
        serialises the answer so the LLM sees confirmation and the graph can
        intercept the return value.

        Returns a JSON object ``{"sql": <submitted_sql>}``.
        """
        return json.dumps({"sql": sql})

    submit_sql_tool = StructuredTool(
        name="submit_sql",
        description=(
            "Submit the final SQL answer. Call this ONLY when you are confident the SQL "
            "correctly answers the user's question. The graph will terminate after this call. "
            "Pass the complete SQL string."
        ),
        func=_submit_sql,
        args_schema=_SQLArgs,
    )

    return [list_tables_tool, describe_table_tool, run_query_tool, submit_sql_tool]
