"""Tests for spelunk.core.sources — spec parsing, kind detection, and attaching."""
from __future__ import annotations

import duckdb
import pytest

from spelunk.core import sources


# --------------------------------------------------------------------------- #
# Spec parsing + kind detection
# --------------------------------------------------------------------------- #
class TestParseSpec:
    def test_no_prefix(self):
        assert sources.parse_spec("./data/sales.parquet") == (None, "./data/sales.parquet")

    def test_name_prefix(self):
        assert sources.parse_spec("sales=./data/x.csv") == ("sales", "./data/x.csv")

    def test_dsn_equals_not_treated_as_prefix(self):
        # The first '=' sits inside the query string; left of it is not an identifier.
        name, loc = sources.parse_spec("postgresql://u:p@h/db?sslmode=require")
        assert name is None
        assert loc == "postgresql://u:p@h/db?sslmode=require"


class TestDetectKind:
    @pytest.mark.parametrize(
        "loc,kind",
        [
            ("a.csv", "file"),
            ("a.parquet", "file"),
            ("a.json", "file"),
            ("a.xlsx", "file"),
            ("a.sqlite", "sqlite"),
            ("a.db", "sqlite"),
            ("sqlite:///x.db", "sqlite"),
            ("postgresql://u@h/d", "postgres"),
            ("mysql://u@h/d", "mysql"),
        ],
    )
    def test_detect(self, loc, kind):
        assert sources.detect_kind(loc) == kind

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            sources.detect_kind("mystery.xyz")

    def test_sql_server_unsupported(self):
        # DuckDB can't attach SQL Server; it's rejected with a clear message (no fallback).
        with pytest.raises(ValueError, match="SQL Server sources are not supported"):
            sources.detect_kind("mssql://u@h/d")


class TestDeriveName:
    def test_file_stem(self):
        src = sources.build_source("./reports/Q1 sales.csv")
        assert src.name == "q1_sales"  # sanitized + lowercased

    def test_dsn_uses_db_name(self):
        src = sources.build_source("sqlite:///C:/data/financial.db")
        assert src.name == "financial"


# --------------------------------------------------------------------------- #
# attach_all wires sources into a real DuckDB connection
# --------------------------------------------------------------------------- #
class TestAttachAll:
    def test_attach_sqlite_and_file(self, sqlite_file, csv_file):
        con = duckdb.connect()
        srcs = sources.attach_all(con, [f"shop={sqlite_file}", f"orders={csv_file}"])
        kinds = {s.name: s.kind for s in srcs}
        assert kinds == {"shop": "sqlite", "orders": "file"}
        # The attached DB is queryable as "<source>"."<table>"...
        n = con.execute('SELECT COUNT(*) FROM "shop"."customers"').fetchone()[0]
        assert n == 3
        # ...and the file source as a bare view.
        n2 = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        assert n2 == 3

    def test_parquet_source(self, parquet_file):
        con = duckdb.connect()
        sources.attach_all(con, [f"regions={parquet_file}"])
        rows = con.execute("SELECT city FROM regions ORDER BY city").fetchall()
        assert [r[0] for r in rows] == ["Melbourne", "Sydney"]

    def test_duplicate_name_raises(self, sqlite_file, csv_file):
        con = duckdb.connect()
        with pytest.raises(ValueError, match="Duplicate source name"):
            sources.attach_all(con, [f"dup={sqlite_file}", f"dup={csv_file}"])

    def test_attached_sqlite_is_read_only(self, sqlite_file):
        con = duckdb.connect()
        sources.attach_all(con, [f"shop={sqlite_file}"])
        with pytest.raises(duckdb.Error):
            con.execute('INSERT INTO "shop"."customers" VALUES (99, \'X\', \'Y\', \'2024-01-01\')')


class TestTeardownSql:
    def test_file_drops_view(self):
        src = sources.build_source("orders=./data/orders.csv")
        assert sources.teardown_sql(src) == ['DROP VIEW IF EXISTS main."orders"']

    def test_attached_db_detaches(self, sqlite_file):
        src = sources.build_source(f"shop={sqlite_file}")
        assert sources.teardown_sql(src) == ['DETACH "shop"']


class TestAttachTarget:
    """The Postgres/MySQL DSN -> key=value string, parsed with the stdlib (no SQLAlchemy)."""

    def test_postgres_dsn(self):
        target = sources._attach_target(
            "postgres", "postgresql://alice:s3cret@db.example:5432/analytics?sslmode=require"
        )
        assert "host=db.example" in target
        assert "port=5432" in target
        assert "user=alice" in target
        assert "password=s3cret" in target
        assert "dbname=analytics" in target
        assert "sslmode=require" in target

    def test_mysql_uses_database_key(self):
        target = sources._attach_target("mysql", "mysql://root@localhost/shop")
        assert "database=shop" in target
        assert "dbname" not in target

    def test_percent_encoded_password_decoded(self):
        target = sources._attach_target("postgres", "postgresql://u:p%40ss@h/d")
        assert "password=p@ss" in target

    def test_teardown_undoes_attach(self, sqlite_file):
        con = duckdb.connect()
        (src,) = sources.attach_all(con, [f"shop={sqlite_file}"])
        con.execute('SELECT COUNT(*) FROM "shop"."customers"')  # attached
        for stmt in sources.teardown_sql(src):
            con.execute(stmt)
        with pytest.raises(duckdb.Error):
            con.execute('SELECT COUNT(*) FROM "shop"."customers"')  # detached
