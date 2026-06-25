"""Contract tests for core.connection. RED until Wave 1 implements connect()."""
from __future__ import annotations

import pytest

from spelunk.core.connection import connect


def test_connect_returns_usable_engine(sample_db):
    engine = connect(sample_db)
    assert engine is not None
    with engine.connect():  # opens without error
        pass


def test_read_only_blocks_writes(sample_db):
    from sqlalchemy import text

    engine = connect(sample_db, read_only=True)
    with pytest.raises(Exception):  # noqa: B017 - any failure on a write is acceptable
        with engine.connect() as c:
            c.execute(text("INSERT INTO customers VALUES (9, 'x', NULL, NULL)"))
            c.commit()


# -- Server-dialect driver normalization (pure URL logic; no live DB needed) ----

from sqlalchemy.engine import make_url  # noqa: E402

from spelunk.core.connection import (  # noqa: E402
    _append_pg_option,
    _apply_require_tls,
    _normalize_driver,
)


@pytest.mark.parametrize(
    "dsn,expected_driver",
    [
        ("postgresql://u:p@h/db", "postgresql+psycopg"),
        ("mysql://u:p@h/db", "mysql+pymysql"),
        ("mariadb://u:p@h/db", "mariadb+pymysql"),
        ("mssql://u:p@h/db", "mssql+pyodbc"),
    ],
)
def test_normalize_driver_fills_default(dsn, expected_driver):
    url = make_url(dsn)
    assert _normalize_driver(url, url.get_backend_name()).drivername == expected_driver


def test_normalize_driver_respects_explicit_driver():
    url = make_url("postgresql+psycopg2://u:p@h/db")
    assert _normalize_driver(url, "postgresql").drivername == "postgresql+psycopg2"


def test_normalize_driver_leaves_sqlite_untouched():
    url = make_url("sqlite:///x.db")
    assert _normalize_driver(url, "sqlite").drivername == "sqlite"


# -- connect_args passthrough (integration on the sqlite path) ------------------

def test_connect_args_threaded_through(sample_db):
    from sqlalchemy import text

    # A standard sqlite3 connect kwarg reaches the DBAPI without breaking the path.
    engine = connect(sample_db, connect_args={"check_same_thread": False})
    with engine.connect() as c:
        assert c.execute(text("SELECT 1")).scalar() == 1


def test_require_tls_is_noop_for_sqlite(sample_db):
    from sqlalchemy import text

    engine = connect(sample_db, require_tls=True)  # ignored, not an error
    with engine.connect() as c:
        assert c.execute(text("SELECT 1")).scalar() == 1


# -- Postgres libpq options merge (pure) ----------------------------------------

def test_append_pg_option_into_empty():
    a: dict = {}
    _append_pg_option(a, "default_transaction_read_only=on")
    assert a["options"] == "-c default_transaction_read_only=on"


def test_append_pg_option_preserves_caller_options():
    a = {"options": "-c search_path=myschema"}
    _append_pg_option(a, "default_transaction_read_only=on")
    assert a["options"] == "-c search_path=myschema -c default_transaction_read_only=on"


# -- require_tls per dialect (pure URL / args logic; no live DB) -----------------

def test_require_tls_postgres_sets_sslmode():
    out = _apply_require_tls(make_url("postgresql+psycopg://u:p@h/db"), "postgresql", {})
    assert out.query.get("sslmode") == "require"


def test_require_tls_respects_existing_sslmode():
    url = make_url("postgresql+psycopg://u:p@h/db?sslmode=verify-full")
    out = _apply_require_tls(url, "postgresql", {})
    assert out.query.get("sslmode") == "verify-full"  # not overridden


def test_require_tls_mssql_sets_encrypt():
    out = _apply_require_tls(make_url("mssql+pyodbc://u:p@h/db"), "mssql", {})
    assert out.query.get("Encrypt") == "yes"


def test_require_tls_mysql_enables_ssl_connect_arg():
    args: dict = {}
    _apply_require_tls(make_url("mysql+pymysql://u:p@h/db"), "mysql", args)
    assert args.get("ssl")  # non-empty dict -> pymysql enables TLS
