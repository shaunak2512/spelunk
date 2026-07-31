"""Source registry — map a user source spec to DuckDB attach/scan statements.

The unified engine is a single DuckDB connection. Every source is reached through it:

  * **files** (CSV/TSV/Parquet/JSON/Excel/Avro/YAML) are scanned with DuckDB's ``read_*`` functions
    and registered as VIEWs in the workspace ``main`` schema (the file stays the source of truth,
    so queries push projection/filters down to the scan rather than copying the file in). A file
    path may be local *or* a remote URL (``https://``, ``s3://``, ``gs://``, ``az://``) — the
    matching filesystem extension (``httpfs`` / ``azure``) is loaded automatically;
  * **lakehouse tables** (Delta / Iceberg) are scanned via ``delta_scan`` / ``iceberg_scan`` and
    likewise registered as VIEWs — write ``delta:<path>`` / ``iceberg:<path>``;
  * **SQLite / PostgreSQL / MySQL / DuckLake** are ``ATTACH``ed read-only, each as its own catalog;
  * **REST/JSON APIs** (``api:<url> [key=value ...]``) are fetched ONCE at attach time — with
    pagination, retries, and rate-limit backoff — into an NDJSON snapshot under the workspace,
    and registered as a VIEW over that local file. Queries run against the pinned snapshot
    (deterministic, no re-fetch per query); refresh by re-attaching. See ``apifetch.py`` for
    the option grammar (paginate=page/offset/cursor/keyset/link, records=<dot.path>,
    auth_env=/header=/param=);
  * **OpenAPI specs** (``openapi:<url-or-path> [auth_env=ENV] [header=N:ENV] [param=n:ENV]
    [default_param=n:v] [records=…] [paginate=…]``) attach an API *connection* plus its
    queryable *endpoint catalog* — one row per (path, method) with params, auth shape,
    pagination/records hints, ``response_fields`` (what the endpoint returns), and a
    paste-ready ``suggested_spec``. **One API is one source:** the connection holds the base
    URL and credentials, and ``fetch`` calls any endpoint under it — so exploring 20 endpoints
    costs 20 flow-scoped *results*, not 20 permanent sources. See ``openapi.py`` for the
    catalog and ``apifetch.py`` for the connection/request split.

Everything reachable is reached through the one DuckDB connection — there is no out-of-engine
fallback. A source DuckDB can't attach (e.g. SQL Server) is not supported; export it to a file
(Parquet/CSV) and point a ``--source`` at that instead.

A spec is a string, optionally prefixed with ``<your-chosen-name>=`` (the word before the ``=``
is the name itself — ``nvd=api:…``, never the literal token ``name=``)::

    sales=./data/sales.parquet
    config=./deploy/values.yaml                # YAML via the `yaml` community extension
    remote=https://example.com/data/sales.parquet
    routes=csv:https://example.com/data/routes.dat  # force a reader for an odd/absent extension
    trips=s3://my-bucket/trips/*.parquet
    events=delta:./warehouse/events            # a Delta Lake table directory
    catalog=iceberg:./warehouse/catalog/table  # an Iceberg table
    lake=ducklake:./catalog.ducklake           # a DuckLake catalog
    gh=api:https://api.github.com/repos/o/r/issues paginate=link   # ONE endpoint -> snapshot
    tmdb=openapi:./tmdb-api.json auth_env=TMDB_TOKEN  # the WHOLE API -> catalog + connection
    sqlite:///C:/data/app.db
    postgresql://user:pw@host/dbname
    ./reports/q1.csv               # name derived from the filename -> q1

A file whose name lacks a recognised extension (an API endpoint, a ``.dat`` dump) is read by
forcing the reader with a format prefix — ``csv:`` / ``tsv:`` / ``json:`` / ``parquet:`` /
``excel:`` / ``avro:`` / ``yaml:`` — placed on the locator (after any ``name=``): ``routes=csv:<url>``.

Attached databases (and DuckLake) are referenced in SQL by ``"<source>"."<table>"``; file and
lakehouse-scan sources by their bare view name.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

if TYPE_CHECKING:
    import duckdb

    from .apifetch import ApiConnection

SourceKind = Literal[
    "file", "sqlite", "postgres", "mysql", "delta", "iceberg", "ducklake", "api", "openapi"
]

# File extension -> DuckDB table function used to scan it (readers in the core/statically-linked
# build, no extension load needed).
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
# Extension-backed file readers: ext -> (duckdb extension, read function). These need an
# INSTALL/LOAD before the scan, unlike the statically-linked readers above.
_EXT_FILE_READERS: dict[str, tuple[str, str]] = {
    ".xlsx": ("excel", "read_xlsx"),
    ".xlsm": ("excel", "read_xlsx"),
    # NB: legacy binary .xls is intentionally absent — DuckDB's excel reader (read_xlsx) handles
    # the OOXML .xlsx/.xlsm formats only, so a .xls would fail at view creation.
    ".avro": ("avro", "read_avro"),
    # YAML comes from the community repository (see _COMMUNITY_EXTS). ``read_yaml`` unnests a
    # top-level sequence (and multi-document files) into rows, the way read_json_auto does —
    # ``read_yaml_objects`` (one row per document) is the other reader and is not wired up.
    ".yaml": ("yaml", "read_yaml"),
    ".yml": ("yaml", "read_yaml"),
}
# Extensions that live in DuckDB's *community* repository rather than core, so their INSTALL
# needs a ``FROM community`` clause.
_COMMUNITY_EXTS = frozenset({"yaml"})
# Extensions that mean "this path is a SQLite database file" (attach, don't scan).
_SQLITE_EXTS = frozenset({".sqlite", ".sqlite3", ".db"})

# Format-override prefixes: force a reader regardless of the locator's extension, for files whose
# name doesn't reveal the format (a ``.dat`` dump, an extensionless API URL). Each maps to a
# canonical extension resolved through the reader tables above, so excel/avro still INSTALL/LOAD
# their extension. Written like the ``delta:``/``iceberg:`` scheme prefixes, e.g. ``csv:<url>``.
_FORMAT_PREFIX: dict[str, str] = {
    "csv": ".csv",
    "tsv": ".tsv",
    "json": ".json",
    "parquet": ".parquet",
    "excel": ".xlsx",
    "avro": ".avro",
    "yaml": ".yaml",
    "yml": ".yaml",
}

# Read options for a JSON snapshot we wrote ourselves (an ``api:`` fetch, the ``openapi:``
# catalog). Both DuckDB defaults being overridden here are *sampling* heuristics that turn into
# hard cast errors later, far from the attach that caused them:
#   * ``sample_size`` caps type inference at the first 20480 rows, so a field whose type first
#     varies past that point breaks the scan mid-query.
#   * ``map_inference_threshold`` silently types a wide object as MAP(VARCHAR, <one value type>).
#     A MAP has ONE value type, so a key holding a string in a record whose siblings hold numbers
#     is unrepresentable, and two snapshots that infer different value types cannot be reconciled
#     at all — this is the source of the notorious
#     ``Could not convert string 'x@y.gov' to INT128`` on a UNION of two API snapshots. A STRUCT
#     reconciles by field name and keeps each field's own type, so we always prefer one.
# Both are cheap to disable on a local file we just wrote (no measurable cost on a 28MB/6k-row
# snapshot) and buy correctness that the sampled defaults only approximate.
_JSON_SNAPSHOT_OPTS = "format='newline_delimited', sample_size=-1, map_inference_threshold=-1"

# Remote-path schemes reachable through a DuckDB filesystem extension, mapped to the extension
# that provides them. httpfs covers http(s)/S3/GCS/R2; azure covers Azure Blob / ADLS.
_REMOTE_EXT: dict[str, str] = {
    "http://": "httpfs",
    "https://": "httpfs",
    "s3://": "httpfs",
    "s3a://": "httpfs",
    "gs://": "httpfs",
    "gcs://": "httpfs",
    "r2://": "httpfs",
    "az://": "azure",
    "azure://": "azure",
    "abfs://": "azure",
    "abfss://": "azure",
}

# Lakehouse table scans: kind -> (duckdb extension, scan function, extra scan args). Written as a
# VIEW like a file. iceberg_scan gets ``allow_moved_paths => true`` so a table whose manifests hold
# paths written for a different location (relative, or an absolute path from generation) still reads
# — DuckDB's own documented default for iceberg_scan; correct paths are unaffected.
_SCAN_KINDS: dict[str, tuple[str, str, str]] = {
    "delta": ("delta", "delta_scan", ""),
    "iceberg": ("iceberg", "iceberg_scan", ", allow_moved_paths => true"),
}

# Map our attach kinds to (duckdb extension, ATTACH TYPE). DuckLake infers its TYPE from the
# ``ducklake:`` locator prefix, so it has no explicit ATTACH TYPE.
_ATTACH_EXT = {"sqlite": "sqlite", "postgres": "postgres", "mysql": "mysql"}

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")
_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,62})=(.+)$", re.DOTALL)


@dataclass
class Source:
    """One registered data source.

    ``setup_sql`` are the statements to run on the DuckDB connection to make the source
    queryable (extension loads + ATTACH/CREATE VIEW). ``info`` carries kind-specific
    provenance — for an ``api`` source, the fetch fingerprint (url, fetched_at, pages,
    row_count, snapshot path) — and is ``None`` for kinds that have none.

    ``connection`` is set only for an ``openapi:`` source, which is not just a catalog but a
    live *connection*: base URL plus credentials, reusable by ``fetch`` for any endpoint of
    that API. It is what makes one API one source instead of one source per endpoint.
    """

    name: str
    kind: SourceKind
    locator: str
    setup_sql: list[str] = field(default_factory=list)
    info: dict | None = None
    connection: "ApiConnection | None" = None


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


def _split_format_prefix(locator: str) -> tuple[str | None, str]:
    """Split a leading format-override prefix (``csv:`` / ``json:`` / ...) off a locator.

    Returns ``(canonical_ext, inner_locator)`` when the locator starts with a known format
    prefix, else ``(None, locator)``. The prefix is matched case-insensitively; the inner
    locator keeps its original case (URLs are case-sensitive). A Windows drive path such as
    ``C:/x.dat`` never matches — a drive is one letter, every prefix is three or more.
    """
    head, sep, rest = locator.partition(":")
    if sep and head.lower() in _FORMAT_PREFIX:
        return _FORMAT_PREFIX[head.lower()], rest
    return None, locator


def _prefix_help(stripped_name: str | None, locator: str) -> str:
    """Explain a consumed ``<name>=`` prefix when the rest of the spec won't classify.

    The common miss is writing the placeholder literally — ``name=nvd api:<url>`` — which parses
    as the name ``name`` and the locator ``nvd api:<url>``. That is worth naming outright, since
    the resulting error otherwise points at the URL, which was never the problem.
    """
    if stripped_name is None:
        return ""
    head, sep, rest = locator.partition(" ")
    if sep and _NAME_RE.match(head) and rest.strip():
        return (
            f"The spec was read as source name {stripped_name!r} plus locator {locator!r} — the "
            f"prefix is the name YOU choose, not the literal word 'name': write "
            f"'{head}={rest.strip()}'. "
        )
    return f"({stripped_name!r} was taken as the source name, from its '=' prefix.) "


def detect_kind(
    locator: str, forced_ext: str | None = None, *, stripped_name: str | None = None
) -> SourceKind:
    """Classify a locator into a :data:`SourceKind` by scheme/extension.

    ``forced_ext`` (the canonical extension from a ``csv:``/``json:``/... format prefix) pins the
    locator to a ``file`` source regardless of its own extension.

    ``stripped_name`` is the ``<name>=`` prefix the caller already removed; it is used only to
    explain the split when detection fails. Without it, writing the prefix literally
    (``name=nvd api:https://…``) reports an unclassifiable locator of ``nvd api:https://…`` and
    never reveals that a name was consumed — which reads as "the API URL is unsupported".
    """
    if forced_ext is not None:
        return "file"
    low = locator.lower()
    if low.startswith(("postgresql://", "postgres://")):
        return "postgres"
    if low.startswith(("mysql://", "mariadb://")):
        return "mysql"
    if low.startswith(("mssql://", "mssql+", "sqlserver://")):
        raise ValueError(
            f"SQL Server sources are not supported: DuckDB cannot attach {locator!r}. "
            "Export the data to a file (Parquet/CSV) and point a --source at that instead."
        )
    if low.startswith("ducklake:"):
        return "ducklake"
    if low.startswith("delta:"):
        return "delta"
    if low.startswith("iceberg:"):
        return "iceberg"
    if low.startswith("api:"):
        return "api"
    if low.startswith("openapi:"):
        return "openapi"
    if low.startswith("sqlite://"):
        return "sqlite"
    ext = _path_ext(locator)
    if ext in _FILE_READERS or ext in _EXT_FILE_READERS:
        return "file"
    if ext in _SQLITE_EXTS:
        return "sqlite"
    raise ValueError(
        f"Could not determine the source type of {locator!r}. "
        + _prefix_help(stripped_name, locator)
        + "If it is a data file with an "
        "unrecognized or absent extension (a .dat dump, an extensionless API URL), force the "
        "reader with a format prefix — csv:/tsv:/json:/parquet:/excel:/avro:/yaml: — "
        f"e.g. csv:{locator}. "
        "Recognized extensions: "
        f"{', '.join(sorted(set(_FILE_READERS) | set(_EXT_FILE_READERS) | _SQLITE_EXTS))} "
        "(local or via https://, s3://, gs://, az:// URL); or a delta:<path> / iceberg:<path> / "
        "ducklake: locator; or a sqlite:// / postgresql:// / mysql:// DSN."
    )


def build_source(spec: str, *, snapshot_dir: str | None = None) -> Source:
    """Parse a single spec into a :class:`Source` (no DuckDB connection touched yet).

    ``snapshot_dir`` is where an ``api:`` source writes its NDJSON snapshot (the session's
    workspace snapshot dir); building an ``api:`` source performs the fetch here — network
    I/O, deliberately *before* any connection lock is taken. Other kinds ignore it.
    """
    explicit, locator = parse_spec(spec)
    forced_ext, locator = _split_format_prefix(locator)
    kind = detect_kind(locator, forced_ext=forced_ext, stripped_name=explicit)
    name = explicit or _derive_name(locator, kind)
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid source name {name!r}. Use a SQL identifier (letters, digits, "
            "underscores; starting with a letter or underscore; max 63 chars)."
        )

    if kind == "file":
        return _build_file_source(name, locator, forced_ext=forced_ext)
    if kind in _SCAN_KINDS:
        return _build_scan_source(name, kind, locator)
    if kind == "ducklake":
        return _build_ducklake_source(name, locator)
    if kind == "api":
        return _build_api_source(name, locator, snapshot_dir)
    if kind == "openapi":
        return _build_openapi_source(name, locator, snapshot_dir)
    return _build_attach_source(name, kind, locator)


def teardown_sql(src: Source) -> list[str]:
    """Statements that undo a source's :attr:`Source.setup_sql` — the inverse of attaching.

    A view-backed source (``file`` / ``delta`` / ``iceberg`` / ``api``) drops its ``main`` view; an
    attached database (SQLite/Postgres/MySQL/DuckLake) is ``DETACH``ed. Used by
    ``DuckSession.remove_source``. An ``api`` source's snapshot file is left on disk — it lives
    under the workspace dir, so workspace cleanup reclaims it.
    """
    if src.kind in ("file", "api", "openapi") or src.kind in _SCAN_KINDS:
        return [f'DROP VIEW IF EXISTS main."{src.name}"']
    if src.kind in _ATTACH_EXT or src.kind == "ducklake":
        return [f'DETACH "{src.name}"']
    return []


def attach_all(
    con: "duckdb.DuckDBPyConnection",
    specs: Iterable[str],
    *,
    snapshot_dir: str | None = None,
) -> list[Source]:
    """Build every source and run its ``setup_sql`` on *con*; return the registered sources.

    Raises on duplicate source names so two sources never collide on one catalog/view name.
    ``snapshot_dir`` is forwarded to :func:`build_source` for ``api:`` sources.
    """
    sources: list[Source] = []
    seen: set[str] = set()
    for spec in specs:
        src = build_source(spec, snapshot_dir=snapshot_dir)
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
def _build_file_source(name: str, locator: str, forced_ext: str | None = None) -> Source:
    """A file source becomes a VIEW in `main` over the matching read_* scan.

    The path may be local or a remote URL (``https://``/``s3://``/``gs://``/``az://``); a remote
    path loads the filesystem extension (``httpfs`` / ``azure``) first and is passed through
    verbatim (not run through ``os.path.abspath``, which would mangle the scheme). ``forced_ext``
    (from a ``csv:``/``json:``/... format prefix) overrides the extension-based reader choice, so a
    file with an odd or absent extension still reads.
    """
    ext = forced_ext if forced_ext is not None else _path_ext(locator)
    path = _duck_path(locator)
    setup: list[str] = list(_remote_setup(locator))
    if ext in _EXT_FILE_READERS:
        ext_name, reader = _EXT_FILE_READERS[ext]
        setup += _load_ext(ext_name)
        scan = f"{reader}('{path}')"
    else:
        scan = f"{_FILE_READERS[ext]}('{path}')"
    setup.append(f'CREATE OR REPLACE VIEW main."{name}" AS SELECT * FROM {scan}')
    return Source(name=name, kind="file", locator=locator, setup_sql=setup)


def _build_scan_source(name: str, kind: SourceKind, locator: str) -> Source:
    """A Delta/Iceberg table becomes a VIEW in `main` over ``delta_scan`` / ``iceberg_scan``.

    The spec is ``delta:<path>`` / ``iceberg:<path>`` where ``<path>`` is a local directory or a
    remote URL; a remote path additionally loads its filesystem extension.
    """
    ext_name, scan_fn, extra_args = _SCAN_KINDS[kind]
    inner = _strip_scheme(locator, kind)
    path = _duck_path(inner)
    setup: list[str] = list(_remote_setup(inner))
    setup += _load_ext(ext_name)
    setup.append(
        f'CREATE OR REPLACE VIEW main."{name}" AS SELECT * FROM {scan_fn}(\'{path}\'{extra_args})'
    )
    return Source(name=name, kind=kind, locator=locator, setup_sql=setup)


def _build_api_source(name: str, locator: str, snapshot_dir: str | None) -> Source:
    """An ``api:`` source: fetch the endpoint into an NDJSON snapshot, view over the snapshot.

    The fetch (pagination, retries, auth — see ``apifetch``) happens here, at build time, so
    the network I/O is done before the session lock is ever taken. The view reads the local
    snapshot file — queries never re-fetch the API. ``Source.info`` carries the fetch
    fingerprint (url, fetched_at, pages, row_count, snapshot path).
    """
    from . import apifetch

    if snapshot_dir is None:
        raise ValueError(
            "api: sources need a workspace to store their snapshot — open the session with a "
            "workspace (DuckSession.open) rather than calling build_source directly."
        )
    spec = apifetch.parse_api_spec(_strip_scheme(locator, "api"))
    os.makedirs(snapshot_dir, exist_ok=True)
    dest = os.path.join(snapshot_dir, f"{name}.ndjson")
    info = apifetch.fetch_snapshot(spec, dest)
    setup = [
        f'CREATE OR REPLACE VIEW main."{name}" AS SELECT * FROM {_snapshot_scan(dest, spec.json)}'
    ]
    if spec.json:
        info = {**info, "typing": "json"}
    return Source(name=name, kind="api", locator=locator, setup_sql=setup, info=info)


def _snapshot_scan(dest: str, as_json: bool = False) -> str:
    """The scan expression for an NDJSON snapshot — inferred columns, or one raw JSON column.

    ``as_json`` (the ``json=true`` spec option) is the escape hatch for an API whose records are
    genuinely polymorphic — a field that is an object in some records and an array in others has
    no single inferred type, and no inference setting can conjure one. The view then has a single
    ``json`` column to pick apart with ``json_extract`` / ``->>``, which is exactly what a caller
    would otherwise hand-roll with ``to_json(...)::VARCHAR`` after the scan has already failed.
    """
    path = _duck_path(dest)
    if as_json:
        return f"read_ndjson_objects('{path}')"
    return f"read_json_auto('{path}', {_JSON_SNAPSHOT_OPTS})"


def _build_openapi_source(name: str, locator: str, snapshot_dir: str | None) -> Source:
    """An ``openapi:`` source: an API *connection* plus its queryable endpoint catalog.

    Two things in one attach, because the spec describes both. The catalog is one row per
    (path, method) with params, auth shape, pagination/records hints, ``response_fields``, and
    a paste-ready ``suggested_spec`` — guidance-as-data the agent queries with SQL. The
    connection is the spec's ``servers[0].url`` plus whatever credentials the locator's options
    supply, and it is what ``fetch`` calls to reach ANY endpoint of the API without attaching
    another source.

    Grammar: ``openapi:<url-or-path> [auth_env=ENV] [header=N:ENV] [param=n:ENV]
    [default_param=n:value] [records=…] [paginate=…] …`` — the trailing options are the
    connection's, and the fetch options among them become defaults every request inherits.
    """
    from . import apifetch, openapi as openapi_mod

    if snapshot_dir is None:
        raise ValueError(
            "openapi: sources need a workspace to store their catalog — open the session with "
            "a workspace (DuckSession.open) rather than calling build_source directly."
        )
    inner, option_tokens = _split_locator_options(_strip_scheme(locator, "openapi"))
    spec = openapi_mod.load_spec(inner)
    rows = openapi_mod.endpoint_rows(spec, inner)
    if not rows:
        raise ValueError(f"OpenAPI spec {inner!r} declares no operations under 'paths'.")
    os.makedirs(snapshot_dir, exist_ok=True)
    dest = os.path.join(snapshot_dir, f"{name}.ndjson")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, dest)
    setup = [f'CREATE OR REPLACE VIEW main."{name}" AS SELECT * FROM {_snapshot_scan(dest)}']
    base_url = openapi_mod.base_url_of(spec, inner)
    connection = apifetch.parse_connection(base_url, option_tokens)
    if not connection.base_url.lower().startswith(("http://", "https://")):
        # A local spec whose servers[0].url is relative (or absent) leaves nothing to build
        # requests from. Say so here, where the fix is one token, rather than letting every
        # fetch die inside urlopen with "unknown url type".
        servers = spec.get("servers") or [{}]
        declared = servers[0].get("url") if isinstance(servers[0], dict) else None
        raise ValueError(
            f"OpenAPI spec {inner!r} does not give an absolute server URL "
            f"(servers[0].url is {declared or '<missing>'!r}), so its endpoints have no host "
            f"to call. Supply one on the source spec: openapi:{inner} "
            "base_url=https://api.example.com"
        )
    info = openapi_mod.catalog_info(inner, rows)
    info["base_url"] = connection.base_url
    info["auth"] = connection.auth_env or (
        connection.extra_headers[0][0] if connection.extra_headers else None
    )
    return Source(
        name=name,
        kind="openapi",
        locator=locator,
        setup_sql=setup,
        info=info,
        connection=connection,
    )


_QUOTED_HEAD_RE = re.compile(r'^\s*(["\'])(.+?)\1\s*(.*)$', re.DOTALL)


def _split_locator_options(body: str) -> tuple[str, list[str]]:
    """Split ``<locator> [key=value ...]`` into its locator and shell-tokenized options.

    The locator is taken off *before* ``shlex`` sees the string: an ``openapi:`` locator is
    routinely a Windows path, and ``shlex`` in POSIX mode would eat every backslash in
    ``C:\\specs\\api.json``. Quote the locator if it contains spaces.
    """
    match = _QUOTED_HEAD_RE.match(body)
    if match:
        locator, rest = match.group(2), match.group(3)
    else:
        parts = body.strip().split(None, 1)
        if not parts:
            raise ValueError("openapi: source needs a spec URL or path.")
        locator, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    return locator, tokens


def _build_ducklake_source(name: str, locator: str) -> Source:
    """A DuckLake catalog is ``ATTACH``ed read-only; its TYPE is inferred from the ``ducklake:``
    locator prefix, which is kept intact and passed straight to ATTACH."""
    target = locator.replace("'", "''")
    setup = _load_ext("ducklake")
    setup.append(f"ATTACH '{target}' AS \"{name}\" (READ_ONLY)")
    return Source(name=name, kind="ducklake", locator=locator, setup_sql=setup)


def _build_attach_source(name: str, kind: SourceKind, locator: str) -> Source:
    """A database source is ATTACHed read-only as its own catalog."""
    ext_name = _ATTACH_EXT[kind]
    target = _attach_target(kind, locator)
    setup = _load_ext(ext_name)
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
            # Strip only the single URI-separator slash, so the 4-slash absolute form
            # (sqlite:////abs/path.db -> /abs/path.db) survives; 3-slash relative stays relative.
            path = rest[1:] if rest.startswith("/") else rest
        else:
            path = locator
        return _duck_path(path)

    url = urlsplit(locator)
    db_key = "database" if kind == "mysql" else "dbname"
    # Percent-decoded like the other fields — urlsplit leaves the path encoded.
    database = unquote(url.path[1:] if url.path.startswith("/") else url.path)
    fields: list[str] = []
    if url.hostname:
        fields.append(f"host={_conn_value(url.hostname)}")
    if url.port:
        fields.append(f"port={url.port}")
    if url.username:
        fields.append(f"user={_conn_value(unquote(url.username))}")
    if url.password:
        fields.append(f"password={_conn_value(unquote(url.password))}")
    if database:
        fields.append(f"{db_key}={_conn_value(database)}")
    for key, val in parse_qsl(url.query):
        fields.append(f"{key}={_conn_value(val)}")
    return " ".join(fields).replace("'", "''")


def _conn_value(val: str) -> str:
    """Quote one value of a ``key=value`` connection string (libpq rules).

    A space in a value would otherwise end the field and turn the rest into bogus keys
    (``password=hunter 2`` -> a ``2`` key), so anything holding whitespace, a quote or a
    backslash is single-quoted with those escaped. Plain values pass through unwrapped.
    """
    if val and not any(ch.isspace() or ch in "'\\" for ch in val):
        return val
    escaped = val.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _derive_name(locator: str, kind: SourceKind) -> str:
    """Derive a SQL-identifier source name from a locator (filename stem or DB name)."""
    # Strip our own scheme prefixes so the name comes from the underlying path, not "delta"/etc.
    if kind in _SCAN_KINDS:
        locator = _strip_scheme(locator, kind)
    elif kind == "ducklake":
        locator = _strip_scheme(locator, "ducklake")
    elif kind == "api":
        # Name from the URL's last path segment; drop the whitespace-separated options first.
        locator = _strip_scheme(locator, "api").split()[0]
    elif kind == "openapi":
        # Name from the spec locator; drop the whitespace-separated connection options first.
        locator = _strip_scheme(locator, "openapi").split()[0]
    if "://" in locator:
        try:
            url = urlsplit(locator)
            database = url.path[1:] if url.path.startswith("/") else url.path
            base = database or url.hostname or kind
        except Exception:
            base = kind
    else:
        base = locator
    # A remote/glob path may carry a query string or wildcard — keep only the last path segment.
    base = base.split("?")[0].rstrip("/")
    stem = os.path.splitext(os.path.basename(base or kind))[0]
    name = _SANITIZE_RE.sub("_", stem).strip("_").lower() or kind
    if not re.match(r"[A-Za-z_]", name[0]):
        name = f"s_{name}"
    return name[:63]


def _path_ext(locator: str) -> str:
    """The lower-cased file extension of *locator*, ignoring any URL query string/fragment.

    Uses ``urlsplit`` only for genuine ``scheme://`` URLs so a Windows drive path (``C:/x.csv``)
    isn't misread as a scheme.
    """
    path = locator
    if "://" in locator:
        path = urlsplit(locator).path
    return os.path.splitext(path.lower())[1]


def _is_remote(path: str) -> bool:
    """True if *path* is a remote URL served by a DuckDB filesystem extension."""
    return path.lower().startswith(tuple(_REMOTE_EXT))


def _remote_setup(path: str) -> list[str]:
    """INSTALL/LOAD statements for the filesystem extension a remote *path* needs (empty if local).

    For ``s3://`` paths a default ``s3_region`` is set so a *public* bucket reads with zero config
    (DuckDB otherwise leaves the region empty and the request 404s). ``us-east-1`` covers most AWS
    open-data buckets; this is only the fallback for anonymous access — a bucket in another region,
    or a private one, needs the user's own DuckDB S3 secret, whose REGION takes precedence.

    ``SET s3_region`` is connection-global, so the fallback is applied *non-destructively*: the
    statement re-sets the region to its own current value when one is already set, and only falls
    back to ``us-east-1`` when it is unset (DuckDB reports NULL) or empty. Registering an S3 source
    therefore can't clobber a region the caller configured by hand for a non-us-east-1 bucket. The
    per-source alternative, if a session ever needs two regions at once, is a ``CREATE SECRET`` with
    a bucket ``SCOPE`` rather than a global ``SET``.
    """
    low = path.lower()
    for scheme, ext_name in _REMOTE_EXT.items():
        if low.startswith(scheme):
            setup = _load_ext(ext_name)
            if scheme in ("s3://", "s3a://"):
                # Global but non-destructive — keeps any region already set. See the note above.
                setup.append(
                    "SET s3_region = coalesce(nullif(current_setting('s3_region'), ''), "
                    "'us-east-1')"
                )
            return setup
    return []


def _load_ext(ext_name: str) -> list[str]:
    """The ``INSTALL``/``LOAD`` pair for a DuckDB extension.

    Community-repository extensions (:data:`_COMMUNITY_EXTS`) need ``INSTALL <ext> FROM community``
    — a bare ``INSTALL`` only searches the core repository and fails with "not found".
    """
    install = f"INSTALL {ext_name}"
    if ext_name in _COMMUNITY_EXTS:
        install += " FROM community"
    return [install, f"LOAD {ext_name}"]


def _strip_scheme(locator: str, kind: str) -> str:
    """Strip a leading ``<kind>:`` scheme prefix (``delta:`` / ``iceberg:`` / ``ducklake:``)."""
    prefix = f"{kind}:"
    return locator[len(prefix):] if locator.lower().startswith(prefix) else locator


def _duck_path(path: str) -> str:
    """A path as a DuckDB string literal: remote URLs pass through verbatim; local paths are made
    absolute and forward-slashed. Single quotes are escaped in both cases."""
    if _is_remote(path):
        return path.replace("'", "''")
    return os.path.abspath(path).replace("\\", "/").replace("'", "''")
