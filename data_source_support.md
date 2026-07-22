# Analytics data sources → DuckDB attach method → Spelunk support

Comprehensive survey of popular file formats, databases, warehouses, and lakehouses used for
analytics, the most straightforward way to attach each to **DuckDB**, and whether **Spelunk in its
current form** can attach it.

**Legend (Spelunk today):** ✅ works now · ⚠️ works only accidentally/partially · ❌ not wired up
(even where DuckDB itself supports it).

Spelunk's registry (`spelunk/core/sources.py`) only builds four file readers and three `ATTACH`
kinds; anything else is rejected at `detect_kind()`. So every "❌" is a **Spelunk** gap, not
necessarily a DuckDB one — most are a few lines away.

## 1. Local file formats

| Source | Auth | DuckDB method | Core/Comm. | Spelunk |
|---|---|---|---|---|
| CSV / TSV / TXT | — | `read_csv_auto(path)` | core | ✅ |
| Parquet | — | `read_parquet(path)` | core | ✅ |
| JSON / NDJSON / JSONL | — | `read_json_auto(path)` | core | ✅ |
| Excel (xlsx/xlsm/xls) | — | `read_xlsx(path)` (`excel` ext) | core | ✅ |
| Avro | — | `read_avro(path)` (`avro` ext) | core | ✅ (`.avro` mapped, ext auto-loaded) |
| Arrow IPC / Feather | — | `read_arrow` / `nanoarrow` ext | community | ❌ |
| Lance | — | `lance` ext (read/write) | core | ❌ |
| Apache ORC | — | *no native reader* | — | ❌ (also n/a in DuckDB) |
| Google Sheets | ✅ OAuth | `gsheets` ext | community | ❌ |

## 2. Cloud object storage (same file formats, remote path)

| Source | Auth | DuckDB method | Core/Comm. | Spelunk |
|---|---|---|---|---|
| HTTP/HTTPS URL | no | `httpfs` + `read_parquet('https://…')` | core | ✅ (URL passed through, `httpfs` auto-loaded) |
| S3 (public bucket) | no | `httpfs`, `read_*('s3://…')` | core | ✅ (`httpfs` auto-loaded; `s3_region` falls back to `us-east-1` only when unset — a region you configured is kept) |
| S3 (private) | ✅ | `httpfs` + `CREATE SECRET (TYPE s3, KEY_ID, SECRET, REGION)` | core | ⚠️ reachable (`s3://` wired) but Spelunk has no secret mechanism — needs a DuckDB secret in the environment |
| Google Cloud Storage | ✅ | `httpfs` + `CREATE SECRET (TYPE gcs, …)` (HMAC) | core | ⚠️ `gs://` wired (`httpfs`); private/HMAC access needs a user secret |
| Azure Blob / ADLS | no / ✅ | `azure` ext + `CREATE SECRET (TYPE azure, …)` | core | ⚠️ `az://` wired (`azure` ext auto-loaded); auth needs a user secret |

## 3. OLTP / relational databases

| Source | Auth | DuckDB method | Core/Comm. | Spelunk |
|---|---|---|---|---|
| SQLite | — | `ATTACH 'f.db' (TYPE sqlite, READ_ONLY)` | core | ✅ |
| PostgreSQL (trust/local) | no | `ATTACH 'dbname=… host=…' (TYPE postgres)` | core | ✅ |
| PostgreSQL (user+pw) | ✅ | same, DSN carries `user=`/`password=` | core | ✅ (`postgresql://u:p@h/db`) |
| MySQL / MariaDB (no pw) | no | `ATTACH '…' (TYPE mysql, READ_ONLY)` | core | ✅ |
| MySQL / MariaDB (auth) | ✅ | same, DSN carries creds | core | ✅ |
| SQL Server | ✅ | *no DuckDB scanner* — via `adbc` ext or export | community | ❌ (explicitly rejected) |
| Oracle | ✅ | via `adbc` ext (ADBC driver) | community | ❌ |
| MongoDB | ✅ | no stable extension | — | ❌ |
| Any ADBC-driver DB | ✅ | `adbc` / `adbc_scanner` ext | community | ❌ |

## 4. Data warehouses

