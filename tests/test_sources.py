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

    @pytest.mark.parametrize(
        "loc,kind",
        [
            ("https://example.com/data/sales.parquet", "file"),
            ("s3://bucket/trips/part.parquet", "file"),
            ("az://container/data.csv", "file"),
            ("data.avro", "file"),
            ("https://host/data.parquet?token=abc", "file"),  # query string ignored for ext
            ("delta:./warehouse/events", "delta"),
            ("delta:s3://bucket/tbl", "delta"),
            ("iceberg:./warehouse/tbl", "iceberg"),
            ("ducklake:./catalog.ducklake", "ducklake"),
        ],
    )
    def test_detect_new_kinds(self, loc, kind):
        assert sources.detect_kind(loc) == kind

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            sources.detect_kind("mystery.xyz")

    def test_unknown_extension_error_teaches_format_prefix(self):
        # A genuinely unrecognized, unprefixed extension errors — and the message points at the
        # format-prefix escape hatch so the agent can retry in one step.
        with pytest.raises(ValueError, match="format prefix"):
            sources.build_source("https://host/data/mystery.dat")

    def test_literal_name_prefix_is_explained(self):
        # `name=<n> <locator>` — writing the prefix placeholder literally — parses as the name
        # 'name' plus an unclassifiable locator. The error has to say so, or it reads as "this
        # URL is unsupported" and the fix (nvd=api:...) stays invisible.
        with pytest.raises(ValueError) as excinfo:
            sources.build_source("name=nvd api:https://services.nvd.nist.gov/rest/json/cves/2.0")
        msg = str(excinfo.value)
        assert "nvd=api:https://services.nvd.nist.gov/rest/json/cves/2.0" in msg
        assert "not the literal word 'name'" in msg

    def test_stripped_name_reported_on_unknown_locator(self):
        # Any consumed prefix is named when detection fails, so the split is never a mystery.
        with pytest.raises(ValueError, match="taken as the source name"):
            sources.build_source("mydata=mystery.xyz")

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

    def test_remote_url_stem(self):
        src = sources.build_source("https://example.com/data/sales.parquet?token=x")
        assert src.name == "sales"

    def test_delta_scheme_stripped_from_name(self):
        src = sources.build_source("delta:./warehouse/events")
        assert src.name == "events"

    def test_ducklake_scheme_stripped_from_name(self):
        src = sources.build_source("ducklake:./my_catalog.ducklake")
        assert src.name == "my_catalog"


# --------------------------------------------------------------------------- #
# Builders for the new source kinds emit the right setup SQL (no connection needed)
# --------------------------------------------------------------------------- #
class TestBuildNewKinds:
    def test_remote_file_loads_httpfs_and_keeps_url(self):
        src = sources.build_source("https://example.com/data/sales.parquet")
        assert src.kind == "file"
        assert ["INSTALL httpfs", "LOAD httpfs"] == src.setup_sql[:2]
        # URL passed through verbatim — NOT run through os.path.abspath.
        assert "read_parquet('https://example.com/data/sales.parquet')" in src.setup_sql[-1]

    def test_azure_url_loads_azure(self):
        src = sources.build_source("x=az://container/data.csv")
        assert ["INSTALL azure", "LOAD azure"] == src.setup_sql[:2]

    def test_avro_loads_avro_ext(self):
        src = sources.build_source("events=./data/events.avro")
        assert ["INSTALL avro", "LOAD avro"] == src.setup_sql[:2]
        assert "read_avro(" in src.setup_sql[-1]

    def test_delta_builds_scan_view(self):
        src = sources.build_source("events=delta:./warehouse/events")
        assert src.kind == "delta"
        assert "INSTALL delta" in src.setup_sql and "LOAD delta" in src.setup_sql
        assert "delta_scan(" in src.setup_sql[-1]
        assert 'CREATE OR REPLACE VIEW main."events"' in src.setup_sql[-1]
        assert sources.teardown_sql(src) == ['DROP VIEW IF EXISTS main."events"']

    def test_iceberg_remote_loads_httpfs_and_iceberg(self):
        src = sources.build_source("tbl=iceberg:s3://bucket/tbl")
        assert src.kind == "iceberg"
        # httpfs first (remote), then the iceberg reader.
        assert src.setup_sql[:2] == ["INSTALL httpfs", "LOAD httpfs"]
        # allow_moved_paths lets a relocated/relative-path table still resolve.
        assert "iceberg_scan('s3://bucket/tbl', allow_moved_paths => true)" in src.setup_sql[-1]

    def test_s3_region_fallback_is_non_destructive(self):
        # The zero-config region for public buckets is a connection-global SET, so it must not
        # clobber a region the caller already chose — it re-sets the current value when there is
        # one and only falls back to us-east-1 when unset/empty. Executed against a real DuckDB
        # connection because the guarantee lives in the SQL expression, not in Python.
        src = sources.build_source("s3://bucket/data.parquet")
        region_stmt = next(s for s in src.setup_sql if s.startswith("SET s3_region"))

        con = duckdb.connect()
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        # Unset (DuckDB reports NULL) → the fallback applies.
        con.execute(region_stmt)
        assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "us-east-1"
        # Already configured → preserved.
        con.execute("SET s3_region = 'ap-southeast-2'")
        con.execute(region_stmt)
        assert con.execute("SELECT current_setting('s3_region')").fetchone()[0] == "ap-southeast-2"
        con.close()

    def test_ducklake_attaches_read_only(self):
        src = sources.build_source("lake=ducklake:./catalog.ducklake")
        assert src.kind == "ducklake"
        assert src.setup_sql[:2] == ["INSTALL ducklake", "LOAD ducklake"]
        assert src.setup_sql[-1] == "ATTACH 'ducklake:./catalog.ducklake' AS \"lake\" (READ_ONLY)"
        assert sources.teardown_sql(src) == ['DETACH "lake"']


