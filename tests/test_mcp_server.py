"""Tests for spelunk.mcp.server — offline, no server spin-up.

Strategy:
- Build a FastMCP instance via build_server(engine) using the sample_db fixture.
- Assert that the expected tool, resource, and resource template are registered
  (using asyncio.run + mcp.list_tools / list_resources / list_resource_templates).
- Call the handlers in-process via mcp.call_tool / mcp.read_resource to verify
  they return correct data from sample_db.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from spelunk.core.connection import connect
from spelunk.mcp.server import build_server


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _run(coro):
    """Run an async coroutine synchronously (avoids requiring pytest-asyncio)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def mcp_server(sample_db):
    """Build a FastMCP server wired to the sample_db SQLite database."""
    engine = connect(sample_db, read_only=False)  # sample_db is writable tmp file
    return build_server(engine)


# --------------------------------------------------------------------------- #
# Registration tests
# --------------------------------------------------------------------------- #

class TestRegistration:
    """Assert that the three components are registered on the FastMCP instance."""

    def test_run_query_tool_is_registered(self, mcp_server):
        tools = _run(mcp_server.list_tools())
        tool_names = [t.name for t in tools]
        assert "run_query" in tool_names, f"Expected 'run_query' in tools, got: {tool_names}"

    def test_list_tables_resource_is_registered(self, mcp_server):
        resources = _run(mcp_server.list_resources())
        uris = [str(r.uri) for r in resources]
        assert any("tables" in u for u in uris), (
            f"Expected a 'db://tables' resource, got URIs: {uris}"
        )

    def test_describe_table_template_is_registered(self, mcp_server):
        templates = _run(mcp_server.list_resource_templates())
        uri_templates = [t.uri_template for t in templates]
        assert any("{table}" in t for t in uri_templates), (
            f"Expected a 'db://{{table}}' template, got: {uri_templates}"
        )


# --------------------------------------------------------------------------- #
# Handler correctness tests (via in-process calls)
# --------------------------------------------------------------------------- #

class TestListTablesResource:
    """db://tables resource returns the tables in sample_db."""

    def test_returns_json_string(self, mcp_server):
        result = _run(mcp_server.read_resource("db://tables"))
        content = result.contents[0].content
        assert isinstance(content, str), "Resource content should be a JSON string"

    def test_contains_customers_and_orders(self, mcp_server):
        result = _run(mcp_server.read_resource("db://tables"))
        content = result.contents[0].content
        data = json.loads(content)
        names = {row["name"] for row in data}
        assert "customers" in names, f"Expected 'customers' in table list, got: {names}"
        assert "orders" in names, f"Expected 'orders' in table list, got: {names}"

    def test_kind_field_present(self, mcp_server):
        result = _run(mcp_server.read_resource("db://tables"))
        data = json.loads(result.contents[0].content)
        for row in data:
            assert "kind" in row, f"Missing 'kind' field in table entry: {row}"


