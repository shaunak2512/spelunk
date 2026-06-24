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

from spelunk.core.connection import _normalize_driver  # noqa: E402


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
