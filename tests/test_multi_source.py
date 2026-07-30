"""One query across genuinely different engines — the headline architectural claim.

Claims QRY-001 (Parquet x Postgres x SQLite x a prior result in one SELECT), SRC-003
(databases attach READ_ONLY) and QRY-019 (paste-ready object names, including the
<source>.<schema>.<table> form that only exists off a non-default schema).

These need a live server; without SPELUNK_TEST_POSTGRES_DSN / SPELUNK_TEST_MYSQL_DSN they
skip. See tests/conftest.py for the one-liner that provides one.
"""
from __future__ import annotations

import duckdb
import pytest

from spelunk.core.duck import DuckSession


@pytest.fixture
def multi(postgres_dsn, sqlite_file, parquet_file):
    """A session spanning three engines at once: Postgres, SQLite and a Parquet file."""
    s = DuckSession.open(
        [f"pg={postgres_dsn}", f"shop={sqlite_file}", f"regions={parquet_file}"]
    )
    yield s
    s.close()


class TestCrossEngineJoin:
    def test_three_engines_in_one_select(self, multi):
        """Parquet x Postgres x SQLite, joined in a single statement."""
        r = multi.query(
            'SELECT e.name, r.state, COUNT(o.id) AS orders '
            'FROM "pg"."employees" e '
            "JOIN regions r ON r.city = e.city "
            'LEFT JOIN "shop"."orders" o ON o.customer_id = e.id '
            "GROUP BY e.name, r.state ORDER BY e.name",
            "cross_engine",
        )
        assert [c["name"] for c in r["columns"]] == ["name", "state", "orders"]
        assert r["row_count"] == 3
        assert r["sample"] == [["Ada", "NSW", 2], ["Grace", "NSW", 0], ["Linus", "VIC", 1]]

    def test_a_prior_result_joins_back_into_the_same_query(self, multi):
        """The fourth input: a result built two steps ago, referenced by bare name."""
        multi.query(
            'SELECT id, salary FROM "pg"."hr"."salaries" WHERE salary > 100000', "well_paid"
        )
        r = multi.query(
            'SELECT e.name, r.state, w.salary '
            'FROM "pg"."employees" e '
            "JOIN well_paid w ON w.id = e.id "
            "JOIN regions r ON r.city = e.city "
            "ORDER BY w.salary DESC",
            "paid_by_state",
        )
        assert r["sample"] == [["Grace", "NSW", 150000], ["Ada", "NSW", 120000]]

    def test_postgres_is_attached_read_only(self, multi):
        """SRC-003 for a real server, not just the ATTACH string."""
        with pytest.raises(duckdb.Error):
            multi._con.execute('INSERT INTO "pg"."employees" VALUES (99, \'Mallory\', \'Perth\')')
        rows = multi.query('SELECT COUNT(*) AS n FROM "pg"."employees"', "n")
        assert rows["sample"] == [[3]]


class TestObjectNaming:
    def test_non_default_schema_is_listed_three_part(self, multi):
        """QRY-019's <source>.<schema>.<table> form — unreachable without a real server."""
        names = {o.name for o in multi.list_objects()}
        assert "pg.employees" in names
        assert "pg.hr.salaries" in names

    def test_every_listed_name_is_paste_ready(self, multi):
        """'Paste-ready' asserted generatively: each listed name must survive being quoted
        into a SELECT. A naming change that breaks agents fails here, not in production."""
        for obj in multi.list_objects():
            quoted = ".".join(f'"{part}"' for part in obj.name.split("."))
            r = multi.query(f"SELECT * FROM {quoted}", "paste_check")
            assert r["row_count"] >= 0


class TestMySQL:
    def test_mysql_joins_and_is_read_only(self, mysql_dsn, parquet_file):
        s = DuckSession.open([f"my={mysql_dsn}", f"regions={parquet_file}"])
        try:
            r = s.query(
                'SELECT e.name, r.state FROM "my"."employees" e '
                "JOIN regions r ON r.city = e.city ORDER BY e.name",
                "my_join",
            )
            assert r["sample"] == [["Ada", "NSW"], ["Grace", "NSW"], ["Linus", "VIC"]]
            with pytest.raises(duckdb.Error):
                s._con.execute('INSERT INTO "my"."employees" VALUES (99, \'Mallory\', \'Perth\')')
        finally:
            s.close()
