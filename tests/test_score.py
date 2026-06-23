"""Acceptance tests for BIRD-style execution accuracy (``spelunk.eval.score``).

Written test-first. We build a tiny SQLite DB directly with stdlib ``sqlite3`` (the same
pattern as ``conftest.sample_db``) so these tests don't depend on any other spelunk module.
``run_sql_raw`` / ``execution_accuracy`` take a plain filesystem path, so we build the DB at
a ``tmp_path`` file rather than reusing the fixture's SQLAlchemy DSN string.
"""
from __future__ import annotations

import sqlite3

import pytest

from spelunk.eval.score import (
    compare_result_sets,
    execution_accuracy,
    run_sql_raw,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    """A tiny 2-table SQLite DB. Returns a plain filesystem path (for stdlib sqlite3)."""
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE customers (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            city        TEXT,
            signup_date TEXT
        );
        CREATE TABLE orders (
            id          INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount      REAL,
            status      TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        """
    )
    con.executemany(
        "INSERT INTO customers VALUES (?,?,?,?)",
        [
            (1, "Ada", "Sydney", "2024-01-05"),
            (2, "Linus", "Melbourne", "2024-03-12"),
            (3, "Grace", None, "2024-06-01"),
        ],
    )
    con.executemany(
        "INSERT INTO orders VALUES (?,?,?,?)",
        [
            (1, 1, 120.50, "shipped"),
            (2, 1, 75.00, "pending"),
            (3, 2, 250.00, "shipped"),
        ],
    )
    con.commit()
    con.close()
    return str(db)


# --- run_sql_raw -----------------------------------------------------------------


def test_run_sql_raw_returns_list_of_tuples(db_path):
    rows = run_sql_raw(db_path, "SELECT id, name FROM customers ORDER BY id")
    assert rows == [(1, "Ada"), (2, "Linus"), (3, "Grace")]
    assert all(isinstance(r, tuple) for r in rows)


# --- compare_result_sets ---------------------------------------------------------


def test_compare_identical_lists_true():
    assert compare_result_sets([(1, "a"), (2, "b")], [(1, "a"), (2, "b")]) is True


def test_compare_order_insensitive_true():
    # BIRD is order-insensitive: same rows in a different order still match.
    assert compare_result_sets([(1, "a"), (2, "b")], [(2, "b"), (1, "a")]) is True


def test_compare_multiset_duplicates_matter():
    # Multiset semantics: row counts must match.
    assert compare_result_sets([(1,), (1,)], [(1,)]) is False
    assert compare_result_sets([(1,), (1,)], [(1,), (1,)]) is True


def test_compare_different_rows_false():
    assert compare_result_sets([(1, "a")], [(1, "b")]) is False


def test_compare_empty_sets_true():
    assert compare_result_sets([], []) is True


# --- execution_accuracy ----------------------------------------------------------


def test_identical_query_true(db_path):
    sql = "SELECT name FROM customers WHERE city = 'Sydney'"
    assert execution_accuracy(sql, sql, db_path) is True


def test_semantically_equal_differently_written_true(db_path):
    # Different syntax, different column order in source, but same result set
    # (order-insensitive comparison makes the ORDER BY / table aliasing irrelevant).
    gold = "SELECT id FROM customers WHERE city IN ('Sydney', 'Melbourne')"
    pred = (
        "SELECT c.id FROM customers AS c "
        "WHERE c.city = 'Melbourne' OR c.city = 'Sydney' "
        "ORDER BY c.id DESC"
    )
    assert execution_accuracy(pred, gold, db_path) is True


def test_join_aggregation_equivalence_true(db_path):
    gold = (
        "SELECT customers.name, SUM(orders.amount) "
        "FROM customers JOIN orders ON customers.id = orders.customer_id "
        "GROUP BY customers.id"
    )
    pred = (
        "SELECT c.name, SUM(o.amount) AS total "
        "FROM orders o JOIN customers c ON o.customer_id = c.id "
        "GROUP BY c.name "
        "ORDER BY total"
    )
    assert execution_accuracy(pred, gold, db_path) is True


def test_different_result_false(db_path):
    gold = "SELECT name FROM customers WHERE city = 'Sydney'"
    pred = "SELECT name FROM customers WHERE city = 'Melbourne'"
    assert execution_accuracy(pred, gold, db_path) is False


def test_malformed_prediction_false_no_raise(db_path):
    gold = "SELECT name FROM customers"
    pred = "SELECT nme FROM custmers WHERE"  # syntax + unknown identifiers
    # Must NOT raise — a prediction that errors scores as False.
    assert execution_accuracy(pred, gold, db_path) is False


def test_malformed_gold_also_false_no_raise(db_path):
    # Even a broken gold query must not raise out of execution_accuracy.
    gold = "SELECT FROM WHERE )("
    pred = "SELECT name FROM customers"
    assert execution_accuracy(pred, gold, db_path) is False


def test_nonexistent_db_false_no_raise(tmp_path):
    # sqlite3.connect auto-creates an empty DB file, so query a missing table to
    # force both sides to error -> False, no exception raised.
    missing = str(tmp_path / "does_not_exist.db")
    assert execution_accuracy(
        "SELECT * FROM customers", "SELECT * FROM customers", missing
    ) is False
