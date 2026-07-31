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

from . import apifetch, guard, sources as sources_mod
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

# When listing an ATTACHed database's objects, its own system schemas are catalog metadata,
# not user data, and are hidden from db://tables. A table in the catalog's *default* schema is
# addressable bare as "<source>"."<table>"; one in any other schema needs the schema segment too
# ("<source>"."<schema>"."<table>"), so it is qualified. A kind with no default here (mysql, whose
# default schema is the database name) always gets the schema segment — 3-part is always valid.
_ATTACHED_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog"})
_ATTACHED_DEFAULT_SCHEMA = {"sqlite": "main", "postgres": "public", "ducklake": "main"}

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
# When a result is small on BOTH axes, query() returns EVERY row as the `sample` (and reports
# complete=True) instead of a 5-row head — so an agent reads its own small deliverable directly
# instead of paging it out into junk tables. Bounded on rows AND cells so a wide schema can't blow
# the token budget: full return requires row_count <= ROW_CAP and row_count*col_count <= CELL_CAP.
_FULL_SAMPLE_ROW_CAP = 50
_FULL_SAMPLE_CELL_CAP = 1000
# A materialized result larger than this, produced by an unfiltered SELECT * over a source,
# triggers a nudge: you probably wanted a slice, and DuckDB would have pushed the filter down.
_LARGE_MATERIALIZE = 100_000
# Caps for `show`: a rendered view crosses the wire as JSON inside structuredContent and is then
# held in a browser DOM, so a result that is perfectly fine as a *table* is not fine as a
# *payload*. Bounded on rows AND cells, like the full-sample caps above — 2000 rows of 60 columns
# is 120k values nobody reads. rows_for_display REFUSES past these rather than truncating: a
# silently shortened result renders as a chart that misstates the data, which is worse than no
# chart. Charts pass a far lower max_rows (see _CHART_MAX_ROWS in mcp/views.py) — 200 bars is
# already past the point of legibility.
_DISPLAY_MAX_ROWS = 2000
_DISPLAY_MAX_CELLS = 40_000
# After this many consecutive single-statement query() calls, nudge once toward query_steps —
# dependent steps batched into one call cost one round trip instead of N.
_BATCH_NUDGE_AT = 3

# A fetch result has no SQL, so its lineage row records the request that produced it instead:
# this prefix plus a compact JSON object (source, path, params, rows_from). Readable in a
# lineage graph, greppable in the tool log, and unambiguous — nothing else starts this way.
_FETCH_PREFIX = "FETCH "


def _full_sample_fits(row_count: int, col_count: int) -> bool:
    """True when a result is small on both axes → return every row as the sample."""
    return (
        row_count <= _FULL_SAMPLE_ROW_CAP
        and row_count * max(col_count, 1) <= _FULL_SAMPLE_CELL_CAP
    )


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


# Entries a workspace dir contains before any work happens: the DB file itself, its WAL, the
# default spill scratch dir (transient — safe to disregard even if a crash left files in it),
# and the api-source snapshot dir (re-fetchable derived data, not user work — a reconnect-churn
# server that attached an api source but never ran a query must still count as empty).
_WORKSPACE_SCAFFOLD_ENTRIES = frozenset(
    {"workspace.duckdb", "workspace.duckdb.wal", "spill", "snapshots"}
)


def _dir_has_user_artifacts(dir_path: str) -> bool:
    """True if ``dir_path`` holds anything beyond the workspace scaffold — e.g. a non-empty
    tool-call log — meaning the run left something worth keeping for postmortems. A 0-byte file
    (the log a server created but never wrote to) is not an artifact. Conservative: anything
    unreadable or unexpected (a foreign subdir) counts as an artifact, so the dir is kept."""
    try:
        for entry in os.listdir(dir_path):
            if entry in _WORKSPACE_SCAFFOLD_ENTRIES:
                continue
            p = os.path.join(dir_path, entry)
            try:
                if os.path.isdir(p) or os.path.getsize(p) > 0:
                    return True
            except OSError:
                return True
    except OSError:
        return True
    return False


def _probe_workspace(dir_path: str) -> str | None:
    """Classify ``dir_path``'s workspace.duckdb: ``None`` (live owner), ``"empty"``, or ``"idle"``.

    Probes by opening read-write: a live owner holds the single-writer lock so the connect
    raises (→ ``None``, never touch); a crashed/exited owner leaves an unlocked file (its WAL is
    just replayed) so it opens. Read-write — not read_only — is deliberate: a read_only open of a
    DB with a pending WAL errors, which would make us mistake a crashed orphan for a live owner
    and never reclaim it. An opened workspace is ``"empty"`` iff it has no tables outside the
    reserved schemas (i.e. no user results; file-source views live in ``main``, which is reserved)."""
    try:
        con = duckdb.connect(os.path.join(dir_path, "workspace.duckdb"))
    except Exception:
        return None
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema NOT IN "
            f"({', '.join('?' * len(_RESERVED_SCHEMAS))})",
            sorted(_RESERVED_SCHEMAS),
        ).fetchone()[0]
    except Exception:
        n = 1  # can't tell -> assume it holds results (never delete what we can't classify)
    finally:
        con.close()
    return "empty" if n == 0 else "idle"


