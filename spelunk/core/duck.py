"""The unified DuckDB session — one connection that is both the query engine and the
session workspace.

Every data source (file scan or attached database) lives in this one DuckDB connection
alongside the workspace flows, so a single ``query`` can join a Parquet file to a Postgres
table to a previously-built result, all in DuckDB SQL. This collapses the old two-engine,
two-lane design (SQLAlchemy ``run_query`` vs the DuckDB flow workspace) into one engine.

Layout inside the connection:
  * **sources** — attached databases are their own catalogs (`"src"."table"`); file sources
    are VIEWs in the ``main`` schema of the workspace catalog (bare name).
  * **flows** — each flow is a schema in the workspace catalog; results are tables in it.
    A query references flow results bare (search_path includes the flow) and sources either
    bare (files) or catalog-qualified (attached DBs).

DuckDB is out-of-core: sources are read on demand with pushdown, and buffering operators
spill to ``temp_directory`` — so a source larger than RAM is the normal case. The workspace
is always disk-backed; ``memory_limit`` / ``temp_directory`` are set at open.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import duckdb

from . import guard, sources as sources_mod
from .types import ColumnInfo, TableDescription, TableInfo

if TYPE_CHECKING:
    from .sources import Source

# Valid flow / result names: SQL-identifier-safe, so they interpolate into CREATE TABLE and
# quoted references without injection risk (this is why callers address results by NAME).
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Internal schema (in the workspace catalog) holding lineage metadata — never a flow, never
# queried by an agent. Kept out of catalog()/drop() by living in _RESERVED_SCHEMAS.
_META_SCHEMA = "_spelunk_meta"

# Schemas in the workspace catalog that are never flows and must never be dropped.
_RESERVED_SCHEMAS = frozenset(
    {"main", "information_schema", "pg_catalog", "system", "temp", _META_SCHEMA}
)

# DuckDB base type names that mark a column as numeric (for profile stats). Matched against the
# type name with any parametrisation stripped (e.g. DECIMAL(18,3) -> DECIMAL) — exact, not
# substring, so INTERVAL is not mistaken for an INT (STDDEV/percentiles fail on interval values).
_NUMERIC_TYPES = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "DECIMAL", "NUMERIC", "REAL", "FLOAT", "DOUBLE",
})


def _is_numeric_type(type_name: str) -> bool:
    """True if a DuckDB column type is numeric — exact base-type match (drops any `(...)`)."""
    return type_name.upper().split("(", 1)[0].strip() in _NUMERIC_TYPES

_SAMPLE_ROWS = 5
# A materialized result larger than this, produced by an unfiltered SELECT * over a source,
# triggers a nudge: you probably wanted a slice, and DuckDB would have pushed the filter down.
_LARGE_MATERIALIZE = 100_000


def _warm_native_imports() -> None:
    """Force DuckDB's lazy ``numpy``/``pandas`` import to happen on the *main* thread.

    DuckDB imports numpy/pandas lazily the first time a result is fetched (its Python
    result-conversion path). When that first import lands on a FastMCP worker thread — the
    MCP server runs sync tools off the event loop via ``anyio.to_thread`` — loading numpy's
    compiled ``multiarray`` extension deadlocks under the running asyncio proactor loop on
    Windows, so the tool call never returns. Importing them here, on the main thread at
    ``open()`` time, means the worker thread only ever sees already-loaded modules.
    """
    import numpy  # noqa: F401
    import pandas  # noqa: F401


def _validate_name(name: str, kind: str = "result name") -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"Invalid {kind} {name!r}. Use a SQL identifier: letters, digits, and "
            "underscores, starting with a letter or underscore (max 63 chars)."
        )
    return name


def _quote_qualified(name: str) -> str:
    """Quote each dot-separated part of a (possibly catalog/schema-qualified) identifier."""
    return ".".join('"' + p.replace('"', '""') + '"' for p in name.split("."))


def _to_python(v: Any) -> Any:
    """Convert a DuckDB value to a JSON-serialisable Python type."""
    if v is None:
        return None
    import math
    from decimal import Decimal

    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) else v
    if isinstance(v, Decimal):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, (int, str)):
        return v
    try:
        return v.item()
    except AttributeError:
        return str(v)


def _process_workspace_id() -> str:
    """A per-process, collision-free id for a workspace subdirectory.

    PID makes it human-traceable to a running server; the random suffix keeps it unique even if
    a PID is reused or two sessions open in one process (e.g. tests)."""
    return f"{os.getpid()}-{uuid4().hex[:8]}"


# Per-process workspaces accumulate under the session root; on startup keep the N most recent and
# reclaim older ones. Dirs younger than the grace window are never touched — a sibling may be
# mid-startup and not yet holding its lock.
_DEFAULT_KEEP_WORKSPACES = 3
_SWEEP_GRACE_SECONDS = 60


def _workspace_is_free(dir_path: str) -> bool:
    """True if ``dir_path``'s workspace.duckdb is NOT held by a live server.

    Probes by opening read-write: a live owner holds the single-writer lock so the connect
    raises; a crashed/exited owner leaves an unlocked file (its WAL is just replayed) so it opens.
    Read-write — not read_only — is deliberate: a read_only open of a DB with a pending WAL errors,
    which would make us mistake a crashed orphan for a live owner and never reclaim it."""
    try:
        con = duckdb.connect(os.path.join(dir_path, "workspace.duckdb"))
    except Exception:
        return False
    con.close()
    return True


def _reclaim_old_workspaces(parent: str, keep: int, *, exclude: str) -> list[str]:
    """Delete all but the ``keep`` most-recent per-process workspace subdirs under ``parent``.

    Best-effort GC: only removes dirs that have no live owner, are older than the grace window,
    and are not ``exclude`` (this process's own dir). Never raises — losing a race to a concurrent
    server just leaves a dir for the next sweep. Returns the dirs actually removed."""
    try:
        dirs = [
            os.path.join(parent, d)
            for d in os.listdir(parent)
            if os.path.isfile(os.path.join(parent, d, "workspace.duckdb"))
        ]
    except OSError:
        return []
    dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)  # newest first
    now = time.time()
    removed: list[str] = []
    for path in dirs[keep:]:  # keep the N newest (this process's fresh dir is among them)
        if os.path.abspath(path) == os.path.abspath(exclude):
            continue
        try:
            if now - os.path.getmtime(path) < _SWEEP_GRACE_SECONDS:
                continue
        except OSError:
            continue
        if not _workspace_is_free(path):
            continue
        try:
            shutil.rmtree(path)
            removed.append(path)
        except OSError:
            pass
    return removed


class DuckSession:
    """A single DuckDB connection wrapping sources + the flow workspace.

    Construct with :meth:`open`. Thread-safety: a DuckDBPyConnection isn't safe for concurrent
    use, so every connection touch holds ``_lock``. Remote pulls (``import_remote``) do their
    slow SQLAlchemy fetch outside the lock.
    """

    def __init__(
        self,
        con: "duckdb.DuckDBPyConnection",
        sources: list["Source"],
        *,
        catalog: str,
        workspace_dir: str,
        tmpdir: "tempfile.TemporaryDirectory | None" = None,
    ) -> None:
        self._con = con
        self.sources = sources
        self._catalog = catalog
        self.workspace_dir = workspace_dir
        self._tmpdir = tmpdir
        self._lock = threading.Lock()
        self.default_flow = "default"
        self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.default_flow}"')
        self._ensure_meta()

    # ------------------------------------------------------------------ lineage store - #
    def _ensure_meta(self) -> None:
        """Create the internal lineage store (idempotent). One row per live result.

        A result is keyed by (flow, name); ``CREATE OR REPLACE`` of a result overwrites its
        row, so the store always reflects the *current* definition. ``deps`` and ``sources``
        are JSON arrays; ``seq`` is a monotonic creation counter used as a stable tie-break
        when ordering independent nodes for replay.
        """
        self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{_META_SCHEMA}"')
        self._con.execute(
            f'CREATE TABLE IF NOT EXISTS "{_META_SCHEMA}".lineage ('
            "flow VARCHAR NOT NULL, name VARCHAR NOT NULL, sql VARCHAR NOT NULL, "
            "kind VARCHAR NOT NULL, deps VARCHAR NOT NULL, sources VARCHAR NOT NULL, "
            "created_at VARCHAR NOT NULL, seq BIGINT NOT NULL, "
            "PRIMARY KEY (flow, name))"
        )

    # ------------------------------------------------------------------ open / close --- #
    @classmethod
    def open(
        cls,
        specs: list[str] | None = None,
        *,
        session_dir: str | None = None,
        per_process: bool = True,
        keep_workspaces: int = _DEFAULT_KEEP_WORKSPACES,
        memory_limit: str | None = None,
        temp_dir: str | None = None,
        max_temp_size: str | None = None,
    ) -> "DuckSession":
        """Open a disk-backed workspace, configure limits, and attach every source.

        With ``session_dir`` the workspace (and named results) persist in
        ``<session_dir>/workspace.duckdb``; without it a private temp directory is used (still
        disk-backed, so large results page to disk — just not durable across restarts).

        ``per_process`` (the DEFAULT) treats ``session_dir`` as a PARENT and gives each process
        its OWN durable workspace at ``<session_dir>/<pid>-<rand>/workspace.duckdb``. This lets
        many concurrent servers each have an isolated, durable workspace under one root — so
        their results can never collide — instead of all contending for one single-writer file.
        Pass ``per_process=False`` for one shared ``<session_dir>/workspace.duckdb`` that the
        same caller can reopen across restarts (the old single-writer behaviour).

        In ``per_process`` mode, opening also reclaims stale workspaces: the ``keep_workspaces``
        most recent subdirs survive (including the one just created) and older ones with no live
        owner are deleted. ``keep_workspaces <= 0`` disables the sweep (keep everything).

        A durable workspace is a single-writer DuckDB file (exclusive lock). If it's already
        held by another server instance — e.g. a second editor window on the same project — we
        DON'T crash this session: we fall back to a private ephemeral workspace and warn on
        stderr. The session stays fully functional; its results just don't persist or share
        with the instance that holds the lock. (With ``per_process`` each process has its own
        subdir, so this contention path is normally never hit.)
        """
        _warm_native_imports()
        tmpdir: tempfile.TemporaryDirectory | None = None
        pp_parent: str | None = None  # the session root to sweep, only when per_process succeeds
        if session_dir is not None:
            base = os.path.abspath(session_dir)
            if per_process:
                pp_parent = base
                base = os.path.join(base, _process_workspace_id())
            os.makedirs(base, exist_ok=True)
            try:
                con = duckdb.connect(os.path.join(base, "workspace.duckdb"))
            except duckdb.IOException as exc:
                pp_parent = None  # fell back to ephemeral — no per-process tree to sweep
                tmpdir = tempfile.TemporaryDirectory(prefix="spelunk_ws_")
                base = tmpdir.name
                con = duckdb.connect(os.path.join(base, "workspace.duckdb"))
                print(
                    f"[spelunk] durable workspace in {session_dir!r} is locked by another "
                    "server instance; using an ephemeral workspace for this session (results "
                    f"will not persist or be shared). Detail: {exc}",
                    file=sys.stderr,
                )
        else:
            tmpdir = tempfile.TemporaryDirectory(prefix="spelunk_ws_")
            base = tmpdir.name
            con = duckdb.connect(os.path.join(base, "workspace.duckdb"))

        spill = temp_dir or os.path.join(base, "spill")
        os.makedirs(spill, exist_ok=True)
        con.execute(f"SET temp_directory = '{spill.replace(chr(92), '/').replace(chr(39), chr(39) * 2)}'")
        if memory_limit:
            con.execute(f"SET memory_limit = '{memory_limit}'")
        if max_temp_size:
            con.execute(f"SET max_temp_directory_size = '{max_temp_size}'")

        catalog = con.execute("SELECT current_database()").fetchone()[0]

        attached: list[Source] = []
        if specs:
            attached = sources_mod.attach_all(con, specs)

        if pp_parent is not None and keep_workspaces > 0:
            _reclaim_old_workspaces(pp_parent, keep_workspaces, exclude=base)

        return cls(con, attached, catalog=catalog, workspace_dir=base, tmpdir=tmpdir)

    def close(self) -> None:
        with self._lock:
            self._con.close()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()

    @property
    def fallback_sources(self) -> list["Source"]:
        """Sources reachable only via SQLAlchemy (SQL Server / exotic) — for import_remote."""
        return [s for s in self.sources if s.kind == "fallback"]

    # ------------------------------------------------------------------ sources ------- #
    def add_source(self, spec: str) -> dict:
        """Attach a new data source at runtime (the same ``spec`` grammar as ``--source``).

        Builds the source (a ``fallback`` spec opens its SQLAlchemy engine here, off the lock),
        rejects a name that collides with an existing source or the workspace catalog, then runs
        its setup SQL and registers it. The source becomes queryable in *every* flow of this
        session — sources are connection-global, not flow-scoped. Returns the source's name,
        kind, and the objects it made queryable.
        """
        src = sources_mod.build_source(spec)  # off-lock: fallback specs connect here
        with self._lock:
            # Check-and-register under one lock: two concurrent add_source calls with the same
            # name must not both pass the uniqueness test (worker threads run tools concurrently).
            existing = {s.name for s in self.sources}
            if src.name in existing or src.name == self._catalog:
                raise ValueError(
                    f"Source name {src.name!r} is already in use "
                    f"(sources: {sorted(existing) or ['(none)']}; workspace catalog: {self._catalog!r}). "
                    "Prefix the spec with a different name, e.g. mydata=<spec>."
                )
            for stmt in src.setup_sql:  # on failure, nothing is appended — self.sources unchanged
                self._con.execute(stmt)
            self.sources.append(src)
            objects = [obj.model_dump() for obj in self._objects_for_source(src)]
        return {"name": src.name, "kind": src.kind, "objects": objects}

    def remove_source(self, name: str) -> dict:
        """Detach a source added at runtime or configured at startup; idempotent on the SQL.

        Runs the source's teardown (``DETACH`` / ``DROP VIEW``), disposes a fallback engine,
        and forgets it. Affects this session's connection only — under the process-per-agent
        model that's the agent's own isolated workspace. Raises if no such source exists.
        """
        with self._lock:
            # Look up and remove under one lock so a concurrent add/remove can't leave a stale
            # entry or double-remove (worker threads run tools concurrently).
            src = next((s for s in self.sources if s.name == name), None)
            if src is None:
                known = sorted(s.name for s in self.sources)
                raise ValueError(f"No source named {name!r}. Configured sources: {known or ['(none)']}.")
            for stmt in sources_mod.teardown_sql(src):
                self._con.execute(stmt)
            self.sources.remove(src)
        if src.engine is not None:
            src.engine.dispose()
        return {"name": src.name, "kind": src.kind, "removed": True}

    # ------------------------------------------------------------------ internals ----- #
    def _resolve_flow(self, flow: str | None) -> str:
        """Default, validate, and reject reserved names — for every flow-scoped write path."""
        flow = flow or self.default_flow
        _validate_name(flow, "flow name")
        if flow in _RESERVED_SCHEMAS:
            raise ValueError(f"{flow!r} is a reserved schema name; choose another flow name.")
        return flow

    def _set_search_path(self, flow: str) -> None:
        """Resolve bare names against the flow first, then `main` (file-source views)."""
        self._con.execute(f"SET search_path = '{flow},main'")

    def _existing_results(self) -> set[tuple[str, str]]:
        """All (flow, name) result tables in the workspace catalog (caller holds ``_lock``).

        Used to classify a parsed table reference as a *result dependency* vs an external
        source leaf: a ref is a dependency iff it names a table that actually exists here.
        """
        rows = self._con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = ? AND table_schema NOT IN "
            f"({', '.join('?' * len(_RESERVED_SCHEMAS))})",
            [self._catalog, *sorted(_RESERVED_SCHEMAS)],
        ).fetchall()
        return {(s, t) for s, t in rows}

    def _classify_refs(
        self,
        sql: str,
        flow: str,
        results: set[tuple[str, str]],
        self_ref: tuple[str, str],
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Split a query's table references into result *deps* and external *source* leaves.

        Parses *sql* (best-effort; an unparseable query yields empty lists) and, for each table
        reference, decides against ``results`` — the live (flow, name) set — whether it is another
        result (a dependency, resolved the same way DuckDB's search_path does: bare → current
        flow, ``a.b`` → flow ``a``, ``cat.a.b`` → flow ``a`` only when ``cat`` is the workspace
        catalog) or an external input (file view / attached-DB table), recorded by its textual
        form. ``self_ref`` is the (flow, name) being recorded — a reference to it is dropped so a
        result never depends on itself (e.g. ``CREATE OR REPLACE t AS SELECT ... FROM t``).
        """
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import SqlglotError

        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except SqlglotError:
            return [], []

        deps: list[dict[str, str]] = []
        sources: list[str] = []
        seen_dep: set[tuple[str, str]] = set()
        seen_src: set[str] = set()
        for tbl in tree.find_all(exp.Table):
            tname = tbl.name
            db = tbl.db  # schema part ('' if absent)
            catalog = tbl.catalog  # catalog part ('' if absent)
            if not tname:
                continue
            if catalog and catalog != self._catalog:
                cand = None  # a foreign catalog (attached DB) — never a workspace result
            elif db:
                cand = (db, tname)
            else:
                cand = (flow, tname)
            if cand is not None and cand in results:
                if cand != self_ref and cand not in seen_dep:
                    seen_dep.add(cand)
                    deps.append({"flow": cand[0], "name": cand[1]})
            else:
                ref = ".".join(p for p in (catalog, db, tname) if p)
                if ref not in seen_src:
                    seen_src.add(ref)
                    sources.append(ref)
        return deps, sources

    def _record_lineage(self, flow: str, name: str, sql: str, kind: str) -> None:
        """Upsert the lineage row for a just-materialized result (caller holds ``_lock``).

        Called from ``query`` / ``import_remote`` right after the CREATE, so the result set is
        already current. Dependencies are computed against every *other* live result.
        """
        results = self._existing_results()
        deps, sources = self._classify_refs(sql, flow, results, (flow, name))
        seq = self._con.execute(
            f'SELECT COALESCE(MAX(seq), 0) + 1 FROM "{_META_SCHEMA}".lineage'
        ).fetchone()[0]
        self._con.execute(
            f'DELETE FROM "{_META_SCHEMA}".lineage WHERE flow = ? AND name = ?', [flow, name]
        )
        self._con.execute(
            f'INSERT INTO "{_META_SCHEMA}".lineage '
            "(flow, name, sql, kind, deps, sources, created_at, seq) VALUES (?,?,?,?,?,?,?,?)",
            [
                flow,
                name,
                sql,
                kind,
                json.dumps(deps),
                json.dumps(sources),
                datetime.now(timezone.utc).isoformat(),
                int(seq),
            ],
        )

    def _delete_lineage(self, flow: str, name: str | None) -> None:
        """Forget lineage for a dropped result (``name`` given) or a whole flow (caller holds lock)."""
        if name is None:
            self._con.execute(f'DELETE FROM "{_META_SCHEMA}".lineage WHERE flow = ?', [flow])
        else:
            self._con.execute(
                f'DELETE FROM "{_META_SCHEMA}".lineage WHERE flow = ? AND name = ?', [flow, name]
            )

    def _result_names(self, flow: str) -> list[str]:
        rows = self._con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog = ? AND table_schema = ? ORDER BY table_name",
            [self._catalog, flow],
        ).fetchall()
        return [r[0] for r in rows]

    def _columns_of(self, flow: str, name: str) -> list[dict[str, str]]:
        rows = self._con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [self._catalog, flow, name],
        ).fetchall()
        return [{"name": c, "type": t} for c, t in rows]

    def _head_sample(self, flow: str, name: str, n: int = _SAMPLE_ROWS) -> list[list]:
        cur = self._con.execute(f'SELECT * FROM "{flow}"."{name}" LIMIT {n}')
        return [[_to_python(v) for v in row] for row in cur.fetchall()]

    # ------------------------------------------------------------------ query --------- #
    def query(self, sql: str, name: str, flow: str | None = None) -> dict:
        """Run a read-only SELECT over sources + flow results, materialize it as a table.

        ``name`` is required and the result is stored as ``"<flow>"."<name>"`` (replacing any
        prior result of that name). Returns the result's columns, true row_count, a head
        sample, and any nudges.
        """
        flow = self._resolve_flow(flow)
        _validate_name(name)
        guard.assert_read_only(sql, "duckdb")
        with self._lock:
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._set_search_path(flow)
            t0 = time.perf_counter()
            with self._friendly_catalog_errors(flow):
                self._con.execute(f'CREATE OR REPLACE TABLE "{flow}"."{name}" AS {sql}')
            elapsed = time.perf_counter() - t0
            row_count = self._con.execute(f'SELECT COUNT(*) FROM "{flow}"."{name}"').fetchone()[0]
            columns = self._columns_of(flow, name)
            sample = self._head_sample(flow, name)
            self._record_lineage(flow, name, sql, "query")
        out = {
            "name": name,
            "flow": flow,
            "row_count": int(row_count),
            "columns": columns,
            "sample": sample,
            "elapsed_s": round(elapsed, 3),
        }
        hints = self._query_hints(sql, int(row_count))
        if hints:
            out["hints"] = hints
        return out

    def _query_hints(self, sql: str, row_count: int) -> list[str]:
        hints: list[str] = []
        if row_count >= _LARGE_MATERIALIZE and _is_unfiltered_star(sql):
            hints.append(
                f"Materialized {row_count:,} rows by copying a source table wholesale. If you "
                "only need a slice, add a WHERE / GROUP BY / column list — DuckDB pushes those "
                "down to the source instead of copying everything."
            )
        return hints

    def _catalog_help(self, flow: str, exc: Exception) -> str:
        names = self._result_names(flow)
        src = ", ".join(f'"{s.name}"' for s in self.sources) or "(none)"
        return (
            f"{exc}\nFlow {flow!r} contains: {names or ['(none)']}. Sources: {src} "
            "(reference attached databases as \"<source>\".\"<table>\"; read db://tables to list them)."
        )

    @contextmanager
    def _friendly_catalog_errors(self, flow: str):
        """Rewrite a DuckDB CatalogException (unknown table/source) into an actionable
        ``ValueError`` listing the flow's results and configured sources — shared by
        ``query`` / ``profile`` / ``export`` so all three fail the same helpful way."""
        try:
            yield
        except duckdb.CatalogException as exc:
            raise ValueError(self._catalog_help(flow, exc)) from exc

    # ------------------------------------------------------------------ profile ------- #
    def profile(self, sql: str, flow: str | None = None) -> dict:
        """Per-column stats over the full result of *sql*, computed in DuckDB."""
        flow = self._resolve_flow(flow)
        guard.assert_read_only(sql, "duckdb")
        with self._lock, self._friendly_catalog_errors(flow):
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._set_search_path(flow)
            t0 = time.perf_counter()
            described = self._con.execute(f"DESCRIBE {sql}").fetchall()
            cols = [(r[0], str(r[1])) for r in described]  # (name, type)
            if not cols:
                return {"row_count": 0, "elapsed_s": 0.0, "columns": {}}

            numeric = {c for c, t in cols if _is_numeric_type(t)}
            select_parts = ["COUNT(*)"]
            meta: list[tuple[str, str]] = [("_total", "_total")]
            for col, _t in cols:
                qc = '"' + col.replace('"', '""') + '"'
                select_parts.append(f"1.0 * (COUNT(*) - COUNT({qc})) / NULLIF(COUNT(*), 0)")
                meta.append((col, "null_rate"))
                select_parts.append(f"COUNT({qc})")
                meta.append((col, "non_null_count"))
                if col in numeric:
                    for fn, label in (("MIN", "min"), ("MAX", "max"), ("AVG", "mean"), ("STDDEV", "std")):
                        select_parts.append(f"{fn}({qc})")
                        meta.append((col, label))
                    for pct, label in ((0.25, "p25"), (0.50, "p50"), (0.75, "p75"), (0.95, "p95")):
                        select_parts.append(f"PERCENTILE_CONT({pct}) WITHIN GROUP (ORDER BY {qc})")
                        meta.append((col, label))
                else:
                    select_parts.append(f"COUNT(DISTINCT {qc})")
                    meta.append((col, "unique"))

            agg_sql = f"SELECT {', '.join(select_parts)} FROM ({sql}) AS _spelunk_q"
            agg_row = list(self._con.execute(agg_sql).fetchone())
            total = int(agg_row[0])
            col_stats: dict[str, dict[str, Any]] = {c: {} for c, _ in cols}
            for i, (col, stat) in enumerate(meta):
                if col == "_total":
                    continue
                val = _to_python(agg_row[i])
                if isinstance(val, float):
                    val = round(val, 6)
                col_stats[col][stat] = val

            for col in (c for c, _ in cols if c not in numeric):
                qc = '"' + col.replace('"', '""') + '"'
                top = self._con.execute(
                    f"SELECT {qc}, COUNT(*) AS f FROM ({sql}) AS _t WHERE {qc} IS NOT NULL "
                    f"GROUP BY {qc} ORDER BY f DESC LIMIT 1"
                ).fetchone()
                if top:
                    col_stats[col]["top"] = _to_python(top[0])
                    col_stats[col]["freq"] = int(top[1])
                else:
                    col_stats[col]["top"] = None
                    col_stats[col]["freq"] = 0
            elapsed = time.perf_counter() - t0
        return {"row_count": total, "elapsed_s": round(elapsed, 3), "columns": col_stats}

    # ------------------------------------------------------------------ export -------- #
    def export(self, target: str, fmt: str, path: str, flow: str | None = None) -> dict:
        """Write a saved result (or a full SELECT) to a file via DuckDB COPY.

        *target* is either a result/table name (e.g. ``joined`` or ``"src"."orders"``) or a
        ``SELECT`` / ``WITH`` query. ``fmt`` is csv, json, or parquet.
        """
        flow = self._resolve_flow(flow)
        fmt = fmt.lower().strip()
        copy_opts = {"parquet": "(FORMAT PARQUET)", "csv": "(FORMAT CSV, HEADER)", "json": "(FORMAT JSON)"}
        if fmt not in copy_opts:
            raise ValueError(f"Unsupported format {fmt!r}. Choose csv, json, or parquet.")

        abs_path = os.path.abspath(path)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        safe_path = abs_path.replace("\\", "/").replace("'", "''")

        is_query = target.strip().lower().startswith(("select", "with"))
        if is_query:
            guard.assert_read_only(target, "duckdb")
            source_expr = f"({target})"
        else:
            source_expr = _quote_qualified(target)

        with self._lock, self._friendly_catalog_errors(flow):
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._set_search_path(flow)
            self._con.execute(f"COPY {source_expr} TO '{safe_path}' {copy_opts[fmt]}")
            row_count = self._con.execute(f"SELECT COUNT(*) FROM {source_expr}").fetchone()[0]
        return {"path": abs_path, "format": fmt, "row_count": int(row_count)}

    # ------------------------------------------------------------------ import_remote - #
    def import_remote(self, sql: str, name: str, flow: str | None = None) -> dict:
        """Pull a SELECT from a SQLAlchemy-only source (SQL Server / exotic) into the flow.

        Needed because DuckDB can't ATTACH such sources, so they can't be queried in place.
        Runs the read-only guard, fetches the full result via the fallback engine, and
        registers it as ``"<flow>"."<name>"``.
        """
        flow = self._resolve_flow(flow)
        _validate_name(name)
        remotes = [s for s in self.fallback_sources if s.engine is not None]
        if not remotes:
            raise ValueError("No fallback (SQLAlchemy) source is configured; nothing to import.")
        if len(remotes) > 1:
            names = ", ".join(f'"{s.name}"' for s in remotes)
            raise ValueError(
                f"Multiple fallback (SQLAlchemy) sources are configured ({names}); import_remote "
                "cannot yet disambiguate which one to pull from. Configure a single fallback "
                "source for this session."
            )

        from .query import run_sql

        # The slow remote fetch happens outside the lock.
        result = run_sql(remotes[0].engine, sql, max_rows=None)
        import pandas as pd

        df = pd.DataFrame(result.rows, columns=result.columns)
        tmp = f"_import_{uuid4().hex}"
        with self._lock:
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._con.register(tmp, df)
            try:
                self._con.execute(f'CREATE OR REPLACE TABLE "{flow}"."{name}" AS SELECT * FROM {tmp}')
            finally:
                self._con.unregister(tmp)
            columns = self._columns_of(flow, name)
            sample = self._head_sample(flow, name)
            # Store the *remote* SELECT so replay re-pulls from the fallback source.
            self._record_lineage(flow, name, sql, "import_remote")
        return {
            "name": name,
            "flow": flow,
            "row_count": result.row_count,
            "columns": columns,
            "sample": sample,
            "elapsed_s": round(result.elapsed_s or 0.0, 3),
        }

    # ------------------------------------------------------------------ catalog / drop  #
    def catalog(self, flow: str | None = None) -> dict:
        """List flows (no arg) or the results in one flow (their columns + row counts)."""
        with self._lock:
            if flow is None:
                rows = self._con.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE catalog_name = ? AND schema_name NOT IN "
                    f"({', '.join('?' * len(_RESERVED_SCHEMAS))}) ORDER BY schema_name",
                    [self._catalog, *sorted(_RESERVED_SCHEMAS)],
                ).fetchall()
                flows = []
                for (sname,) in rows:
                    count = len(self._result_names(sname))
                    flows.append({"flow": sname, "result_count": count})
                return {"flows": flows}

            _validate_name(flow, "flow name")
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            results = []
            for tname in self._result_names(flow):
                count = self._con.execute(f'SELECT COUNT(*) FROM "{flow}"."{tname}"').fetchone()[0]
                results.append({"name": tname, "row_count": int(count), "columns": self._columns_of(flow, tname)})
            return {"flow": flow, "results": results}

    def drop(self, name: str | None = None, flow: str | None = None) -> dict:
        """Drop one result (``name`` given) or an entire flow (``name`` omitted)."""
        flow = flow or self.default_flow
        _validate_name(flow, "flow name")
        if flow in _RESERVED_SCHEMAS:
            raise ValueError(f"Cannot drop from reserved schema {flow!r}.")
        with self._lock:
            if name is not None:
                _validate_name(name)
                existed = self._con.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_catalog = ? AND table_schema = ? AND table_name = ?",
                    [self._catalog, flow, name],
                ).fetchone()[0] > 0
                self._con.execute(f'DROP TABLE IF EXISTS "{flow}"."{name}"')
                self._delete_lineage(flow, name)
                return {"flow": flow, "name": name, "dropped": bool(existed)}

            dropped = len(self._result_names(flow))
            self._con.execute(f'DROP SCHEMA IF EXISTS "{flow}" CASCADE')
            self._delete_lineage(flow, None)
            return {"flow": flow, "dropped_results": dropped}

    # ------------------------------------------------------------------ lineage ------- #
    def _load_all_lineage(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Every recorded result across all flows, keyed by (flow, name) (caller holds lock)."""
        rows = self._con.execute(
            f'SELECT flow, name, sql, kind, deps, sources, created_at, seq FROM "{_META_SCHEMA}".lineage'
        ).fetchall()
        nodes: dict[tuple[str, str], dict[str, Any]] = {}
        for flow, name, sql, kind, deps, sources, created_at, seq in rows:
            nodes[(flow, name)] = {
                "flow": flow,
                "name": name,
                "sql": sql,
                "kind": kind,
                "deps": json.loads(deps),
                "sources": json.loads(sources),
                "created_at": created_at,
                "seq": seq,
            }
        return nodes

    @staticmethod
    def _ref(flow: str, name: str) -> str:
        return f"{flow}.{name}"

    def lineage(self, name: str | None = None, flow: str | None = None) -> dict:
        """Return the provenance graph of results: the SQL and dependency edges that built them.

        With ``name``: the upstream closure that produced that result — the node plus every
        result it (transitively) depends on, following cross-flow edges. With no ``name``: every
        result in ``flow``. ``missing`` lists dependency refs with no lineage row (dropped, or an
        external input). Nodes are ordered so a dependency always precedes its dependents.
        """
        flow = self._resolve_flow(flow)
        with self._lock:
            allnodes = self._load_all_lineage()
            if name is not None:
                _validate_name(name)
                root = (flow, name)
                if root not in allnodes:
                    known = sorted(n for (f, n) in allnodes if f == flow)
                    raise ValueError(
                        f"No lineage for result {name!r} in flow {flow!r}. "
                        f"Flow {flow!r} has recorded results: {known or ['(none)']}."
                    )
                selected: dict[tuple[str, str], dict[str, Any]] = {}
                missing: list[str] = []
                stack = [root]
                while stack:
                    key = stack.pop()
                    if key in selected:
                        continue
                    node = allnodes.get(key)
                    if node is None:
                        missing.append(self._ref(*key))
                        continue
                    selected[key] = node
                    for dep in node["deps"]:
                        stack.append((dep["flow"], dep["name"]))
            else:
                selected = {k: v for k, v in allnodes.items() if k[0] == flow}
                missing = []
                present = set(selected)
                for node in selected.values():
                    for dep in node["deps"]:
                        dkey = (dep["flow"], dep["name"])
                        if dkey not in present and dkey not in allnodes:
                            missing.append(self._ref(*dkey))

        edges = []
        for key, node in selected.items():
            for dep in node["deps"]:
                edges.append({"from": self._ref(dep["flow"], dep["name"]), "to": self._ref(*key)})
        order = self._topo_order(selected)
        return {
            "flow": flow,
            "root": name,
            "nodes": [
                {
                    "flow": n["flow"],
                    "name": n["name"],
                    "kind": n["kind"],
                    "sql": n["sql"],
                    "deps": n["deps"],
                    "sources": n["sources"],
                    "created_at": n["created_at"],
                }
                for n in sorted(selected.values(), key=lambda n: n["seq"])
            ],
            "edges": edges,
            "order": [self._ref(f, nm) for (f, nm) in order],
            "missing": sorted(set(missing)),
        }

    @staticmethod
    def _topo_order(nodes: dict[tuple[str, str], dict[str, Any]]) -> list[tuple[str, str]]:
        """Kahn topological sort over the sub-DAG induced by ``nodes`` (deps outside are ignored).

        Ties are broken by ``seq`` then name for deterministic output. Raises ``ValueError`` naming
        the cycle members if the graph is not acyclic.
        """
        present = set(nodes)
        indeg = {k: 0 for k in nodes}
        adj: dict[tuple[str, str], list[tuple[str, str]]] = {k: [] for k in nodes}
        for key, node in nodes.items():
            for dep in node["deps"]:
                dkey = (dep["flow"], dep["name"])
                if dkey in present and dkey != key:
                    adj[dkey].append(key)
                    indeg[key] += 1

        def rank(k: tuple[str, str]) -> tuple[int, str, str]:
            return (nodes[k]["seq"], k[0], k[1])

        ready = sorted((k for k in nodes if indeg[k] == 0), key=rank)
        order: list[tuple[str, str]] = []
        while ready:
            key = ready.pop(0)
            order.append(key)
            for nxt in adj[key]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
            ready.sort(key=rank)
        if len(order) != len(nodes):
            cyclic = sorted(f"{f}.{n}" for (f, n) in nodes if (f, n) not in set(order))
            raise ValueError(
                f"Cannot order results — dependency cycle among: {', '.join(cyclic)}. "
                "A result was redefined to depend on one that depends on it; drop or redefine one."
            )
        return order

    # ------------------------------------------------------------------ replay -------- #
    def replay(self, flow: str | None = None, into: str | None = None, dry_run: bool = False) -> dict:
        """Rebuild a flow's results from their recorded SQL, in dependency order.

        Re-runs every ``query`` result and re-pulls every ``import_remote`` result of ``flow``,
        ordered so dependencies rebuild first. External inputs (sources, cross-flow results) must
        already exist — they are read, not rebuilt. With ``into`` the flow is rebuilt into a fresh
        namespace (non-destructive); without it the flow is refreshed in place. ``dry_run`` returns
        the plan without executing. Raises on a dependency cycle, or up-front if an ``import_remote``
        result can't be re-pulled (no single fallback source configured).
        """
        flow = self._resolve_flow(flow)
        target = self._resolve_flow(into) if into is not None else flow
        with self._lock:
            allnodes = self._load_all_lineage()
            selected = {k: v for k, v in allnodes.items() if k[0] == flow}
            if not selected:
                raise ValueError(
                    f"Flow {flow!r} has no recorded lineage to replay. "
                    "Only results built by query / import_remote in this session can be replayed."
                )
            order = self._topo_order(selected)
            plan = [
                {"name": nm, "kind": selected[(f, nm)]["kind"], "sql": selected[(f, nm)]["sql"]}
                for (f, nm) in order
            ]
            if dry_run:
                return {
                    "source_flow": flow,
                    "target_flow": target,
                    "dry_run": True,
                    "order": [nm for (_f, nm) in order],
                    "plan": plan,
                }

            # Fail before mutating anything if a remote pull can't be satisfied.
            if any(step["kind"] == "import_remote" for step in plan):
                self._require_single_remote()

            if target != flow:
                self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{target}"')
            rebuilt = []
            for (_f, nm) in order:
                node = selected[(_f, nm)]
                self._set_search_path(target)
                if node["kind"] == "import_remote":
                    rc = self._replay_import_remote(target, nm, node["sql"])
                else:
                    with self._friendly_catalog_errors(target):
                        self._con.execute(
                            f'CREATE OR REPLACE TABLE "{target}"."{nm}" AS {node["sql"]}'
                        )
                    rc = self._con.execute(
                        f'SELECT COUNT(*) FROM "{target}"."{nm}"'
                    ).fetchone()[0]
                    self._record_lineage(target, nm, node["sql"], "query")
                rebuilt.append({"name": nm, "kind": node["kind"], "row_count": int(rc)})
        return {
            "source_flow": flow,
            "target_flow": target,
            "dry_run": False,
            "order": [nm for (_f, nm) in order],
            "rebuilt": rebuilt,
        }

    def _require_single_remote(self) -> "Source":
        remotes = [s for s in self.fallback_sources if s.engine is not None]
        if not remotes:
            raise ValueError(
                "This flow contains import_remote results, but no fallback (SQLAlchemy) source "
                "is configured to re-pull them. Configure the source, or replay a flow without "
                "remote pulls."
            )
        if len(remotes) > 1:
            names = ", ".join(f'"{s.name}"' for s in remotes)
            raise ValueError(
                f"Multiple fallback sources are configured ({names}); replay cannot disambiguate "
                "which to re-pull import_remote results from."
            )
        return remotes[0]

    def _replay_import_remote(self, target: str, name: str, sql: str) -> int:
        """Re-pull one import_remote result during replay (caller holds ``_lock``)."""
        from .query import run_sql

        engine = self._require_single_remote().engine
        result = run_sql(engine, sql, max_rows=None)
        import pandas as pd

        df = pd.DataFrame(result.rows, columns=result.columns)
        tmp = f"_replay_{uuid4().hex}"
        self._con.register(tmp, df)
        try:
            self._con.execute(f'CREATE OR REPLACE TABLE "{target}"."{name}" AS SELECT * FROM {tmp}')
        finally:
            self._con.unregister(tmp)
        self._record_lineage(target, name, sql, "import_remote")
        return int(result.row_count)

    # ------------------------------------------------------------------ introspection - #
    def list_objects(self) -> list[TableInfo]:
        """List the source objects an agent can query: attached-DB tables + file views.

        Attached-DB tables are named ``<source>.<table>`` (paste-ready); file sources appear
        as their single view name. Row counts are filled for SQLite/file sources (cheap) and
        left None for remote DBs (a COUNT could be expensive).
        """
        out: list[TableInfo] = []
        with self._lock:
            for src in self.sources:
                out.extend(self._objects_for_source(src))
        return out

    def _objects_for_source(self, src: "Source") -> list[TableInfo]:
        """The queryable objects a single source contributes (caller holds ``_lock``).

        A file source is one bare view; an attached database contributes its tables/views as
        ``<source>.<table>``. Fallback sources aren't queryable in place (import_remote only).
        """
        if src.kind == "file":
            return [TableInfo(name=src.name, kind="view", row_count=self._safe_count(src.name))]
        if src.kind in ("sqlite", "postgres", "mysql"):
            rows = self._con.execute(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_catalog = ? ORDER BY table_name",
                [src.name],
            ).fetchall()
            objs: list[TableInfo] = []
            for tname, ttype in rows:
                qualified = f"{src.name}.{tname}"
                kind = "view" if "VIEW" in (ttype or "").upper() else "table"
                rc = self._safe_count(_quote_qualified(qualified)) if src.kind == "sqlite" else None
                objs.append(TableInfo(name=qualified, kind=kind, row_count=rc))
            return objs
        return []

    def describe(self, table: str) -> TableDescription:
        """Describe one source object: columns, primary key, a sample, and a row count.

        FKs/indexes are best-effort and usually empty for attached sources (DuckDB exposes less
        than SQLAlchemy reflection). *table* may be bare (``sales``) or qualified (``db.orders``).
        """
        ref = _quote_qualified(table)
        with self._lock:
            described = self._con.execute(f"DESCRIBE {ref}").fetchall()
            columns: list[ColumnInfo] = []
            pk: list[str] = []
            for row in described:
                col_name, col_type, nullable = row[0], str(row[1]), row[2]
                key = row[3] if len(row) > 3 else None
                is_pk = (key or "").upper() == "PRI"
                if is_pk:
                    pk.append(col_name)
                columns.append(
                    ColumnInfo(
                        name=col_name,
                        type=col_type,
                        nullable=(str(nullable).upper() != "NO"),
                        primary_key=is_pk,
                    )
                )
            sample_rows = self._sample_dicts(ref, [c.name for c in columns])
            row_count = self._safe_count(ref)
        return TableDescription(
            name=table,
            columns=columns,
            primary_key=pk,
            sample_rows=sample_rows,
            row_count=row_count,
        )

    def _sample_dicts(self, ref: str, col_names: list[str]) -> list[dict[str, Any]]:
        try:
            cur = self._con.execute(f"SELECT * FROM {ref} LIMIT {_SAMPLE_ROWS}")
        except duckdb.Error:
            return []
        return [dict(zip(col_names, (_to_python(v) for v in row))) for row in cur.fetchall()]

    def _safe_count(self, ref: str) -> int | None:
        try:
            return int(self._con.execute(f"SELECT COUNT(*) FROM {ref}").fetchone()[0])
        except duckdb.Error:
            return None


def _is_unfiltered_star(sql: str) -> bool:
    """True if *sql* is a single ``SELECT *`` over one table with no WHERE/GROUP/LIMIT.

    That pattern copies a whole source table into the workspace — the case the materialize
    nudge targets. Returns False on anything it can't confidently classify.
    """
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except SqlglotError:
        return False
    if not isinstance(tree, exp.Select):
        return False
    if not any(isinstance(e, exp.Star) for e in tree.expressions):
        return False
    if tree.args.get("where") or tree.args.get("group") or tree.args.get("limit"):
        return False
    return len(list(tree.find_all(exp.Table))) == 1