class TestDescribeTableTemplate:
    """db://{table} template returns a TableDescription for a named table."""

    def test_customers_description_has_columns(self, mcp_server):
        result = _run(mcp_server.read_resource("db://customers"))
        content = result.contents[0].content
        data = json.loads(content)
        col_names = [c["name"] for c in data["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "city" in col_names

    def test_orders_has_foreign_key(self, mcp_server):
        result = _run(mcp_server.read_resource("db://orders"))
        content = result.contents[0].content
        data = json.loads(content)
        fk_columns = [fk["column"] for fk in data.get("foreign_keys", [])]
        assert "customer_id" in fk_columns, (
            f"Expected FK on customer_id in orders, got: {fk_columns}"
        )

    def test_sample_rows_populated(self, mcp_server):
        result = _run(mcp_server.read_resource("db://customers"))
        content = result.contents[0].content
        data = json.loads(content)
        assert len(data["sample_rows"]) > 0, "Expected at least one sample row"

    def test_primary_key_identified(self, mcp_server):
        result = _run(mcp_server.read_resource("db://customers"))
        data = json.loads(result.contents[0].content)
        assert "id" in data["primary_key"], (
            f"Expected 'id' in primary_key, got: {data['primary_key']}"
        )


class TestRunQueryTool:
    """run_query tool executes SQL and returns QueryResult-shaped data."""

    def test_select_all_customers(self, mcp_server):
        result = _run(mcp_server.call_tool("run_query", {"sql": "SELECT * FROM customers"}))
        # call_tool returns a ToolResult; structured_content holds the dict
        data = result.structured_content
        assert "columns" in data
        assert "rows" in data
        assert "row_count" in data
        assert data["row_count"] == 3, f"Expected 3 customers, got: {data['row_count']}"

    def test_customer_names_present(self, mcp_server):
        result = _run(mcp_server.call_tool("run_query", {"sql": "SELECT name FROM customers ORDER BY id"}))
        data = result.structured_content
        names = [row[0] for row in data["rows"]]
        assert names == ["Ada", "Linus", "Grace"], f"Unexpected names: {names}"

    def test_orders_count(self, mcp_server):
        result = _run(mcp_server.call_tool("run_query", {"sql": "SELECT COUNT(*) FROM orders"}))
        data = result.structured_content
        assert data["rows"][0][0] == 3, f"Expected 3 orders, got: {data['rows']}"

    def test_join_query(self, mcp_server):
        sql = (
            "SELECT c.name, o.amount "
            "FROM customers c JOIN orders o ON c.id = o.customer_id "
            "WHERE o.status = 'shipped'"
        )
        result = _run(mcp_server.call_tool("run_query", {"sql": sql}))
        data = result.structured_content
        assert data["row_count"] == 2, f"Expected 2 shipped orders, got: {data['row_count']}"

    def test_result_has_columns_field(self, mcp_server):
        result = _run(mcp_server.call_tool("run_query", {"sql": "SELECT id, name FROM customers"}))
        data = result.structured_content
        assert data["columns"] == ["id", "name"], f"Unexpected columns: {data['columns']}"

    def test_unsafe_write_raises(self, mcp_server):
        from spelunk.core.types import UnsafeSQLError
        with pytest.raises(Exception):
            # run_sql raises UnsafeSQLError for writes; FastMCP may wrap it
            _run(mcp_server.call_tool("run_query", {"sql": "DELETE FROM customers"}))


# --------------------------------------------------------------------------- #
# describe_query tool
# --------------------------------------------------------------------------- #

class TestDescribeQueryTool:
    """describe_query returns per-column stats with correct field names."""

    def test_numeric_column_has_non_null_count(self, mcp_server):
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT amount FROM orders"}))
        data = result.structured_content
        assert "non_null_count" in data["columns"]["amount"], (
            f"Expected 'non_null_count' key in numeric column stats, got: {data['columns']['amount'].keys()}"
        )

    def test_numeric_column_has_no_bare_count(self, mcp_server):
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT amount FROM orders"}))
        data = result.structured_content
        assert "count" not in data["columns"]["amount"], (
            "Key 'count' should be renamed to 'non_null_count'"
        )

    def test_numeric_column_has_std(self, mcp_server):
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT amount FROM orders"}))
        data = result.structured_content
        assert "std" in data["columns"]["amount"], (
            f"Expected 'std' in numeric column stats, got: {data['columns']['amount'].keys()}"
        )
        assert data["columns"]["amount"]["std"] is not None

    def test_text_column_has_non_null_count(self, mcp_server):
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT status FROM orders"}))
        data = result.structured_content
        assert "non_null_count" in data["columns"]["status"], (
            f"Expected 'non_null_count' key in text column stats, got: {data['columns']['status'].keys()}"
        )

    def test_text_column_has_no_bare_count(self, mcp_server):
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT status FROM orders"}))
        data = result.structured_content
        assert "count" not in data["columns"]["status"], (
            "Key 'count' should be renamed to 'non_null_count'"
        )

    def test_row_count_matches_expected(self, mcp_server):
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT amount FROM orders"}))
        data = result.structured_content
        assert data["row_count"] == 3, f"Expected 3 rows, got: {data['row_count']}"

    def test_non_null_count_excludes_nulls(self, mcp_server):
        # city has one NULL (Grace has no city); non_null_count should be 2
        result = _run(mcp_server.call_tool("describe_query", {"sql": "SELECT city FROM customers"}))
        data = result.structured_content
        assert data["columns"]["city"]["non_null_count"] == 2, (
            f"Expected non_null_count=2 for city (one NULL), got: {data['columns']['city']}"
        )


# --------------------------------------------------------------------------- #
# build_server contract
# --------------------------------------------------------------------------- #

class TestBuildServerContract:
    """Structural checks on the returned FastMCP instance."""

    def test_returns_fastmcp_instance(self, sample_db):
        from fastmcp import FastMCP
        engine = connect(sample_db, read_only=False)
        server = build_server(engine)
        assert isinstance(server, FastMCP)

    def test_server_has_three_registered_components(self, mcp_server):
        tools = _run(mcp_server.list_tools())
        resources = _run(mcp_server.list_resources())
        templates = _run(mcp_server.list_resource_templates())
        tool_names = {t.name for t in tools}
        assert tool_names == {"run_query", "export_query", "describe_query"}, (
            f"Unexpected tools: {tool_names}"
        )
        assert len(resources) == 1, f"Expected 1 resource, got: {[str(r.uri) for r in resources]}"
        assert len(templates) == 1, f"Expected 1 template, got: {[t.uri_template for t in templates]}"

    def test_multiple_servers_are_independent(self, sample_db):
        """build_server should not share state between calls."""
        engine = connect(sample_db, read_only=False)
        s1 = build_server(engine)
        s2 = build_server(engine)
        tools1 = _run(s1.list_tools())
        tools2 = _run(s2.list_tools())
        assert len(tools1) == 3
        assert len(tools2) == 3
