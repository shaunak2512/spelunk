"""Shared fixtures. The fixture builds the sample DB with stdlib sqlite3 (NOT spelunk.core),
so it works regardless of whether the core functions are implemented yet."""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def sample_db(tmp_path) -> str:
    """A tiny 2-table SQLite DB with a foreign key. Returns its SQLAlchemy DSN."""
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
        CREATE INDEX idx_orders_customer_id ON orders (customer_id);
        CREATE UNIQUE INDEX idx_customers_city ON customers (city);
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
    return f"sqlite:///{db.as_posix()}"
