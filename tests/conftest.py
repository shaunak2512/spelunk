"""Shared fixtures. The fixture builds the sample DB with stdlib sqlite3 (NOT spelunk.core),
so it works regardless of whether the core functions are implemented yet.

The ``api`` fixture is a local threaded mock HTTP server shared by the ``api:`` source tests
and the ``fetch`` tests — no network anywhere in the suite.

The ``postgres_dsn`` / ``mysql_dsn`` fixtures are the exception: they need a real server,
because DuckDB's postgres/mysql extensions speak the wire protocol. Each reads a DSN from the
environment and skips when there isn't one.
"""
from __future__ import annotations

import json
import os
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


# --- Real database servers (Postgres / MySQL) ---------------------------------------- #
#
# Claims SRC-003 (databases attach READ_ONLY), QRY-001 (one query spanning Parquet x Postgres x
# SQLite x a prior result) and QRY-019 (<source>.<schema>.<table> naming) cannot be falsified
# without a live server: DuckDB's postgres/mysql extensions speak the real wire protocol.
#
# Point the suite at one you already have; without it these tests skip, which leaves those
# claims honestly unverified rather than silently unexercised:
#
#   $env:SPELUNK_TEST_POSTGRES_DSN = "postgresql://postgres:pw@127.0.0.1:5432/spelunk"
#   $env:SPELUNK_TEST_MYSQL_DSN    = "mysql://root:pw@127.0.0.1:3306/spelunk"
#
# Any throwaway server works, e.g.
#   docker run -d --rm -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=spelunk -p 5432:5432 postgres:16-alpine


def _seed_db(engine: str, dsn: str) -> None:
    """Create the shared `employees` table (plus, on Postgres, a non-default schema).

    Seeded through DuckDB's own extension so the fixture needs no psycopg/mysqlclient — and so
    a seed failure is a real signal that the extension cannot reach the server.
    """
    import duckdb

    con = duckdb.connect()
    con.execute(f"INSTALL {engine}; LOAD {engine};")
    con.execute(f"ATTACH '{dsn}' AS seed (TYPE {engine})")
    try:
        con.execute("DROP TABLE IF EXISTS seed.employees")
        con.execute("CREATE TABLE seed.employees (id INTEGER, name VARCHAR, city VARCHAR)")
        con.execute(
            "INSERT INTO seed.employees VALUES (1,'Ada','Sydney'),(2,'Linus','Melbourne'),"
            "(3,'Grace','Sydney')"
        )
        if engine == "postgres":
            # QRY-019's <source>.<schema>.<table> form only exists off the default schema.
            con.execute("CREATE SCHEMA IF NOT EXISTS hr")
            con.execute("DROP TABLE IF EXISTS seed.hr.salaries")
            con.execute("CREATE TABLE seed.hr.salaries (id INTEGER, salary INTEGER)")
            con.execute("INSERT INTO seed.hr.salaries VALUES (1,120000),(2,95000),(3,150000)")
    finally:
        con.close()


def _live_db(engine: str, env_var: str) -> str:
    dsn = os.environ.get(env_var)
    if not dsn:
        pytest.skip(f"needs a live {engine} server — set {env_var} (see tests/conftest.py)")
    _seed_db(engine, dsn)
    return dsn


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    return _live_db("postgres", "SPELUNK_TEST_POSTGRES_DSN")


@pytest.fixture(scope="session")
def mysql_dsn() -> str:
    return _live_db("mysql", "SPELUNK_TEST_MYSQL_DSN")


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
