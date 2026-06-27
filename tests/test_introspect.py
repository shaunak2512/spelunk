"""Contract tests for core.introspect. RED until Wave 2 implements list_objects/describe."""
from __future__ import annotations

from spelunk.core.connection import connect
from spelunk.core.introspect import describe, list_objects


def test_list_objects_finds_tables(sample_db):
    engine = connect(sample_db)
    names = {t.name for t in list_objects(engine)}
    assert {"customers", "orders"} <= names


def test_describe_columns_and_fk(sample_db):
    engine = connect(sample_db)
    d = describe(engine, "orders")
    colnames = {c.name for c in d.columns}
    assert {"id", "customer_id", "amount", "status"} <= colnames
    # orders.customer_id -> customers.id
    assert any(fk.ref_table == "customers" for fk in d.foreign_keys)


def test_describe_indexes(sample_db):
    engine = connect(sample_db)
    d = describe(engine, "orders")
    idx_names = {i.name for i in d.indexes}
    assert "idx_orders_customer_id" in idx_names
    # the index covers customer_id
    idx = next(i for i in d.indexes if i.name == "idx_orders_customer_id")
    assert "customer_id" in idx.columns
    assert not idx.unique


def test_describe_unique_index(sample_db):
    engine = connect(sample_db)
    d = describe(engine, "customers")
    unique_idxs = [i for i in d.indexes if i.unique]
    assert any(i.name == "idx_customers_city" for i in unique_idxs)


def test_describe_profile_populated(sample_db):
    engine = connect(sample_db)
    d = describe(engine, "customers", profile=True)
    assert d.sample_rows  # a few sample rows present
    assert d.profile  # per-column stats present
