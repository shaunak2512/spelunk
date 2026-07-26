"""Shared fixtures. The fixture builds the sample DB with stdlib sqlite3 (NOT spelunk.core),
so it works regardless of whether the core functions are implemented yet.

The ``api`` fixture is a local threaded mock HTTP server shared by the ``api:`` source tests
and the ``fetch`` tests — no network anywhere in the suite.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parts = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        srv = self.server
        srv.calls.append(
            {
                "path": parts.path,
                "query": query,
                # The parsed dict collapses repeated keys; serialization tests need the raw form.
                "query_string": parts.query,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        handler = srv.handlers.get(parts.path)
        if handler is None:
            self._send(404, {"error": f"no route {parts.path}"}, {})
            return
        nth = sum(1 for c in srv.calls if c["path"] == parts.path)  # 1-based, per path
        status, payload, extra = handler(nth, query)
        self._send(status, payload, extra)

    def _send(self, status, payload, extra_headers):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, val in extra_headers.items():
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request stderr noise
        pass


@pytest.fixture()
def api():
    """A local mock API: set ``api.handlers[path] = fn(nth_call, query) -> (status, payload,
    extra_headers)``; requests are logged to ``api.calls``. ``api.base`` is the URL root."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.calls = []
    srv.handlers = {}
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


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


@pytest.fixture
def sqlite_file(sample_db) -> str:
    """The sample DB as a raw filesystem path (for DuckSession source specs)."""
    return sample_db[len("sqlite:///"):]


@pytest.fixture
def csv_file(tmp_path) -> str:
    """A small CSV of orders, returned as a filesystem path."""
    import csv

    p = tmp_path / "orders.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["oid", "customer_id", "amount"])
        for row in [(1, 1, 120.5), (2, 1, 75.0), (3, 2, 250.0)]:
            w.writerow(row)
    return str(p)


@pytest.fixture
def parquet_file(tmp_path) -> str:
    """A small Parquet file, written via DuckDB itself (no pyarrow needed)."""
    import duckdb

    p = tmp_path / "regions.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * FROM (VALUES ('Sydney', 'NSW'), ('Melbourne', 'VIC')) AS t(city, state)) "
        f"TO '{str(p).replace(chr(92), '/')}' (FORMAT PARQUET)"
    )
    con.close()
    return str(p)
