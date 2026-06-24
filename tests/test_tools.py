"""Tests for spelunk.agent.tools — offline, no LLM required.

Each tool is invoked directly via tool.invoke({...}) or tool.func(...).
The sample_db fixture (from conftest.py) provides a 2-table SQLite database
with a foreign key from orders.customer_id -> customers.id.
"""
from __future__ import annotations

import json

import pytest

from spelunk.core.connection import connect
from spelunk.agent.tools import make_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_tool(tools, name):
    """Return the named tool from the list; fail clearly if missing."""
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool {name!r} not found; available: {[t.name for t in tools]}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(sample_db):
    """SQLAlchemy engine built from the conftest sample_db DSN."""
    # sample_db fixture returns an sqlite:// DSN for a read-only-capable DB.
    # connect() with read_only=False so we can use the DSN directly
    # (the sqlite mode=ro URI form is handled inside connect; we just need an engine).
    return connect(sample_db, read_only=False)


@pytest.fixture
def tools(engine):
    """The list of LangChain tools built over the sample engine."""
    return make_tools(engine, profile=True)


# ---------------------------------------------------------------------------
# Tool inventory
# ---------------------------------------------------------------------------

def test_make_tools_returns_four(tools):
    """make_tools must return exactly 4 tools."""
    assert len(tools) == 4


def test_tool_names(tools):
    """Each expected tool name is present."""
    names = {t.name for t in tools}
    assert "list_tables" in names
    assert "describe_table" in names
    assert "run_query" in names
    assert "submit_sql" in names


# ---------------------------------------------------------------------------
# list_tables
# ---------------------------------------------------------------------------

def test_list_tables_finds_customers_and_orders(tools):
    tool = _get_tool(tools, "list_tables")
    result = tool.invoke({})
    # Result must be a string (JSON) the model can read.
    assert isinstance(result, str)
    data = json.loads(result)
    names = [item["name"] for item in data]
    assert "customers" in names
    assert "orders" in names


def test_list_tables_returns_json_list(tools):
    tool = _get_tool(tools, "list_tables")
    result = tool.invoke({})
    data = json.loads(result)
    assert isinstance(data, list)
    assert len(data) >= 2


# ---------------------------------------------------------------------------
# describe_table
# ---------------------------------------------------------------------------

def test_describe_table_orders_has_columns(tools):
    tool = _get_tool(tools, "describe_table")
    result = tool.invoke({"name": "orders"})
    assert isinstance(result, str)
    data = json.loads(result)
    col_names = [c["name"] for c in data["columns"]]
    assert "id" in col_names
    assert "customer_id" in col_names
    assert "amount" in col_names
    assert "status" in col_names


def test_describe_table_orders_has_fk_to_customers(tools):
    tool = _get_tool(tools, "describe_table")
    result = tool.invoke({"name": "orders"})
    data = json.loads(result)
    fks = data["foreign_keys"]
    assert len(fks) >= 1
    fk = fks[0]
    assert fk["ref_table"] == "customers"
    assert fk["column"] == "customer_id"


def test_describe_table_customers_returns_name(tools):
    tool = _get_tool(tools, "describe_table")
    result = tool.invoke({"name": "customers"})
    data = json.loads(result)
    assert data["name"] == "customers"


def test_describe_table_has_sample_rows(tools):
    tool = _get_tool(tools, "describe_table")
    result = tool.invoke({"name": "customers"})
    data = json.loads(result)
    # customers has 3 rows; sample should be non-empty (capped at 5).
    assert len(data["sample_rows"]) > 0


# ---------------------------------------------------------------------------
# run_query
# ---------------------------------------------------------------------------

def test_run_query_returns_rows(tools):
    tool = _get_tool(tools, "run_query")
    result = tool.invoke({"sql": "SELECT * FROM customers"})
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["row_count"] == 3
    assert "id" in data["columns"]


def test_run_query_join(tools):
    tool = _get_tool(tools, "run_query")
    sql = (
        "SELECT c.name, o.amount "
        "FROM orders o JOIN customers c ON o.customer_id = c.id"
    )
    result = tool.invoke({"sql": sql})
    data = json.loads(result)
    assert data["row_count"] == 3  # 3 orders in fixture
    assert "name" in data["columns"]
    assert "amount" in data["columns"]


def test_run_query_blocks_write(tools):
    """run_query must raise UnsafeSQLError for non-SELECT statements."""
    from spelunk.core.types import UnsafeSQLError
    tool = _get_tool(tools, "run_query")
    with pytest.raises(UnsafeSQLError):
        tool.invoke({"sql": "DELETE FROM customers"})


# ---------------------------------------------------------------------------
# submit_sql
# ---------------------------------------------------------------------------

def test_submit_sql_captures_and_returns_sql(tools):
    tool = _get_tool(tools, "submit_sql")
    answer_sql = "SELECT id, name FROM customers WHERE city = 'Sydney'"
    result = tool.invoke({"sql": answer_sql})
    # Result is a string; it should contain the submitted SQL.
    assert isinstance(result, str)
    assert answer_sql in result


def test_submit_sql_roundtrip_via_json(tools):
    """submit_sql result must be parseable JSON containing the SQL."""
    tool = _get_tool(tools, "submit_sql")
    answer_sql = "SELECT COUNT(*) FROM orders"
    result = tool.invoke({"sql": answer_sql})
    data = json.loads(result)
    assert data["sql"] == answer_sql