# --------------------------------------------------------------------------- #
# Format-override prefixes (csv:/tsv:/json:/parquet:/excel:/avro:) force a reader
# --------------------------------------------------------------------------- #
class TestFormatPrefix:
    def test_csv_prefix_forces_reader_on_odd_extension(self):
        # A .dat file that is really CSV: csv: forces read_csv_auto regardless of extension.
        src = sources.build_source("routes=csv:https://host/data/routes.dat")
        assert src.kind == "file"
        # Remote → httpfs loaded first, URL kept verbatim (no os.path.abspath mangling).
        assert src.setup_sql[:2] == ["INSTALL httpfs", "LOAD httpfs"]
        assert "read_csv_auto('https://host/data/routes.dat')" in src.setup_sql[-1]

    def test_parquet_prefix_overrides_bin_extension(self):
        src = sources.build_source("d=parquet:./data.bin")
        assert "read_parquet(" in src.setup_sql[-1]

    def test_json_prefix_overrides_txt_extension(self):
        src = sources.build_source("d=json:./data.txt")
        assert "read_json_auto(" in src.setup_sql[-1]

    def test_excel_prefix_loads_extension(self):
        src = sources.build_source("book=excel:./data.bin")
        assert ["INSTALL excel", "LOAD excel"] == src.setup_sql[-3:-1]
        assert "read_xlsx(" in src.setup_sql[-1]

    def test_prefix_is_case_insensitive(self):
        src = sources.build_source("d=CSV:./data.dat")
        assert "read_csv_auto(" in src.setup_sql[-1]

    def test_name_composes_with_format_prefix(self):
        # name= sets the source name; the format prefix is stripped from the locator.
        src = sources.build_source("myname=csv:./file.dat")
        assert src.name == "myname"

    def test_derived_name_strips_format_prefix(self):
        # With no name=, the name derives from the inner path, not the prefix.
        src = sources.build_source("csv:https://host/path/routes.dat")
        assert src.name == "routes"

    def test_windows_drive_not_mistaken_for_prefix(self):
        # A drive letter is one char; every format prefix is three+, so C:/ never matches.
        assert sources.detect_kind("C:/data/foo.csv") == "file"
        _, inner = sources._split_format_prefix("C:/data/foo.csv")
        assert inner == "C:/data/foo.csv"

    def test_unknown_prefix_falls_through_to_extension(self):
        # An unrecognized prefix is not a format override; detection falls back to the real ext.
        ext, inner = sources._split_format_prefix("foo:./bar.csv")
        assert ext is None and inner == "foo:./bar.csv"


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