| Source | Auth | DuckDB method | Core/Comm. | Spelunk |
|---|---|---|---|---|
| Amazon Redshift | ✅ | Postgres wire-compatible → `postgres` ext `ATTACH` | core | ⚠️ likely works via `postgresql://` DSN (untested; some type quirks) |
| Google BigQuery | ✅ (always) | `ATTACH '…' (TYPE bigquery)` (`bigquery` ext) | community | ❌ |
| Snowflake | ✅ (always) | `ATTACH '…' (TYPE snowflake)` via ADBC (`snowflake` ext) | community | ❌ |
| ClickHouse | no / ✅ | `chsql` / `chsql_native` ext | community | ❌ |
| Databricks SQL | ✅ | `unity_catalog` ext or ADBC | core/comm. | ❌ |

## 5. Lakehouse / open table formats

| Source | Auth | DuckDB method | Core/Comm. | Spelunk |
|---|---|---|---|---|
| Iceberg (files, local/S3) | no / ✅ | `iceberg` ext, `iceberg_scan(path)` | core | ✅ (`iceberg:<path>`, VIEW over `iceberg_scan`, `allow_moved_paths`) |
| Iceberg REST catalog | ✅ | `ATTACH '…' (TYPE iceberg)` + secret | core | ❌ (catalog attach + secret not wired) |
| Delta Lake | no / ✅ | `delta` ext, `delta_scan(path)` / `ATTACH` | core | ✅ (`delta:<path>`, VIEW over `delta_scan`) |
| DuckLake | no / ✅ | `ATTACH '…' (TYPE ducklake)` (`ducklake` ext) | core | ✅ (`ducklake:<catalog>` ATTACHed READ_ONLY) |
| Unity Catalog | ✅ | `ATTACH '…' (TYPE uc_catalog)` (`unity_catalog` ext) | core | ❌ |
| Hive Metastore | no / ✅ | `hive_metastore` ext (attach as catalog) | community | ❌ |
| Apache Paimon | no | `duckdb-paimon` ext | community | ❌ |
| Apache Hudi | — | *no native reader* (read underlying Parquet) | — | ❌ |
| Lance (dataset) | no | `lance` ext | core | ❌ |

## 6. Other interfaces

| Source | Auth | DuckDB method | Core/Comm. | Spelunk |
|---|---|---|---|---|
| Arrow Flight servers | ✅ | `airport` ext | community | ❌ |
| REST / OData / GraphQL (incl. SAP) | ✅ | `erpl_web` ext | community | ❌ |
| SharePoint/OneDrive/Drive/Dropbox/SFTP | ✅ | `cloudfs` ext | community | ❌ |
| Google Firestore | ✅ | `fire_duck_ext` | community | ❌ |

## Summary of Spelunk's current reach

5 local file families (CSV/Parquet/JSON/Excel/**Avro**) — local **or remote** (`https://`,
`s3://`, `gs://`, `az://`, filesystem ext auto-loaded) — plus 3 attached DBs
(SQLite/Postgres/MySQL) and the core lakehouse formats **Delta / Iceberg / DuckLake**. All
read-only.

The **cheapest high-value wins have now landed** (`spelunk/core/sources.py`): `_duck_path` passes
remote URLs through instead of running them through `os.path.abspath`, `httpfs`/`azure` load on
demand, and `delta:<path>` / `iceberg:<path>` / `ducklake:<catalog>` map to `delta_scan` /
`iceberg_scan` / a DuckLake `ATTACH`. Verified end-to-end against public datasets (DuckDB blobs,
the Ookla open-data S3 bucket, the Apache Avro sample, a `deltalake`-written table, DuckDB's
`lineitem_iceberg`, and a locally-built DuckLake catalog).

Remaining edges:

- **Redshift** would probably attach today through a `postgresql://` DSN (wire-compatible), though
  Spelunk neither documents nor tests it.
- **Private** cloud storage (S3/GCS/Azure with credentials) and **Iceberg REST catalogs** are
  reachable at the DuckDB level but need a `CREATE SECRET` that Spelunk has no interface for yet —
  the next win would be surfacing secret/region configuration.

## Sources

- [DuckDB core extensions](https://duckdb.org/docs/current/core_extensions/overview)
- [DuckDB lakehouse formats](https://duckdb.org/docs/current/lakehouse_formats)
- [DuckDB community extensions list](https://duckdb.org/community_extensions/list_of_extensions)
- [httpfs / S3 API](https://duckdb.org/docs/current/core_extensions/httpfs/s3api)
- [Snowflake community extension](https://duckdb.org/community_extensions/extensions/snowflake)
- [Avro extension](https://duckdb.org/docs/current/core_extensions/avro)
- [DuckLake extension](https://duckdb.org/docs/lts/core_extensions/ducklake)
