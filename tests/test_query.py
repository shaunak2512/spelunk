"""Contract tests for core.query. RED until Wave 2 implements run_sql."""
from __future__ import annotations

import pytest

from spelunk.core.connection import connect
from spelunk.core.query import run_sql
from spelunk.core.types import UnsafeSQLError


def test_select_returns_rows(sample_db):
    engine = connect(sample_db)
    r = run_sql(engine, "SELECT name FROM customers ORDER BY id")
    assert [c.lower() for c in r.columns] == ["name"]
    assert r.row_count >= 1


def test_auto_limit_truncates(sample_db):
    engine = connect(sample_db)
    r = run_sql(engine, "SELECT * FROM customers", max_rows=2)
    assert r.row_count <= 2
    assert r.truncated is True


def test_write_is_blocked(sample_db):
    engine = connect(sample_db)
    with pytest.raises(UnsafeSQLError):
        run_sql(engine, "DELETE FROM orders")
