"""Tests for spelunk.core.duck.DuckSession — the unified engine + workspace."""
from __future__ import annotations

import os

import pytest

from spelunk.core.duck import DuckSession
from spelunk.core.types import UnsafeSQLError


@pytest.fixture
def session(sqlite_file, csv_file):
    """A session over the sample SQLite DB ('shop') and the orders CSV ('orders')."""
    s = DuckSession.open([f"shop={sqlite_file}", f"orders={csv_file}"])
    yield s
    s.close()


class TestIntrospection:
    def test_list_objects_spans_sources(self, session):
        objs = {o.name: o for o in session.list_objects()}
        assert "shop.customers" in objs and "shop.orders" in objs
        assert "orders" in objs  # the CSV file view
        assert objs["shop.customers"].row_count == 3
        assert objs["orders"].kind == "view"

    def test_describe_columns_and_pk(self, session):
        desc = session.describe("shop.customers")
        names = [c.name for c in desc.columns]
        assert names == ["id", "name", "city", "signup_date"]
        assert desc.primary_key == ["id"]
        assert desc.row_count == 3
        assert len(desc.sample_rows) == 3


class TestQuery:
    def test_materializes_and_returns_sample(self, session):
        r = session.query("SELECT id, name FROM \"shop\".\"customers\" ORDER BY id", "cust")
        assert r["name"] == "cust"
        assert r["row_count"] == 3
        assert [c["name"] for c in r["columns"]] == ["id", "name"]
        assert r["sample"][0] == [1, "Ada"]

    def test_cross_source_join(self, session):
        r = session.query(
            'SELECT c.name, o.amount FROM "shop"."customers" c '
            "JOIN orders o ON c.id = o.customer_id",
            "joined",
        )
        assert r["row_count"] == 3

    def test_result_is_reusable_by_name(self, session):
        session.query("SELECT * FROM \"shop\".\"customers\"", "cust")
        r2 = session.query("SELECT COUNT(*) AS n FROM cust", "cnt")
        assert r2["sample"] == [[3]]

    def test_read_only_guard_rejects_writes(self, session):
        with pytest.raises(UnsafeSQLError):
            session.query("DROP TABLE orders", "x")

    def test_required_name_validated(self, session):
        with pytest.raises(ValueError):
            session.query("SELECT 1", "bad name!")

    def test_large_unfiltered_star_hint(self, session):
        # Build a >100k-row table, then SELECT * it wholesale -> nudge fires.
        session.query("SELECT i AS x FROM range(150000) t(i)", "big")
        r = session.query("SELECT * FROM big", "copy")
        assert "hints" in r and any("wholesale" in h for h in r["hints"])

    def test_small_query_has_no_hint(self, session):
        r = session.query("SELECT * FROM orders", "o")
        assert "hints" not in r


class TestProfile:
    def test_numeric_and_text_stats(self, session):
        session.query(
            'SELECT c.name, o.amount FROM "shop"."customers" c '
            "JOIN orders o ON c.id = o.customer_id",
            "joined",
        )
        stats = session.profile("SELECT * FROM joined")["columns"]
        assert stats["amount"]["min"] == 75.0
        assert stats["amount"]["max"] == 250.0
        assert "p50" in stats["amount"]  # percentiles always available in DuckDB
        assert stats["name"]["top"] == "Ada"
        assert stats["name"]["unique"] == 2


class TestFlows:
    def test_flows_isolate_same_name(self, session):
        session.query("SELECT 1 AS a", "r", flow="q1")
        session.query("SELECT 2 AS a", "r", flow="q2")
        assert session.query("SELECT a FROM q1.r", "x", flow="q1")["sample"] == [[1]]
        # cross-flow qualified reference
        assert session.query('SELECT a FROM "q2"."r"', "y", flow="q1")["sample"] == [[2]]

    def test_catalog_lists_flows_then_results(self, session):
        session.query("SELECT 1 AS a", "r1", flow="work")
        flows = {f["flow"]: f["result_count"] for f in session.catalog()["flows"]}
        assert flows.get("work") == 1
        names = [r["name"] for r in session.catalog("work")["results"]]
        assert names == ["r1"]

    def test_drop_result_then_flow(self, session):
        session.query("SELECT 1 AS a", "r1", flow="work")
        assert session.drop("r1", flow="work")["dropped"] is True
        assert session.drop("r1", flow="work")["dropped"] is False
        session.query("SELECT 1 AS a", "r2", flow="work")
        assert session.drop(flow="work")["dropped_results"] == 1

    def test_cannot_drop_reserved(self, session):
        with pytest.raises(ValueError, match="reserved"):
            session.drop(flow="main")


class TestExport:
    def test_export_result_name(self, session, tmp_path):
        session.query("SELECT * FROM orders", "o")
        out = str(tmp_path / "o.csv")
        res = session.export("o", "csv", out)
        assert res["row_count"] == 3 and os.path.exists(out)

    def test_export_raw_select(self, session, tmp_path):
        out = str(tmp_path / "q.json")
        res = session.export('SELECT * FROM "shop"."customers"', "json", out)
        assert res["row_count"] == 3 and os.path.exists(out)

    def test_export_bad_format(self, session):
        with pytest.raises(ValueError):
            session.export("orders", "xlsx", "x.xlsx")


class TestPersistence:
    def test_durable_workspace_survives_reopen(self, sqlite_file, tmp_path):
        d = str(tmp_path / "sess")
        s1 = DuckSession.open([f"shop={sqlite_file}"], session_dir=d)
        s1.query("SELECT * FROM \"shop\".\"customers\"", "kept", flow="work")
        s1.close()
        s2 = DuckSession.open([f"shop={sqlite_file}"], session_dir=d)
        try:
            assert "kept" in [r["name"] for r in s2.catalog("work")["results"]]
        finally:
            s2.close()

    def test_locked_workspace_falls_back_to_ephemeral(self, sqlite_file, tmp_path, capsys):
        """A second SERVER PROCESS on the same locked session_dir stays functional (ephemeral).

        Must use a subprocess to hold the lock: two sessions in one process would share DuckDB's
        cached instance and never hit the cross-process lock.
        """
        import os
        import subprocess
        import sys as _sys
        import textwrap

        d = str(tmp_path / "sess")
        os.makedirs(d, exist_ok=True)
        holder_code = textwrap.dedent(
            f"""
            import duckdb, os, time
            _con = duckdb.connect(os.path.join({d!r}, "workspace.duckdb"))  # keep ref -> hold lock
            print("LOCKED", flush=True)
            time.sleep(30)
            """
        )
        holder = subprocess.Popen([_sys.executable, "-c", holder_code], stdout=subprocess.PIPE)
        try:
            assert holder.stdout.readline().strip() == b"LOCKED"  # wait until it holds the lock
            s2 = DuckSession.open([f"shop={sqlite_file}"], session_dir=d)  # must NOT crash
            try:
                assert "locked" in capsys.readouterr().err  # warned on stderr
                assert s2.query("SELECT 1 AS a", "r")["sample"] == [[1]]  # fully usable
            finally:
                s2.close()
        finally:
            holder.terminate()
            holder.wait(timeout=10)
