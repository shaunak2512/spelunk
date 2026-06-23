"""Database connection management (Wave 1)."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url


def connect(dsn: str, *, read_only: bool = True) -> Engine:
    """Return a pooled SQLAlchemy ``Engine`` for ``dsn``.

    Contract:
      * ``read_only=True`` must block writes as defence-in-depth alongside ``guard`` —
        e.g. SQLite ``?mode=ro`` / immutable, or a read-only role/transaction for server DBs.
      * use SQLAlchemy's default connection pooling.
      * ``dsn`` is a standard SQLAlchemy URL, e.g. ``sqlite:///path/to.db``.

    Read-only enforcement is dialect-specific:
      * **SQLite** (the tested path): the URL is rewritten to SQLite's read-only URI
        form ``sqlite:///file:<abs path>?mode=ro&uri=true``. SQLite opens the file with
        ``O_RDONLY`` so any write (INSERT/UPDATE/DELETE/DDL) raises
        ``sqlalchemy.exc.OperationalError`` ("attempt to write a readonly database").
      * **Other dialects** (best-effort, NOT exercised by tests): a ``begin`` event sets
        the session/transaction read-only. For PostgreSQL we emit
        ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY``. This is documented but
        intentionally minimal — server-side read-only roles are the real defence.
    """
    url = make_url(dsn)

    if not read_only:
        return create_engine(url)

    backend = url.get_backend_name()

    if backend == "sqlite":
        return create_engine(_sqlite_read_only_url(url))

    # Best-effort, non-tested path for server dialects.
    engine = create_engine(url)

    if backend in ("postgresql", "postgres"):

        @event.listens_for(engine, "begin")
        def _set_read_only(conn):  # pragma: no cover - not exercised by tests
            conn.exec_driver_sql(
                "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
            )

    return engine


def _sqlite_read_only_url(url):
    """Rewrite a ``sqlite:///<path>`` URL to its read-only URI form.

    SQLite's URI filename syntax (``mode=ro&uri=true``) opens the database file
    read-only at the OS level, so writes fail with ``OperationalError`` rather than
    being silently swallowed.

    Edge cases handled:
      * In-memory / no-path DSNs (``sqlite://`` / ``sqlite:///:memory:``) are left
        untouched — there is no file to open read-only, and a fresh in-memory DB has
        no schema to read anyway.
      * The path is made absolute and forward-slashed so the resulting ``file:`` URI is
        valid on Windows (drive letters) and POSIX alike.
      * ``query`` is built via ``set`` so SQLAlchemy URL-encodes it correctly; ``uri=true``
        tells the pysqlite driver to treat the database name as a URI filename.
    """
    import os

    db_path = url.database

    # In-memory or driver-default databases: nothing to open read-only.
    if not db_path or db_path == ":memory:":
        return url

    abs_path = os.path.abspath(db_path).replace("\\", "/")

    # Build the URI-filename form: sqlite:///file:<abs path>?mode=ro&uri=true
    return url.set(database=f"file:{abs_path}").update_query_dict(
        {"mode": "ro", "uri": "true"}, append=False
    )
