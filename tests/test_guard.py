"""Contract tests for core.guard — pure string functions, no DB needed.
RED until Wave 1 implements guard.py."""
from __future__ import annotations

import pytest

from spelunk.core.guard import assert_read_only, enforce_limit
from spelunk.core.types import UnsafeSQLError

READ_ONLY_OK = [
    "SELECT 1",
    "select * from customers",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT name FROM customers WHERE city = 'Sydney'",
]

WRITES_BAD = [
    "INSERT INTO customers VALUES (4, 'x', NULL, NULL)",
    "UPDATE customers SET city = 'X'",
    "DELETE FROM orders",
    "DROP TABLE customers",
    "ALTER TABLE customers ADD COLUMN z INT",
    "CREATE TABLE t (a INT)",
    "TRUNCATE TABLE orders",
    "SELECT 1; DROP TABLE customers",  # multi-statement
]


@pytest.mark.parametrize("sql", READ_ONLY_OK)
def test_allows_read_only(sql):
    assert_read_only(sql, "sqlite")  # must not raise


@pytest.mark.parametrize("sql", WRITES_BAD)
def test_rejects_non_select(sql):
    with pytest.raises(UnsafeSQLError):
        assert_read_only(sql, "sqlite")


def test_limit_injected_when_absent():
    out = enforce_limit("SELECT * FROM customers", "sqlite", 100)
    assert "limit" in out.lower()


def test_limit_clamped_when_too_high():
    out = enforce_limit("SELECT * FROM customers LIMIT 10000", "sqlite", 100)
    assert "100" in out
    assert "10000" not in out


def test_limit_left_when_below_cap():
    out = enforce_limit("SELECT * FROM customers LIMIT 5", "sqlite", 100)
    assert "5" in out