def _reclaim_old_workspaces(parent: str, keep: int, *, exclude: str) -> list[str]:
    """Reclaim per-process workspace subdirs under ``parent``. Two tiers:

    * beyond the ``keep`` most-recent subdirs: any dir with no live owner is deleted;
    * within the keep window: a dir is deleted anyway if it is *empty* — no user results in its
      DB and no other artifacts (e.g. only a 0-byte tool log). MCP reconnect churn leaves fully
      formed but worthless workspaces that would otherwise crowd the keep window.

    Best-effort GC: only touches dirs older than the grace window, never ``exclude`` (this
    process's own dir), never a dir whose owner holds the DuckDB lock. Never raises — losing a
    race to a concurrent server just leaves a dir for the next sweep. Returns the dirs removed."""
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
    for i, path in enumerate(dirs):
        if os.path.abspath(path) == os.path.abspath(exclude):
            continue
        try:
            if now - os.path.getmtime(path) < _SWEEP_GRACE_SECONDS:
                continue
        except OSError:
            continue
        within_keep = i < keep
        if within_keep and _dir_has_user_artifacts(path):
            continue  # in the keep window and visibly non-empty: retained for postmortems
        status = _probe_workspace(path)
        if status is None:
            continue  # live owner holds the lock
        if within_keep and status != "empty":
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
    use, so every connection touch holds ``_lock``.
    """

    def __init__(
        self,
        con: "duckdb.DuckDBPyConnection",
        sources: list["Source"],
        *,
        catalog: str,
        workspace_dir: str,
        tmpdir: "tempfile.TemporaryDirectory | None" = None,
        per_process: bool = False,
    ) -> None:
        self._con = con
        self.sources = sources
        self._catalog = catalog
        self.workspace_dir = workspace_dir
        self._tmpdir = tmpdir
        # True when workspace_dir is a durable per-process subdir this session exclusively owns
        # (safe to self-delete on clean exit if nothing was ever materialized).
        self._per_process = per_process
        self._lock = threading.Lock()
        self.default_flow = "default"
        # Consecutive single-statement query() calls — at _BATCH_NUDGE_AT the response nudges
        # the agent toward query_steps (one call per pipeline, not one per step).
        self._single_query_streak = 0
        self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.default_flow}"')
        self._ensure_meta()

    # ------------------------------------------------------------------ lineage store - #
    def _ensure_meta(self) -> None:
        """Create the internal lineage store (idempotent). One row per live result.

        A result is keyed by (flow, name); ``CREATE OR REPLACE`` of a result overwrites its
        row, so the store always reflects the *current* definition. ``deps`` and ``sources``
        are JSON arrays; ``seq`` is a monotonic creation counter used as a stable tie-break
        when ordering independent nodes for replay. ``description`` is an optional one-line,
        plain-English label (nullable) carried through to ``lineage`` / ``catalog``.
        """
        self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{_META_SCHEMA}"')
        self._con.execute(
            f'CREATE TABLE IF NOT EXISTS "{_META_SCHEMA}".lineage ('
            "flow VARCHAR NOT NULL, name VARCHAR NOT NULL, sql VARCHAR NOT NULL, "
            "kind VARCHAR NOT NULL, deps VARCHAR NOT NULL, sources VARCHAR NOT NULL, "
            "created_at VARCHAR NOT NULL, seq BIGINT NOT NULL, description VARCHAR, "
            "PRIMARY KEY (flow, name))"
        )
        # Migrate a durable workspace whose lineage table predates the description column
        # (a shared/per-process workspace created before this feature). No-op on a fresh table.
        self._con.execute(
            f'ALTER TABLE "{_META_SCHEMA}".lineage ADD COLUMN IF NOT EXISTS description VARCHAR'
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
        owner are deleted. *Empty* workspaces — no user results, no artifacts beyond a 0-byte
        tool log (reconnect churn) — are reclaimed even inside the keep window.
        ``keep_workspaces <= 0`` disables the sweep (keep everything).

        A durable workspace is a single-writer DuckDB file (exclusive lock). If it's already
        held by another server instance — e.g. a second editor window on the same project — we
        DON'T crash this session: we fall back to a private ephemeral workspace and warn on
        stderr. The session stays fully functional; its results just don't persist or share
        with the instance that holds the lock. (With ``per_process`` each process has its own
        subdir, so this contention path is normally never hit.)

        The same fallback covers a ``session_dir`` that can't be *created* — a relative one
        resolves against the CWD, and an MCP host picks the CWD, not us (Claude Desktop on
        Windows launches servers in ``C:\\Windows\\system32``, where ``makedirs`` is denied).
        Degrading to ephemeral keeps the server answering; raising would kill it before
        ``initialize`` and the host would report only a disconnect.
        """
        _warm_native_imports()
        tmpdir: tempfile.TemporaryDirectory | None = None
        pp_parent: str | None = None  # the session root to sweep, only when per_process succeeds
        if session_dir is not None:
            base = os.path.abspath(session_dir)
            if per_process:
                pp_parent = base
                base = os.path.join(base, _process_workspace_id())
            try:
                os.makedirs(base, exist_ok=True)
                con = duckdb.connect(os.path.join(base, "workspace.duckdb"))
            except (OSError, duckdb.IOException) as exc:
                # Two distinct failures, one recovery. Either the dir isn't creatable/writable
                # — a RELATIVE session_dir resolves against a CWD we don't own, and MCP hosts on
                # Windows launch servers in C:\Windows\system32, where makedirs is denied — or
                # the DuckDB file is held by another server's single-writer lock. Crashing on
                # either is the worst outcome: main() dies before answering `initialize` and the
                # host reports only "server disconnected", naming nothing.
                pp_parent = None  # fell back to ephemeral — no per-process tree to sweep
                tmpdir = tempfile.TemporaryDirectory(prefix="spelunk_ws_")
                base = tmpdir.name
                con = duckdb.connect(os.path.join(base, "workspace.duckdb"))
                cause = (
                    "is locked by another server instance"
                    if isinstance(exc, duckdb.IOException)
                    else "could not be created or written to"
                )
                print(
                    f"[spelunk] durable workspace in {session_dir!r} {cause}; using an ephemeral "
                    "workspace for this session (results will not persist or be shared). "
                    f"Detail: {exc}",
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
            attached = sources_mod.attach_all(
                con, specs, snapshot_dir=os.path.join(base, "snapshots")
            )

        if pp_parent is not None and keep_workspaces > 0:
            _reclaim_old_workspaces(pp_parent, keep_workspaces, exclude=base)

        return cls(
            con,
            attached,
            catalog=catalog,
            workspace_dir=base,
            tmpdir=tmpdir,
            per_process=pp_parent is not None,
        )

    def close(self, *, reclaim_if_empty: bool = False) -> None:
        """Close the connection (and delete an ephemeral temp workspace).

        ``reclaim_if_empty=True`` additionally deletes a durable *per-process* workspace dir on
        the way out when it holds no user results and no other artifacts — a server that started
        but never did any work (MCP reconnect churn) then leaves nothing behind. Clean-shutdown
        complement to the startup sweep, which handles crash debris. No-op for shared or
        ephemeral workspaces. NOTE: any open handle into the dir (e.g. a tool-log FileHandler)
        blocks deletion on Windows — release those before calling."""
        reclaim = False
        if reclaim_if_empty and self._per_process and self._tmpdir is None:
            with self._lock:
                reclaim = not self._existing_results()
        with self._lock:
            self._con.close()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
        if reclaim and not _dir_has_user_artifacts(self.workspace_dir):
            try:
                shutil.rmtree(self.workspace_dir)
            except OSError:
                pass  # e.g. a still-open log handle; the next startup sweep will get it

    # ------------------------------------------------------------------ sources ------- #
    def add_source(self, spec: str) -> dict:
        """Attach a new data source at runtime (the same ``spec`` grammar as ``--source``).

        Builds the source, rejects a name that collides with an existing source or the workspace
        catalog, then runs its setup SQL and registers it. The source becomes queryable in *every*
        flow of this session — sources are connection-global, not flow-scoped. Returns the source's
        name, kind, and the objects it made queryable (plus, for an ``api:`` source, the fetch
        fingerprint under ``info``). Building an ``api:`` source performs its fetch here, before
        the lock, so slow network I/O never stalls other tool calls.
        """
        src = sources_mod.build_source(
            spec, snapshot_dir=os.path.join(self.workspace_dir, "snapshots")
        )
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
        out = {"name": src.name, "kind": src.kind, "objects": objects}
        if src.info is not None:
            out["info"] = src.info
        return out

    def remove_source(self, name: str) -> dict:
        """Detach a source added at runtime or configured at startup; idempotent on the SQL.

        Runs the source's teardown (``DETACH`` / ``DROP VIEW``) and forgets it. Affects this
        session's connection only — under the process-per-agent model that's the agent's own
        isolated workspace. Raises if no such source exists.
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

        # CTE names defined in this query are internal aliases, not results or external inputs —
        # a bare ``FROM <cte>`` must not be recorded as a source leaf (they resolve within the SQL).
        cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}

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
            if not db and not catalog and tname in cte_names:
                continue  # reference to a CTE defined in this same query
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

    def _referenced_names(self, sql: str, flow: str) -> set[str]:
        """Names in *flow* that *sql* references (best-effort parse; unparseable → empty).

        Used to decide terminality inside a batch: a step is terminal when no later step
        references its name. Resolution mirrors :meth:`_classify_refs` — bare and ``<flow>.name``
        (or ``<catalog>.<flow>.name``) refs count when they land in ``flow``; foreign-catalog and
        CTE refs don't. Purely a parse — touches no connection state.
        """
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import SqlglotError

        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except SqlglotError:
            return set()
        cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        refs: set[str] = set()
        for tbl in tree.find_all(exp.Table):
            tname = tbl.name
            if not tname:
                continue
            db = tbl.db
            catalog = tbl.catalog
            if not db and not catalog and tname in cte_names:
                continue
            if catalog and catalog != self._catalog:
                continue  # foreign catalog (attached DB) — never a workspace result
            if (db or flow) == flow:
                refs.add(tname)
        return refs

    def _record_lineage(
        self,
        flow: str,
        name: str,
        sql: str,
        kind: str,
        description: str | None = None,
        deps: list[dict[str, str]] | None = None,
        sources: list[str] | None = None,
    ) -> None:
        """Upsert the lineage row for a just-materialized result (caller holds ``_lock``).

        Called from ``query`` right after the CREATE, so the result set is already current.
        Dependencies are computed against every *other* live result. ``description`` is an
        optional one-line label; blank/whitespace-only is normalised to NULL. ``deps`` and
        ``sources`` may be passed explicitly by a non-SQL producer — ``fetch`` knows its own
        edges (the ``rows_from`` result, the API source) and has no SQL to parse them out of.
        """
        results = self._existing_results()
        if deps is None or sources is None:
            deps, sources = self._classify_refs(sql, flow, results, (flow, name))
        description = (description or "").strip() or None
        seq = self._con.execute(
            f'SELECT COALESCE(MAX(seq), 0) + 1 FROM "{_META_SCHEMA}".lineage'
        ).fetchone()[0]
        self._con.execute(
            f'DELETE FROM "{_META_SCHEMA}".lineage WHERE flow = ? AND name = ?', [flow, name]
        )
        self._con.execute(
            f'INSERT INTO "{_META_SCHEMA}".lineage '
            "(flow, name, sql, kind, deps, sources, created_at, seq, description) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                flow,
                name,
                sql,
                kind,
                json.dumps(deps),
                json.dumps(sources),
                datetime.now(timezone.utc).isoformat(),
                int(seq),
                description,
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

    def _descriptions_for_flow(self, flow: str) -> dict[str, str | None]:
        """Map each result name in ``flow`` to its recorded description (caller holds ``_lock``)."""
        rows = self._con.execute(
            f'SELECT name, description FROM "{_META_SCHEMA}".lineage WHERE flow = ?', [flow]
        ).fetchall()
        return {name: desc for name, desc in rows}

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

    def rows_for_display(
        self,
        name: str,
        flow: str | None = None,
        max_rows: int = _DISPLAY_MAX_ROWS,
        max_cells: int = _DISPLAY_MAX_CELLS,
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """Read a saved result IN FULL for rendering: ``(columns, rows-as-dicts)``.

        Read-only — no table is created, no lineage row is written. This is the read side of
        ``show``: a *view* of a result, not a new result.

        Refuses rather than truncates. A view that silently drops rows is a chart that misstates
        the data, so anything past ``max_rows`` / ``max_cells`` raises with the real row count and
        the fix (aggregate or ``LIMIT`` first) — the same stance as a glob that matches nothing
        erroring instead of yielding an empty view.
        """
        flow = self._resolve_flow(flow)
        _validate_name(name)
        with self._lock:
            columns = self._columns_of(flow, name)
            if not columns:
                known = self._result_names(flow)
                raise ValueError(
                    f"No result named {name!r} in flow {flow!r}. "
                    f"Flow {flow!r} holds: {known or ['(none)']}."
                )
            row_count = int(
                self._con.execute(f'SELECT COUNT(*) FROM "{flow}"."{name}"').fetchone()[0]
            )
            cells = row_count * max(len(columns), 1)
            if row_count > max_rows or cells > max_cells:
                limit = f"{max_rows} rows" if row_count > max_rows else f"{max_cells} cells"
                raise ValueError(
                    f"Result {name!r} is too large to display: {row_count} rows x "
                    f"{len(columns)} columns ({cells} cells), limit {limit}. Nothing was "
                    "truncated — a shortened view would misstate the data. Aggregate or filter "
                    f"it first with `query` (e.g. a GROUP BY, or a top-N with ORDER BY ... "
                    f"LIMIT), then show that result."
                )
            col_names = [c["name"] for c in columns]
            cur = self._con.execute(f'SELECT * FROM "{flow}"."{name}"')
            rows = [
                {k: _to_python(v) for k, v in zip(col_names, row)} for row in cur.fetchall()
            ]
        return columns, rows

    # ------------------------------------------------------------------ query --------- #
    def query(
        self, sql: str, name: str, flow: str | None = None, description: str | None = None
    ) -> dict:
        """Run a read-only SELECT over sources + flow results, materialize it as a table.

        ``name`` is required and the result is stored as ``"<flow>"."<name>"`` (replacing any
        prior result of that name). Returns the result's columns, true row_count, a sample, a
        ``complete`` flag, and any nudges. The sample is a 5-row head, but when the result is
        small on both axes (row_count <= 50 and row_count*columns <= 1000) it is the *whole*
        result — ``complete`` is True exactly when ``sample`` holds every row, so an agent can
        read a small deliverable directly instead of paging it out. ``description`` is an
        optional one-line, plain-English label stored in lineage (surfaced by ``lineage`` /
        ``catalog``).
        """
        flow = self._resolve_flow(flow)
        _validate_name(name)
        guard.assert_read_only(sql, "duckdb")
        out = self._materialize_query(sql, name, flow, description)
        self._single_query_streak += 1
        if self._single_query_streak == _BATCH_NUDGE_AT:
            out.setdefault("hints", []).append(
                f"That's {_BATCH_NUDGE_AT} single-query calls in a row. Dependent steps can run "
                "in ONE call: query(steps=[{sql, name}, ...]) executes them in order, and later "
                "steps reference earlier steps' names — a whole pipeline per round trip."
            )
        return out

    def query_steps(self, steps: list[dict], flow: str | None = None) -> dict:
        """Run an ordered batch of queries in one call — each step materialized like ``query``.

        ``steps`` is a list of ``{"sql": ..., "name": ..., "description"?: ...}`` items executed
        in list order in a single flow, so a later step can reference an earlier step's ``name``
        (it is a live, lineage-recorded result by then). Semantics are identical to calling
        :meth:`query` once per step — same guard, same ``CREATE OR REPLACE``, same lineage rows
        (including the optional one-line ``description``) — so ``lineage`` / ``replay`` see no
        difference. Steps need not form a single pipeline — a batch can be a
        dependent chain, a bundle of unrelated queries, or a mix; use it whenever you want more
        than one result in one round trip. All steps are statically validated (name, read-only
        SQL) before anything runs; execution is fail-fast — the failing step reports its error,
        earlier steps stay materialized, later steps are skipped. Every *terminal* step (one no
        later step references — always includes the last, plus any independent query) carries a
        sample and ``complete`` flag — full rows when the result is small, per :meth:`query`.
        Non-terminal intermediates stay compact (name/row_count/columns) so a long pipeline's
        scaffolding doesn't bloat the response.
        """
        flow = self._resolve_flow(flow)
        if not steps:
            raise ValueError("steps must be a non-empty list of {sql, name} items.")
        parsed: list[tuple[str, str, str | None]] = []
        for i, step in enumerate(steps):
            sql = step.get("sql") if isinstance(step, dict) else None
            name = step.get("name") if isinstance(step, dict) else None
            description = step.get("description") if isinstance(step, dict) else None
            if not sql or not name:
                raise ValueError(f"steps[{i}] must have both 'sql' and 'name'.")
            _validate_name(name, f"steps[{i}] name")
            guard.assert_read_only(sql, "duckdb")
            parsed.append((sql, name, description))

        # A step is terminal when no later step references its name → it's a deliverable, not
        # scaffolding, so it earns a sample. Refs are a static parse of each step's SQL.
        step_refs = [self._referenced_names(sql, flow) for sql, _, _ in parsed]
        terminal = [
            not any(name in step_refs[j] for j in range(i + 1, len(parsed)))
            for i, (_, name, _) in enumerate(parsed)
        ]

        self._single_query_streak = 0
        results: list[dict] = []
        completed = 0
        failed_step: int | None = None
        t0 = time.perf_counter()
        for i, (sql, name, description) in enumerate(parsed):
            if failed_step is not None:
                results.append({"name": name, "status": "skipped"})
                continue
            try:
                full = self._materialize_query(sql, name, flow, description)
            except Exception as exc:
                failed_step = i
                results.append({"name": name, "status": "failed", "error": str(exc)})
                continue
            entry = {
                "name": name,
                "status": "ok",
                "row_count": full["row_count"],
                "columns": full["columns"],
                "elapsed_s": full["elapsed_s"],
            }
            if "hints" in full:
                entry["hints"] = full["hints"]
            if terminal[i]:
                entry["sample"] = full["sample"]
                entry["complete"] = full["complete"]
            completed += 1
            results.append(entry)
        out = {
            "flow": flow,
            "step_count": len(parsed),
            "completed": completed,
            "steps": results,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        }
        if failed_step is not None:
            out["failed_step"] = failed_step
        return out

    # ------------------------------------------------------------------ fetch --------- #
    def fetch(
        self,
        source: str,
        path: str,
        name: str,
        params: dict[str, Any] | None = None,
        flow: str | None = None,
        description: str | None = None,
        rows_from: str | None = None,
        records: str | None = None,
        paginate: str | None = None,
        max_pages: int | None = None,
        max_rows: int | None = None,
        max_urls: int | None = None,
        concurrency: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict:
        """Call one endpoint of an attached API source and materialize the response as a result.

        The counterpart to :meth:`query` for data that lives behind HTTP rather than in a
        source: ``source`` names an attached ``openapi:`` connection (base URL + credentials),
        ``path`` is an endpoint under it, and the rows land as table ``"<flow>"."<name>"`` with
        the same return shape ``query`` uses. An endpoint response is a *result*, not a source —
        droppable, lineage-tracked, and flow-scoped — so chasing five endpoints costs five
        results instead of five permanent connection-global sources.

        ``params`` are query params (a ``{placeholder}`` in ``path`` consumes the param of that
        name as a path segment instead). ``rows_from`` names an existing result whose columns
        bind the remaining placeholders — one request per distinct row, the list→detail
        fan-out. Pagination and credential params are reserved; passing one is an error.

        Network I/O happens before the session lock is taken, so a slow endpoint never stalls
        another flow's call.
        """
        flow = self._resolve_flow(flow)
        _validate_name(name)
        conn, source_name, styles, hints = self._connection_for(source, path)
        req = apifetch.ApiRequest(
            path=path,
            params=dict(params or {}),
            options={
                "records": records,
                "paginate": paginate,
                "max_pages": max_pages,
                "max_rows": max_rows,
                **dict(options or {}),
            },
            param_styles=styles,
            hints=hints,
        )
        dest = os.path.join(self.workspace_dir, "snapshots", f"{flow}.{name}.ndjson")
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        provenance: dict[str, Any] = {"source": source_name, "path": path}
        if params:
            provenance["params"] = params
        deps: list[dict[str, str]] = []
        if rows_from:
            provenance["rows_from"] = rows_from
            bindings, dep_flow, dep_name = self._fanout_bindings(flow, rows_from, path, max_urls)
            deps = [{"flow": dep_flow, "name": dep_name}]
            info = apifetch.fetch_fanout(
                conn,
                req,
                bindings,
                dest,
                concurrency=concurrency or apifetch.DEFAULT_CONCURRENCY,
            )
        else:
            spec, _values = apifetch.resolve_request(conn, req)
            info = apifetch.fetch_snapshot(spec, dest, apifetch.HostLimiter())
        return self._materialize_fetch(
            flow, name, dest, info, description, provenance, deps, source_name,
            as_json=bool(apifetch.resolved_option(conn, req, "json", False)),
        )

    def fetch_steps(self, steps: list[dict], flow: str | None = None) -> dict:
        """Run an ordered batch of fetches in one call — each materialized like :meth:`fetch`.

        The point of the batch is round trips: an agent chasing several endpoints of one API
        (movies, genres, credits) issues one call instead of one per endpoint. Semantics match
        N sequential :meth:`fetch` calls — same lineage rows, same snapshots. Fail-fast: earlier
        fetches stay materialized, the failing step reports its error, later steps are skipped.
        A later step may name an earlier one in ``rows_from``.
        """
        flow = self._resolve_flow(flow)
        if not steps:
            raise ValueError("steps must be a non-empty list of {source, path, name} items.")
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not step.get("path") or not step.get("name"):
                raise ValueError(f"steps[{i}] must have both 'path' and 'name'.")
            _validate_name(step["name"], f"steps[{i}] name")

        results: list[dict] = []
        completed = 0
        failed_step: int | None = None
        t0 = time.perf_counter()
        for i, step in enumerate(steps):
            if failed_step is not None:
                results.append({"name": step["name"], "status": "skipped"})
                continue
            try:
                full = self.fetch(flow=flow, **step)
            except Exception as exc:
                failed_step = i
                results.append({"name": step["name"], "status": "failed", "error": str(exc)})
                continue
            completed += 1
            results.append({
                "name": full["name"],
                "status": "ok",
                "row_count": full["row_count"],
                "columns": full["columns"],
                "info": full["info"],
                "sample": full["sample"],
                "complete": full["complete"],
            })
        out = {
            "flow": flow,
            "step_count": len(steps),
            "completed": completed,
            "steps": results,
            "elapsed_s": round(time.perf_counter() - t0, 3),
        }
        if failed_step is not None:
            out["failed_step"] = failed_step
        return out

    def _connection_for(
        self, source: str, path: str
    ) -> tuple[Any, str, dict[str, tuple[str, bool]], dict[str, Any]]:
        """Resolve a source name to its API connection, param styles, and endpoint hints.

        Both of the latter come from the source's own endpoint catalog: the spec already
        declares whether a list param goes over the wire as ``a,b`` or ``k=a&k=b``, and whether
        this endpoint paginates and where its records live — so nobody has to restate the
        obvious per call. An uncatalogued path is not an error: specs are routinely incomplete,
        and the spec establishes the connection, not the reachable surface.
        """
        with self._lock:
            src = next((s for s in self.sources if s.name == source), None)
            if src is None or src.connection is None:
                connections = sorted(s.name for s in self.sources if s.connection is not None)
                raise ValueError(
                    f"No API connection named {source!r}. Connections: "
                    f"{connections or ['(none)']}. Attach one with "
                    "add_source('name=openapi:<spec-url-or-path> auth_env=<ENV>')."
                )
            styles, hints = self._endpoint_meta(src.name, path)
        return src.connection, src.name, styles, hints

    def _endpoint_meta(
        self, source: str, path: str
    ) -> tuple[dict[str, tuple[str, bool]], dict[str, Any]]:
        """``({param: (style, explode)}, fetch-option hints)`` for one catalogued GET endpoint.

        Returns empty dicts for an endpoint the spec doesn't describe — the caller then supplies
        whatever it knows. A cursor-style hint is deliberately NOT emitted: ``cursor_path`` lives
        in the response body, which OpenAPI does not declare reliably, so guessing it would send
        a fetch off to a page that doesn't exist. Caller holds ``_lock``.
        """
        try:
            row = self._con.execute(
                f'SELECT params, pagination_hint, records_hint FROM main."{source}" '
                "WHERE path = ? AND method = 'get'",
                [path],
            ).fetchone()
        except duckdb.Error:
            return {}, {}
        if row is None:
            return {}, {}
        params, pagination_hint, records_hint = row

        styles: dict[str, tuple[str, bool]] = {}
        query_names: dict[str, str] = {}
        for param in params or []:
            if isinstance(param, dict) and param.get("name"):
                styles[param["name"]] = (param.get("style") or "form", bool(param.get("explode")))
                if param.get("location") == "query":
                    query_names[param["name"].lower()] = param["name"]

        # Always stated for a catalogued endpoint, None included: "this response has no wrapper
        # path" is real information, and it has to be able to override a connection-wide
        # records= the way `paginate: none` overrides a connection-wide paginate=.
        hints: dict[str, Any] = {
            "records": records_hint if records_hint and records_hint != "<root>" else None
        }
        size = next(
            (query_names[n] for n in ("limit", "per_page", "page_size", "count", "$top")
             if n in query_names),
            None,
        )
        if pagination_hint == "page":
            hints["paginate"] = "page"
            if size:
                hints["size_param"] = size
        elif pagination_hint == "offset":
            hints["paginate"] = "offset"
            offset = next(
                (query_names[n] for n in ("offset", "skip", "$skip") if n in query_names), None
            )
            if offset:
                hints["offset_param"] = offset
            if size:
                hints["size_param"] = size
        elif pagination_hint is None:
            # The endpoint declares no pagination params at all — a detail endpoint. Saying so
            # matters: a connection-wide `paginate=page` default would otherwise make every
            # detail fetch append ?page=1 and re-request a single object until the repeat-page
            # guard trips. Correct an under-documented endpoint with paginate= on the call.
            hints["paginate"] = "none"
        return styles, hints

    def _fanout_bindings(
        self, flow: str, rows_from: str, path: str, max_urls: int | None
    ) -> tuple[list[dict[str, Any]], str, str]:
        """Read one binding dict per distinct row of the ``rows_from`` result.

        The prep query IS the fan-out spec: selecting, aliasing and limiting the rows is plain
        SQL, so filtering to a top-N or anti-joining against what was already fetched composes
        with everything. Rows are ordered by the bound columns so the same prep query always
        produces the same snapshot, and a NULL in any bound column drops that row — a URL with
        ``/None/`` in it is never what the caller meant.
        """
        placeholders = sorted(set(apifetch.placeholders_in(path)))
        if not placeholders:
            raise ValueError(
                f"rows_from={rows_from!r} needs at least one {{placeholder}} in the path to "
                "bind its columns to, e.g. path='/3/movie/{movie_id}'."
            )
        dep_flow, _, dep_name = rows_from.rpartition(".")
        dep_flow = dep_flow or flow
        # Both halves are interpolated into quoted identifiers below, so they go through the
        # same gate as every other flow/name entry point — a `"` in either would otherwise
        # close the identifier and run the remainder on the read-WRITE workspace connection.
        _validate_name(dep_flow, "rows_from flow name")
        _validate_name(dep_name, "rows_from result name")
        cols = ", ".join(f'"{p}"' for p in placeholders)
        limit = max_urls if max_urls is not None else apifetch.DEFAULT_MAX_URLS
        with self._lock:
            try:
                rows = self._con.execute(
                    f'SELECT DISTINCT {cols} FROM "{dep_flow}"."{dep_name}" '
                    f"WHERE {' AND '.join(f'{c} IS NOT NULL' for c in cols.split(', '))} "
                    f"ORDER BY {cols}"
                ).fetchall()
            except duckdb.Error as exc:
                raise ValueError(
                    f"rows_from={rows_from!r}: cannot read column(s) {placeholders} from it "
                    f"({exc}). Alias the prep query's columns to the placeholder names, e.g. "
                    f"SELECT id AS {placeholders[0]} FROM ..."
                ) from None
        if len(rows) > limit:
            raise ValueError(
                f"rows_from={rows_from!r} has {len(rows)} distinct rows, over the {limit}-URL "
                "cap — that would be one HTTP request each. LIMIT the prep query (or raise "
                "max_urls deliberately). Refusing rather than silently fetching a prefix."
            )
        return (
            [{p: _to_python(v) for p, v in zip(placeholders, row)} for row in rows],
            dep_flow,
            dep_name,
        )

    def _materialize_fetch(
        self,
        flow: str,
        name: str,
        dest: str,
        info: dict,
        description: str | None,
        provenance: dict[str, Any],
        deps: list[dict[str, str]],
        source_name: str,
        *,
        as_json: bool = False,
    ) -> dict:
        """Load a fetched NDJSON snapshot into the flow + record its ``fetch`` lineage node.

        Reads the snapshot through the same scan an ``api:`` source view uses, so a fetched
        endpoint and an attached one infer their columns identically — including the inference
        settings that keep a wide or late-varying payload from failing the load.
        """
        with self._lock:
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._set_search_path(flow)
            t0 = time.perf_counter()
            self._con.execute(
                f'CREATE OR REPLACE TABLE "{flow}"."{name}" AS SELECT * FROM '
                + sources_mod._snapshot_scan(dest, as_json)
            )
            elapsed = time.perf_counter() - t0
            row_count = int(
                self._con.execute(f'SELECT COUNT(*) FROM "{flow}"."{name}"').fetchone()[0]
            )
            columns = self._columns_of(flow, name)
            n = row_count if _full_sample_fits(row_count, len(columns)) else _SAMPLE_ROWS
            sample = self._head_sample(flow, name, n)
            self._record_lineage(
                flow,
                name,
                _FETCH_PREFIX + json.dumps(provenance, sort_keys=True, default=str),
                "fetch",
                description,
                deps=deps,
                sources=[source_name],
            )
            self._checkpoint()
        out = {
            "name": name,
            "flow": flow,
            "row_count": row_count,
            "columns": columns,
            "sample": sample,
            "complete": len(sample) == row_count,
            "elapsed_s": round(elapsed, 3),
            "info": info,
        }
        if info.get("skipped_count"):
            out.setdefault("hints", []).append(
                f"{info['skipped_count']} of {info['urls']} URLs 404'd and were skipped — see "
                "info.skipped before aggregating over this result."
            )
        if info.get("truncated"):
            out.setdefault("hints", []).append(
                "A cap (max_pages/max_rows) stopped this fetch early — the API had more to give."
            )
        return out

    def _materialize_query(
        self, sql: str, name: str, flow: str, description: str | None = None
    ) -> dict:
        """CREATE OR REPLACE the result table + record lineage (caller validated name + SQL)."""
        with self._lock:
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._set_search_path(flow)
            t0 = time.perf_counter()
            with self._friendly_catalog_errors(flow, sql):
                self._con.execute(f'CREATE OR REPLACE TABLE "{flow}"."{name}" AS {sql}')
            elapsed = time.perf_counter() - t0
            row_count = int(self._con.execute(f'SELECT COUNT(*) FROM "{flow}"."{name}"').fetchone()[0])
            columns = self._columns_of(flow, name)
            n = row_count if _full_sample_fits(row_count, len(columns)) else _SAMPLE_ROWS
            sample = self._head_sample(flow, name, n)
            self._record_lineage(flow, name, sql, "query", description)
            self._checkpoint()
        out = {
            "name": name,
            "flow": flow,
            "row_count": row_count,
            "columns": columns,
            "sample": sample,
            "complete": len(sample) == row_count,
            "elapsed_s": round(elapsed, 3),
        }
        hints = self._query_hints(sql, int(row_count))
        if hints:
            out["hints"] = hints
        return out

    def _checkpoint(self) -> None:
        """Fold the WAL into the workspace ``.duckdb`` file after a successful write.

        DuckDB defers merging committed changes from the write-ahead log into the main database
        file until the WAL crosses a size threshold, so freshly materialized results can linger
        in ``workspace.duckdb.wal`` well after the ``query`` returns. We ``CHECKPOINT`` the workspace
        catalog explicitly at the end of each materialization — so ``query`` commits after every
        call, and ``query(steps=[...])`` after every step that succeeds. The catalog is named so
        we only touch the workspace, never the read-only attached sources. Must be called while
        holding ``_lock``.

        Best-effort: a checkpoint can legitimately no-op or abort (e.g. a concurrent reader on the
        WAL) and the data is already durably committed to the WAL regardless, so a failure here is
        never fatal to the query — the next successful checkpoint folds it in.
        """
        try:
            self._con.execute(f'CHECKPOINT "{self._catalog}"')
        except duckdb.Error:
            pass

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

    def _conversion_help(self, sql: str, exc: Exception) -> str:
        """Name the JSON-typing trap behind a conversion error over API-snapshot sources.

        DuckDB reports the *value* that wouldn't cast and not the column, the file, or the reason
        — "Could not convert string 'x@y.gov' to INT128" — which is a long bisect for anyone who
        doesn't already know that two JSON snapshots infer their types independently. We can't
        name the field either, but naming the mechanism is what turns the bisect into a fix.
        """
        involved = [s.name for s in self.sources if s.kind == "api" and _references(sql, s.name)]
        if not involved:
            return str(exc)
        return (
            f"{exc}\nThis query reads API snapshot source(s) {', '.join(involved)}, whose columns "
            "are INFERRED from each snapshot's own JSON — independently per source. Combining two "
            "of them (UNION/UNION ALL) can therefore hit a type one side never saw. Either query "
            "them separately, or re-add the source(s) with json=true to get one raw JSON column "
            "and pick fields out with json_extract / ->> instead of inferred types."
        )

    @contextmanager
    def _friendly_catalog_errors(self, flow: str, sql: str = ""):
        """Rewrite a DuckDB CatalogException (unknown table/source) into an actionable
        ``ValueError`` listing the flow's results and configured sources — shared by
        ``query`` / ``profile`` / ``export`` so all three fail the same helpful way.

        A ConversionException over an ``api:`` source gets the same treatment for the same
        reason: the raw DuckDB message names a value and nothing else."""
        try:
            yield
        except duckdb.CatalogException as exc:
            raise ValueError(self._catalog_help(flow, exc)) from exc
        except duckdb.ConversionException as exc:
            raise ValueError(self._conversion_help(sql, exc)) from exc

    # ------------------------------------------------------------------ profile ------- #
    def profile(self, sql: str, flow: str | None = None) -> dict:
        """Per-column stats over the full result of *sql*, computed in DuckDB."""
        flow = self._resolve_flow(flow)
        guard.assert_read_only(sql, "duckdb")
        with self._lock, self._friendly_catalog_errors(flow, sql):
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

        with self._lock, self._friendly_catalog_errors(flow, source_expr):
            self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{flow}"')
            self._set_search_path(flow)
            self._con.execute(f"COPY {source_expr} TO '{safe_path}' {copy_opts[fmt]}")
            row_count = self._con.execute(f"SELECT COUNT(*) FROM {source_expr}").fetchone()[0]
        return {"path": abs_path, "format": fmt, "row_count": int(row_count)}

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
            descriptions = self._descriptions_for_flow(flow)
            results = []
            for tname in self._result_names(flow):
                count = self._con.execute(f'SELECT COUNT(*) FROM "{flow}"."{tname}"').fetchone()[0]
                results.append({
                    "name": tname,
                    "row_count": int(count),
                    "columns": self._columns_of(flow, tname),
                    "description": descriptions.get(tname),
                })
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
            "SELECT flow, name, sql, kind, deps, sources, created_at, seq, description "
            f'FROM "{_META_SCHEMA}".lineage'
        ).fetchall()
        nodes: dict[tuple[str, str], dict[str, Any]] = {}
        for flow, name, sql, kind, deps, sources, created_at, seq, description in rows:
            nodes[(flow, name)] = {
                "flow": flow,
                "name": name,
                "sql": sql,
                "kind": kind,
                "deps": json.loads(deps),
                "sources": json.loads(sources),
                "created_at": created_at,
                "seq": seq,
                "description": description,
            }
        return nodes

    @staticmethod
    def _ref(flow: str, name: str) -> str:
        return f"{flow}.{name}"

    def lineage(
        self,
        name: str | None = None,
        flow: str | None = None,
        render: str | None = None,
        path: str | None = None,
    ) -> dict:
        """Return the provenance graph of results: the SQL and dependency edges that built them.

        With ``name``: the upstream closure that produced that result — the node plus every
        result it (transitively) depends on, following cross-flow edges. With no ``name``: every
        result in ``flow``. ``missing`` lists dependency refs with no lineage row (dropped, or an
        external input). Nodes are ordered so a dependency always precedes its dependents. Each
        node carries its optional one-line ``description`` (``None`` when none was given) so the
        DAG reads as a plain-English, sequential story for a non-technical audience.

        ``render`` (``"mermaid"`` or ``"dot"``) adds a ready-to-display diagram string under the
        key matching the requested format — built deterministically from the same nodes/edges, so
        no downstream parsing is needed. ``path`` writes that diagram to a file (defaulting
        ``render`` to ``"mermaid"``) and reports the absolute path under ``"rendered_to"``.
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
        nodes = [
            {
                "flow": n["flow"],
                "name": n["name"],
                "kind": n["kind"],
                "description": n["description"],
                "sql": n["sql"],
                "deps": n["deps"],
                "sources": n["sources"],
                "created_at": n["created_at"],
            }
            for n in sorted(selected.values(), key=lambda n: n["seq"])
        ]
        result = {
            "flow": flow,
            "root": name,
            "nodes": nodes,
            "edges": edges,
            "order": [self._ref(f, nm) for (f, nm) in order],
            "missing": sorted(set(missing)),
        }

        if path is not None and render is None:
            render = "mermaid"
        if render is not None:
            renderers = {"mermaid": self._to_mermaid, "dot": self._to_dot}
            if render not in renderers:
                raise ValueError(
                    f"Unsupported render {render!r}. Choose 'mermaid' or 'dot'."
                )
            diagram = renderers[render](nodes, edges)
            result[render] = diagram
            if path is not None:
                abs_path = os.path.abspath(path)
                parent = os.path.dirname(abs_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(diagram + "\n")
                result["rendered_to"] = abs_path
        return result

    @staticmethod
    def _graph_layout(
        nodes: list[dict[str, Any]], edges: list[dict[str, str]]
    ) -> tuple[list[str], list[tuple[str, str]], dict[str, str]]:
        """Shared layout for the diagram renderers: leaf refs, the edges to draw, and node ids.

        Leaves are the external inputs a result reads — the sources (files, DB tables) plus any
        dependency whose lineage row is gone (an edge ``from`` that is not a materialized result),
        each drawn with an edge into the result that consumes it. The returned edge list is the
        *complete, de-duplicated* set to draw, so a renderer emits it in one pass and cannot
        double-draw a dropped dependency (which appears both in ``edges`` and as a leaf). Ids
        ``n0, n1, ...`` are assigned deterministically — results in ``seq`` order first, then
        leaves in first-seen order — so a given graph lays out identically in every format.
        """
        result_refs = [f"{n['flow']}.{n['name']}" for n in nodes]
        result_set = set(result_refs)

        # Result-to-result edges, then the leaf edges feeding each result.
        draw_edges: list[tuple[str, str]] = [
            (e["from"], e["to"]) for e in edges if e["from"] in result_set
        ]
        leaf_edges: list[tuple[str, str]] = []
        for n in nodes:
            ref = f"{n['flow']}.{n['name']}"
            for src in n["sources"]:
                leaf_edges.append((src, ref))
        for e in edges:
            if e["from"] not in result_set:
                leaf_edges.append((e["from"], e["to"]))

        leaf_refs = list(dict.fromkeys(src for src, _ in leaf_edges))
        # dict.fromkeys de-dups while preserving first-seen order (a source read by two results
        # keeps both edges; the same edge recorded twice collapses to one).
        draw_edges = list(dict.fromkeys(draw_edges + leaf_edges))
        ids = {ref: f"n{i}" for i, ref in enumerate(result_refs + leaf_refs)}
        return leaf_refs, draw_edges, ids

    @classmethod
    def _to_mermaid(cls, nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> str:
        """Serialize a lineage graph to a Mermaid ``flowchart TD`` string.

        Results render as grey boxes with a **bold** name over an *italic* description; external
        leaves (sources + dropped deps) render as blue cylinders — the datastore glyph — with an
        edge into the result that consumes them, so shape *and* colour distinguish an input from a
        computed step, the diagram has no dangling edges, and it shows where data enters.
        """
        def esc(text: str) -> str:
            # Mermaid HTML labels: entity-escape (& first) so markup in user text can't break out.
            return (
                text.replace("\\", "/")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("\n", " ")
            )

        leaf_refs, draw_edges, ids = cls._graph_layout(nodes, edges)
        lines = ["flowchart TD"]
        for n in nodes:
            ref = f"{n['flow']}.{n['name']}"
            # kind is "query" for every result today — only surface it if that ever changes.
            suffix = "" if n["kind"] == "query" else f" ({esc(n['kind'])})"
            label = f"<b>{esc(n['name'])}</b>{suffix}"
            if n["description"]:
                label += f"<br/><i>{esc(n['description'])}</i>"
            lines.append(f'    {ids[ref]}["{label}"]')
        for ref in leaf_refs:
            lines.append(f'    {ids[ref]}[("<b>{esc(ref)}</b>")]')
        for src, dst in draw_edges:
            lines.append(f'    {ids[src]} --> {ids[dst]}')
        # Explicit fills + text colour so the diagram reads the same on a light or dark page.
        lines.append(
            "    classDef source fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#0f172a"
        )
        lines.append(
            "    classDef result fill:#f1f5f9,stroke:#334155,stroke-width:2px,color:#0f172a"
        )
        result_ids = [ids[f"{n['flow']}.{n['name']}"] for n in nodes]
        if result_ids:
            lines.append(f"    class {','.join(result_ids)} result")
        if leaf_refs:
            lines.append(f"    class {','.join(ids[r] for r in leaf_refs)} source")
        return "\n".join(lines)

    @classmethod
    def _to_dot(cls, nodes: list[dict[str, Any]], edges: list[dict[str, str]]) -> str:
        """Serialize a lineage graph to a Graphviz DOT ``digraph`` string.

        Mirrors the Mermaid renderer — results are grey boxes with a bold name over an italic
        description, sources are blue cylinders — using HTML-like labels, so ``dot -Tsvg`` yields a
        real image. Node ids match ``_graph_layout`` for byte-identical output per graph.
        """
        def esc(text: str) -> str:
            # DOT HTML-like label (<...> delimited): entity-escape, & first.
            return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", " ")
            )

        leaf_refs, draw_edges, ids = cls._graph_layout(nodes, edges)
        lines = [
            "digraph lineage {",
            "  rankdir=TB;",
            '  node [fontname="Helvetica", style=filled, penwidth=2];',
        ]
        for n in nodes:
            ref = f"{n['flow']}.{n['name']}"
            suffix = "" if n["kind"] == "query" else f" ({esc(n['kind'])})"
            label = f"<B>{esc(n['name'])}</B>{suffix}"
            if n["description"]:
                label += f"<BR/><I>{esc(n['description'])}</I>"
            lines.append(
                f"  {ids[ref]} [shape=box, fillcolor=\"#f1f5f9\", color=\"#334155\", "
                f"fontcolor=\"#0f172a\", label=<{label}>];"
            )
        for ref in leaf_refs:
            lines.append(
                f"  {ids[ref]} [shape=cylinder, fillcolor=\"#dbeafe\", color=\"#2563eb\", "
                f"fontcolor=\"#0f172a\", label=<<B>{esc(ref)}</B>>];"
            )
        for src, dst in draw_edges:
            lines.append(f'  {ids[src]} -> {ids[dst]};')
        lines.append("}")
        return "\n".join(lines)

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

        Re-runs every ``query`` result of ``flow``, ordered so dependencies rebuild first.
        External inputs (sources, cross-flow results) must already exist — they are read, not
        rebuilt. With ``into`` the flow is rebuilt into a fresh namespace (non-destructive);
        without it the flow is refreshed in place. ``dry_run`` returns the plan without executing.
        Raises on a dependency cycle.

        **``fetch`` results are preserved, not re-fetched** — they are inputs here, like a
        source. Their rows are a pinned snapshot, and silently re-issuing the requests would put
        network latency, rate limits and a changed upstream inside an operation whose whole
        purpose is to rebuild deterministically. They are reported under ``preserved``; refresh
        one by calling ``fetch`` again. In a rebuild ``into`` a fresh flow they are copied, so
        the new flow is complete and the original snapshot stays untouched.
        """
        flow = self._resolve_flow(flow)
        target = self._resolve_flow(into) if into is not None else flow
        with self._lock:
            allnodes = self._load_all_lineage()
            selected = {k: v for k, v in allnodes.items() if k[0] == flow}
            if not selected:
                raise ValueError(
                    f"Flow {flow!r} has no recorded lineage to replay. "
                    "Only results built by query in this session can be replayed."
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

            if target != flow:
                self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{target}"')
            rebuilt = []
            preserved = []
            for (_f, nm) in order:
                node = selected[(_f, nm)]
                self._set_search_path(target)
                if node["kind"] == "fetch":
                    # An input, not a step: keep the pinned snapshot rather than re-issuing the
                    # request. Into a fresh flow it is copied so the rebuild is still complete.
                    if target != flow:
                        self._con.execute(
                            f'CREATE OR REPLACE TABLE "{target}"."{nm}" AS '
                            f'SELECT * FROM "{flow}"."{nm}"'
                        )
                        # Carry the recorded edges over. A fetch node has no SQL to re-derive
                        # them from (that is why `fetch` passes them explicitly), so dropping
                        # them here would orphan the copy: `ids -> details -> roi` would render
                        # end-to-end in the original flow and as a bare leaf in the rebuild.
                        # Deps that are themselves being rebuilt now live in `target`, so they
                        # are remapped — the query branch gets this for free by re-parsing its
                        # SQL against the target search_path.
                        self._record_lineage(
                            target, nm, node["sql"], "fetch", node.get("description"),
                            deps=[
                                {
                                    "flow": target if (d["flow"], d["name"]) in selected
                                    else d["flow"],
                                    "name": d["name"],
                                }
                                for d in node["deps"]
                            ],
                            sources=list(node["sources"]),
                        )
                    rc = self._con.execute(f'SELECT COUNT(*) FROM "{target}"."{nm}"').fetchone()[0]
                    preserved.append({"name": nm, "kind": "fetch", "row_count": int(rc)})
                    continue
                with self._friendly_catalog_errors(target, node["sql"]):
                    self._con.execute(
                        f'CREATE OR REPLACE TABLE "{target}"."{nm}" AS {node["sql"]}'
                    )
                rc = self._con.execute(
                    f'SELECT COUNT(*) FROM "{target}"."{nm}"'
                ).fetchone()[0]
                self._record_lineage(target, nm, node["sql"], "query", node.get("description"))
                rebuilt.append({"name": nm, "kind": node["kind"], "row_count": int(rc)})
        out = {
            "source_flow": flow,
            "target_flow": target,
            "dry_run": False,
            "order": [nm for (_f, nm) in order],
            "rebuilt": rebuilt,
        }
        if preserved:
            out["preserved"] = preserved
        return out

    # ------------------------------------------------------------------ introspection - #
    def list_objects(self) -> list[TableInfo]:
        """List the source objects an agent can query: attached-DB tables + file views.

        Attached-DB tables are named ``<source>.<table>`` (or ``<source>.<schema>.<table>`` for a
        non-default schema — paste-ready either way); file sources appear as their single view
        name. Row counts are filled only where a COUNT is cheap (SQLite tables); file/lakehouse
        views and remote DBs are left None (a COUNT could scan a whole remote object) — ``describe``
        fills them on demand.
        """
        out: list[TableInfo] = []
        with self._lock:
            for src in self.sources:
                out.extend(self._objects_for_source(src))
        return out

    def _objects_for_source(self, src: "Source") -> list[TableInfo]:
        """The queryable objects a single source contributes (caller holds ``_lock``).

        A file source is one bare view; an attached database contributes its user tables/views.
        A table in the catalog's default schema is named ``<source>.<table>``; one in any other
        schema is named ``<source>.<schema>.<table>`` so it stays addressable (a Postgres/MySQL
        source often keeps its tables in a non-default schema). The attached DB's own system
        schemas (information_schema, pg_catalog) are metadata, not data, and are hidden.
        """
        if src.kind in ("file", "delta", "iceberg", "api", "openapi"):
            # No eager COUNT(*): a file/lakehouse source can be remote (https/s3/...), so counting
            # here would trigger a full scan while holding the session lock and stall every other
            # tool call. describe() (db://{table}) fills the count lazily, on demand. (An api
            # snapshot is local and cheap, but keeping one code path keeps the contract uniform.)
            return [TableInfo(name=src.name, kind="view", row_count=None)]
        if src.kind in ("sqlite", "postgres", "mysql", "ducklake"):
            rows = self._con.execute(
                "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                "WHERE table_catalog = ? ORDER BY table_schema, table_name",
                [src.name],
            ).fetchall()
            default_schema = _ATTACHED_DEFAULT_SCHEMA.get(src.kind)
            objs: list[TableInfo] = []
            for schema, tname, ttype in rows:
                if schema in _ATTACHED_SYSTEM_SCHEMAS:
                    continue
                qualified = (
                    f"{src.name}.{tname}"
                    if schema == default_schema
                    else f"{src.name}.{schema}.{tname}"
                )
                kind = "view" if "VIEW" in (ttype or "").upper() else "table"
                rc = self._safe_count(_quote_qualified(qualified)) if src.kind == "sqlite" else None
                objs.append(TableInfo(name=qualified, kind=kind, row_count=rc))
            return objs
        return []

    def describe(self, table: str) -> TableDescription:
        """Describe one source object: columns, primary key, a sample, and a row count.

        FKs/indexes are best-effort and usually empty for attached sources (DuckDB exposes little
        constraint metadata). *table* may be bare (``sales``) or qualified (``db.orders``).
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


def _references(sql: str, name: str) -> bool:
    """Whether *sql* reads a table/view called *name* (case-insensitive).

    Used only to decide whether an error message is worth enriching, so an unparseable query
    falls back to a word-boundary match rather than giving up: a missed hint is worse than an
    occasional one aimed at a name that appeared in a string literal.
    """
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except SqlglotError:
        return re.search(rf"\b{re.escape(name)}\b", sql, re.IGNORECASE) is not None
    return any(t.name.lower() == name.lower() for t in tree.find_all(exp.Table))


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
