"""Database connection management (Wave 1).

Connects to SQLite (the eval path) plus the three server dialects the MCP path
targets against *existing* databases: **PostgreSQL**, **MySQL/MariaDB**, and
**Microsoft SQL Server**. Drivers for the server dialects ship in the optional
``servers`` extra (``pip install spelunk[servers]``).
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url

# When a DSN names only the backend (e.g. ``postgresql://`` with no ``+driver``),
# default to a driver we ship in the ``servers`` extra so an off-the-shelf DSN
# Just Works. An explicit ``+driver`` in the DSN is always respected.
_DEFAULT_DRIVERS = {
    "postgresql": "psycopg",   # psycopg 3; install with spelunk[servers]
    "mysql": "pymysql",        # pure-Python, no system libs
    "mariadb": "pymysql",
    "mssql": "pyodbc",         # needs an ODBC driver; pymssql is an alternative
}


def connect(
    dsn: str,
    *,
    read_only: bool = True,
    connect_args: dict | None = None,
    require_tls: bool = False,
) -> Engine:
    """Return a pooled SQLAlchemy ``Engine`` for ``dsn``.

    Contract:
      * ``read_only=True`` must block writes as defence-in-depth alongside ``guard`` —
        e.g. SQLite ``?mode=ro`` / immutable, or a read-only transaction for server DBs.
      * use SQLAlchemy's default connection pooling.
      * ``dsn`` is a standard SQLAlchemy URL, e.g. ``sqlite:///path/to.db``,
        ``postgresql://user:pw@host/db``, ``mysql://user:pw@host/db``,
        ``mssql://user:pw@host/db?driver=ODBC+Driver+18+for+SQL+Server``.

    Driver selection:
      * If the DSN names only the backend (no ``+driver``), a sensible default driver
        from the ``servers`` extra is filled in (Postgres→psycopg, MySQL/MariaDB→pymysql,
        SQL Server→pyodbc). An explicit ``+driver`` is always honoured.

    Authentication / driver tuning:
      * ``connect_args`` is merged into the DBAPI ``connect()`` keyword arguments. Use it
        for auth that cannot be expressed in the URL — e.g. a pre-built ``ssl.SSLContext``,
        pyodbc ``attrs_before`` carrying an Azure AD access token, or psycopg
        ``sslrootcert``. Caller keys win; the only key we *merge* rather than overwrite is
        libpq ``options`` (the Postgres read-only flag is appended to any options you pass).
      * ``require_tls=True`` turns on transport encryption per dialect: Postgres
        ``sslmode=require``, SQL Server ``Encrypt=yes``, MySQL/MariaDB a pymysql SSL
        context. This only *encrypts* the connection — it does NOT verify the server
        certificate; for full verification pass a CA via ``connect_args`` / the DSN
        (e.g. Postgres ``sslmode=verify-full`` + ``sslrootcert``). A no-op for SQLite, and
        never overrides a TLS setting you already put in the DSN/``connect_args``.

    Read-only enforcement is dialect-specific. The guard layer (sqlglot) is the primary
    defence; these connection-level measures are defence-in-depth. For server databases
    the *real* defence is connecting with a read-only role/login — prefer that in production.
      * **SQLite** (the tested path): the URL is rewritten to SQLite's read-only URI
        form ``sqlite:///file:<abs path>?mode=ro&uri=true``. SQLite opens the file with
        ``O_RDONLY`` so any write raises ``sqlalchemy.exc.OperationalError``.
      * **PostgreSQL**: connect with ``default_transaction_read_only=on`` (libpq option),
        so every transaction on the session refuses writes.
      * **MySQL/MariaDB**: ``SET SESSION TRANSACTION READ ONLY`` is issued on each new
        DBAPI connection, making subsequent transactions read-only.
      * **SQL Server**: there is no portable session read-only switch; enforcement relies
        on the sqlglot guard plus (recommended) a read-only login. Best-effort only.

    Server engines enable ``pool_pre_ping`` so stale pooled connections to a remote DB
    are detected and recycled transparently.
    """
    url = make_url(dsn)
    backend = url.get_backend_name()
    url = _normalize_driver(url, backend)

    # Caller-supplied connect_args are the base we layer our own settings onto.
    args: dict = dict(connect_args or {})

    # ---- SQLite -----------------------------------------------------------------
    if backend == "sqlite":
        # require_tls is meaningless for a local file DB; silently ignored.
        if read_only:
            url = _sqlite_read_only_url(url)
        return create_engine(url, connect_args=args)

    # ---- Server dialects (PostgreSQL / MySQL / MariaDB / SQL Server) -------------
    if require_tls:
        url = _apply_require_tls(url, backend, args)

    if read_only and backend in ("postgresql", "postgres"):
        # libpq option; appended so it never clobbers caller-supplied options.
        _append_pg_option(args, "default_transaction_read_only=on")

    engine = create_engine(url, pool_pre_ping=True, connect_args=args)

    if read_only and backend in ("mysql", "mariadb"):

        @event.listens_for(engine, "connect")
        def _set_session_read_only(dbapi_conn, _record):  # pragma: no cover - needs a live MySQL
            cur = dbapi_conn.cursor()
            try:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            finally:
                cur.close()

    # SQL Server (and any other backend): no portable session read-only; the sqlglot
    # guard plus a read-only login are the defence. Engine is returned as-is.
    return engine


def _normalize_driver(url, backend):
    """Fill in a default DBAPI driver when the DSN names only the backend.

    ``postgresql://...`` becomes ``postgresql+psycopg://...`` etc. A DSN that already
    specifies a driver (``postgresql+psycopg2://``) is returned unchanged, as is any
    backend without a registered default (e.g. ``sqlite``).
    """
    # url.drivername is "<backend>" or "<backend>+<driver>".
    if "+" in url.drivername:
        return url
    default = _DEFAULT_DRIVERS.get(backend)
    if default is None:
        return url
    return url.set(drivername=f"{backend}+{default}")


def _append_pg_option(args: dict, option: str) -> None:
    """Append a libpq ``-c <option>`` to ``connect_args['options']`` in place.

    Preserves any ``options`` string the caller already supplied (so a read-only flag and
    a caller's ``search_path`` can coexist) rather than overwriting it.
    """
    flag = f"-c {option}"
    existing = args.get("options")
    args["options"] = f"{existing} {flag}" if existing else flag


def _apply_require_tls(url, backend, args: dict):
    """Enable transport encryption for a server dialect; return the (possibly new) URL.

    Encryption only — no certificate verification (pass a CA via ``connect_args`` / the DSN
    for that). Never overrides a TLS setting the caller already specified.
    """
    if backend in ("postgresql", "postgres"):
        if "sslmode" not in url.query and "sslmode" not in args:
            url = url.update_query_dict({"sslmode": "require"})
    elif backend == "mssql":
        if not any(k.lower() == "encrypt" for k in url.query):
            url = url.update_query_dict({"Encrypt": "yes"})
    elif backend in ("mysql", "mariadb"):
        # pymysql enables TLS when handed a non-empty ssl dict; without a CA it
        # encrypts without verifying the server certificate.
        args.setdefault("ssl", {"check_hostname": False})
    return url


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
