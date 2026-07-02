"""Source registry — map a user source spec to DuckDB attach/scan statements.

The unified engine is a single DuckDB connection. Every source is reached through it:

  * **files** (CSV/TSV/Parquet/JSON/Excel) are scanned with DuckDB's ``read_*`` functions and
    registered as VIEWs in the workspace ``main`` schema (the file stays the source of truth,
    so queries push projection/filters down to the scan rather than copying the file in);
  * **SQLite / PostgreSQL / MySQL** are ``ATTACH``ed read-only, each as its own catalog;
  * **SQL Server** (and anything DuckDB can't attach) falls back to a SQLAlchemy engine whose
    rows are pulled on demand by the server's ``import_remote`` tool — DuckDB has no MSSQL
    scanner, so such a source can't be queried in place.

A spec is a string, optionally prefixed ``name=``::

    sales=./data/sales.parquet
    sqlite:///C:/data/app.db
    postgresql://user:pw@host/dbname
    ./reports/q1.csv               # name derived from the filename -> q1

Attached databases are referenced in SQL by ``"<source>"."<table>"``; file sources by their
bare view name.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Literal

if TYPE_CHECKING:
    import duckdb
    from sqlalchemy.engine import Engine

SourceKind = Literal["file", "sqlite", "postgres", "mysql", "fallback"]

# File extension -> DuckDB table function used to scan it.
_FILE_READERS: dict[str, str] = {
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".txt": "read_csv_auto",
    ".parquet": "read_parquet",
    ".pq": "read_parquet",
    ".json": "read_json_auto",
    ".ndjson": "read_json_auto",
    ".jsonl": "read_json_auto",
}
# Excel is scanned via read_xlsx, which lives in the loadable `excel` extension.
_EXCEL_EXTS = frozenset({".xlsx", ".xlsm", ".xls"})
# Extensions that mean "this path is a SQLite database file" (attach, don't scan).
_SQLITE_EXTS = frozenset({".sqlite", ".sqlite3", ".db"})

# Map our attach kinds to (duckdb extension, ATTACH TYPE).
_ATTACH_EXT = {"sqlite": "sqlite", "postgres": "postgres", "mysql": "mysql"}

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")
_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,62})=(.+)$", re.DOTALL)


@dataclass
class Source:
    """One registered data source.

    ``setup_sql`` are the statements to run on the DuckDB connection to make the source
    queryable (extension loads + ATTACH/CREATE VIEW). ``engine`` is set only for ``fallback``
    sources (SQL Server / exotic auth), which are pulled via SQLAlchemy instead of attached.
    """

    name: str
    kind: SourceKind
    locator: str
    setup_sql: list[str] = field(default_factory=list)
    engine: "Engine | None" = None


def parse_spec(spec: str) -> tuple[str | None, str]:
    """Split an optional ``name=`` prefix off a spec; return ``(name_or_None, locator)``.

    The prefix is only honoured when the left side is a valid SQL identifier, so a DSN such
    as ``postgresql://u:p@h/db?sslmode=require`` (whose first ``=`` sits inside the query
    string, left of which is not an identifier) is treated wholly as the locator.
    """
    m = _PREFIX_RE.match(spec.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return None, spec.strip()


def detect_kind(locator: str) -> SourceKind:
    """Classify a locator into a :data:`SourceKind` by scheme/extension."""
    low = locator.lower()
    if low.startswith(("postgresql://", "postgres://")):
        return "postgres"
    if low.startswith(("mysql://", "mariadb://")):
        return "mysql"
    if low.startswith(("mssql://", "mssql+", "sqlserver://")):
        return "fallback"
    if low.startswith("sqlite://"):
        return "sqlite"
    ext = os.path.splitext(low)[1]
    if ext in _FILE_READERS or ext in _EXCEL_EXTS:
        return "file"
    if ext in _SQLITE_EXTS:
        return "sqlite"
    raise ValueError(
        f"Could not determine the source type of {locator!r}. Supported: files "
        f"({', '.join(sorted(set(_FILE_READERS) | _EXCEL_EXTS | _SQLITE_EXTS))}), "
        "or a sqlite:// / postgresql:// / mysql:// / mssql:// DSN."
    )


def build_source(spec: str) -> Source:
    """Parse a single spec into a :class:`Source` (no DuckDB connection touched yet)."""
    explicit, locator = parse_spec(spec)
    kind = detect_kind(locator)
    name = explicit or _derive_name(locator, kind)
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid source name {name!r}. Use a SQL identifier (letters, digits, "
            "underscores; starting with a letter or underscore; max 63 chars)."
        )

    if kind == "file":
        return _build_file_source(name, locator)
    if kind in _ATTACH_EXT:
        return _build_attach_source(name, kind, locator)

    # fallback (SQL Server / exotic): no attach — carry a read-only SQLAlchemy engine.
    from .connection import connect

    return Source(name=name, kind="fallback", locator=locator, engine=connect(locator, read_only=True))


def teardown_sql(src: Source) -> list[str]:
    """Statements that undo a source's :attr:`Source.setup_sql` — the inverse of attaching.

    A ``file`` source drops its ``main`` view; an attached database is ``DETACH``ed. A
    ``fallback`` source has no DuckDB object (its SQLAlchemy engine is disposed by the caller),
    so this returns no statements. Used by ``DuckSession.remove_source``.
    """
    if src.kind == "file":
        return [f'DROP VIEW IF EXISTS main."{src.name}"']
    if src.kind in _ATTACH_EXT:
        return [f'DETACH "{src.name}"']
    return []


def attach_all(con: "duckdb.DuckDBPyConnection", specs: Iterable[str]) -> list[Source]:
    """Build every source and run its ``setup_sql`` on *con*; return the registered sources.

    Raises on duplicate source names so two sources never collide on one catalog/view name.
    """
    sources: list[Source] = []
    seen: set[str] = set()
    for spec in specs:
        src = build_source(spec)
        if src.name in seen:
            raise ValueError(
                f"Duplicate source name {src.name!r}. Give one an explicit prefix, "
                "e.g. mydata=<spec>."
            )
        seen.add(src.name)
        for stmt in src.setup_sql:
            con.execute(stmt)
        sources.append(src)
    return sources


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _build_file_source(name: str, locator: str) -> Source:
    """A file source becomes a VIEW in `main` over the matching read_* scan."""
    ext = os.path.splitext(locator.lower())[1]
    path = _duck_path(locator)
    setup: list[str] = []
    if ext in _EXCEL_EXTS:
        setup += ["INSTALL excel", "LOAD excel"]
        scan = f"read_xlsx('{path}')"
    else:
        scan = f"{_FILE_READERS[ext]}('{path}')"
    setup.append(f'CREATE OR REPLACE VIEW main."{name}" AS SELECT * FROM {scan}')
    return Source(name=name, kind="file", locator=locator, setup_sql=setup)


def _build_attach_source(name: str, kind: SourceKind, locator: str) -> Source:
    """A database source is ATTACHed read-only as its own catalog."""
    ext_name = _ATTACH_EXT[kind]
    target = _attach_target(kind, locator)
    setup = [f"INSTALL {ext_name}", f"LOAD {ext_name}"]
    setup.append(f"ATTACH '{target}' AS \"{name}\" (TYPE {ext_name}, READ_ONLY)")
    return Source(name=name, kind=kind, locator=locator, setup_sql=setup)


def _attach_target(kind: SourceKind, locator: str) -> str:
    """Return the string DuckDB's ATTACH expects for *kind*.

    SQLite -> a filesystem path. Postgres/MySQL -> a ``key=value`` connection string built
    from the DSN (more reliable across the scanners than passing a raw URL).
    """
    if kind == "sqlite":
        if locator.lower().startswith("sqlite://"):
            rest = locator[len("sqlite://"):]
            # Strip only the single URI-separator slash, so the SQLAlchemy 4-slash absolute form
            # (sqlite:////abs/path.db -> /abs/path.db) survives; 3-slash relative stays relative.
            path = rest[1:] if rest.startswith("/") else rest
        else:
            path = locator
        return _duck_path(path)

    from sqlalchemy.engine import make_url

    url = make_url(locator)
    db_key = "database" if kind == "mysql" else "dbname"
    parts: list[str] = []
    if url.host:
        parts.append(f"host={url.host}")
    if url.port:
        parts.append(f"port={url.port}")
    if url.username:
        parts.append(f"user={url.username}")
    if url.password:
        parts.append(f"password={url.password}")
    if url.database:
        parts.append(f"{db_key}={url.database}")
    for key, val in url.query.items():
        if isinstance(val, (list, tuple)):
            val = val[0]
        parts.append(f"{key}={val}")
    return " ".join(parts).replace("'", "''")


def _derive_name(locator: str, kind: SourceKind) -> str:
    """Derive a SQL-identifier source name from a locator (filename stem or DB name)."""
    if "://" in locator:
        from sqlalchemy.engine import make_url

        try:
            url = make_url(locator)
            base = url.database or url.host or kind
        except Exception:
            base = kind
    else:
        base = locator
    stem = os.path.splitext(os.path.basename(base or kind))[0]
    name = _SANITIZE_RE.sub("_", stem).strip("_").lower() or kind
    if not re.match(r"[A-Za-z_]", name[0]):
        name = f"s_{name}"
    return name[:63]


def _duck_path(path: str) -> str:
    """Absolute, forward-slashed, single-quote-escaped path for a DuckDB string literal."""
    return os.path.abspath(path).replace("\\", "/").replace("'", "''")
