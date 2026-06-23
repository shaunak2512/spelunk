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
