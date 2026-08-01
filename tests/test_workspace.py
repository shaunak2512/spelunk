"""Workspace lifecycle claims that nothing exercised: resource guards, the ephemeral default,
the sweep's escape hatch and grace window, checkpoint semantics, and out-of-core execution.

Claims WSP-004, WSP-009, WSP-010, WSP-012, WSP-013, WSP-014, WSP-016.

Several of these are documented LIMITATIONS or escape hatches rather than features. They earn
tests for the same reason a feature does: if the boundary moves, someone loses data.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time

import duckdb
import pytest

from spelunk.core.duck import DuckSession

_UNITS = {"KIB": 2**10, "MIB": 2**20, "GIB": 2**30, "KB": 10**3, "MB": 10**6, "GB": 10**9}


def _setting(session: DuckSession, name: str) -> str:
    return session._con.execute(
        "SELECT value FROM duckdb_settings() WHERE name = ?", [name]
    ).fetchone()[0]


def _as_bytes(value: str) -> float:
    """DuckDB echoes byte settings in its own units ('512MB' comes back as '488.2 MiB')."""
    match = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", value)
    assert match, f"unparseable size setting: {value!r}"
    return float(match.group(1)) * _UNITS[match.group(2).upper()]


class _ConnectionProxy:
    """Wraps the DuckDB connection so a test can observe or refuse specific statements.

    Needed because DuckDBPyConnection.execute is read-only — it cannot be monkeypatched in
    place, so the session's `_con` is swapped for this instead.
    """

    def __init__(self, con, on_execute):
        self._con = con
        self._on_execute = on_execute

    def execute(self, sql, *args, **kwargs):
        self._on_execute(sql)
        return self._con.execute(sql, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._con, item)


class TestEphemeralWorkspace:
    """WSP-004: open(session_dir=None) is a private temp workspace."""

    def test_lands_in_a_private_temp_dir_not_the_cwd(self, sqlite_file, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        before = set(os.listdir(tmp_path))
        s = DuckSession.open([f"shop={sqlite_file}"])
        try:
            ws = os.path.abspath(s.workspace_dir)
            assert ws.startswith(os.path.abspath(tempfile.gettempdir()))
            assert os.path.isfile(os.path.join(ws, "workspace.duckdb"))
            # Nothing is dropped into the caller's working directory.
            assert set(os.listdir(tmp_path)) == before
            assert s.query("SELECT 1 AS a", "r")["sample"] == [[1]]
        finally:
            s.close()

    def test_is_reclaimed_on_close(self, sqlite_file):
        s = DuckSession.open([f"shop={sqlite_file}"])
        ws = s.workspace_dir
        s.query("SELECT 1 AS a", "r")
        s.close()
        assert not os.path.exists(ws), "an ephemeral workspace must not outlive its session"

    def test_two_ephemeral_sessions_are_isolated(self, sqlite_file):
        s1 = DuckSession.open([f"shop={sqlite_file}"])
        s2 = DuckSession.open([f"shop={sqlite_file}"])
        try:
            assert s1.workspace_dir != s2.workspace_dir
            s1.query("SELECT 1 AS a", "tmp")
            s2.query("SELECT 2 AS a", "tmp")
            assert s1.query("SELECT a FROM tmp", "r1")["sample"] == [[1]]
            assert s2.query("SELECT a FROM tmp", "r2")["sample"] == [[2]]
        finally:
            s1.close()
            s2.close()


class TestUnwritableSessionDir:
    """WSP-017: a session_dir that cannot be CREATED degrades to ephemeral instead of raising.

    The real-world trigger is the relative default: --session-dir defaults to '.spelunk_session',
    which resolves against a CWD the MCP *host* picks. Claude Desktop on Windows launches servers
    in C:\\Windows\\system32, where makedirs is denied — and a raise there kills main() before it
    answers `initialize`, so the host reports only "server disconnected" and names no cause.

    Simulated rather than acted out: making a directory genuinely unwritable is an ACL operation
    with no portable form, and the recovery path is what's under test, not the OS's enforcement.
    """

    def test_falls_back_to_ephemeral_and_warns(self, sqlite_file, tmp_path, capsys, monkeypatch):
        denied = str(tmp_path / "denied")
        real_makedirs = os.makedirs

        def refuse_under_denied(path, *args, **kwargs):
            if os.path.abspath(path).startswith(denied):
                raise PermissionError(5, "Access is denied", path)  # WinError 5, as reported
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setattr("spelunk.core.duck.os.makedirs", refuse_under_denied)

        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=denied)
        try:
            ws = os.path.abspath(s.workspace_dir)
            assert ws.startswith(os.path.abspath(tempfile.gettempdir())), "not an ephemeral dir"
            assert not os.path.exists(denied), "the refused dir must not have been created"
            # The point of degrading: the session still serves, sources and all.
            assert s.query("SELECT 1 AS a", "r")["sample"] == [[1]]
            assert s.query('SELECT COUNT(*) AS n FROM "shop"."orders"', "n")["sample"] == [[3]]
        finally:
            s.close()

        err = capsys.readouterr().err
        assert "could not be created" in err, f"no diagnosis on stderr: {err!r}"
        assert "Access is denied" in err, "the OS cause must survive into the warning"
        assert "ephemeral" in err
        # Same recovery as lock contention, but NOT the same diagnosis: one says "fix permissions
        # or move the workspace", the other "another server already holds it". Collapsing them
        # sends you hunting the wrong problem. The lock half is
        # test_duck.py::TestPersistence::test_locked_workspace_falls_back_to_ephemeral, which
        # needs a subprocess — two sessions in one process share DuckDB's cached instance and
        # never hit the cross-process lock at all.
        assert "locked by another" not in err

    def test_a_non_lock_io_error_is_not_diagnosed_as_contention(
        self, sqlite_file, tmp_path, capsys, monkeypatch
    ):
        """`duckdb.IOException` is not lock-specific — it also covers a corrupt or
        version-incompatible database file. Blaming "another server instance" for one of those
        sends you looking for a process that does not exist."""
        # The ephemeral fallback needs a working connect, so only the durable attempt is refused.
        real_connect = duckdb.connect
        seen: list[str] = []

        def connect_once_then_work(path, *args, **kwargs):
            seen.append(str(path))
            if len(seen) == 1:
                raise duckdb.IOException(
                    "Failed to deserialize: the file was written by a newer version of DuckDB"
                )
            return real_connect(path, *args, **kwargs)

        monkeypatch.setattr("spelunk.core.duck.duckdb.connect", connect_once_then_work)

        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            assert s.query("SELECT 1 AS a", "r")["sample"] == [[1]]
        finally:
            s.close()

        err = capsys.readouterr().err
        assert "could not be opened" in err, f"wrong diagnosis: {err!r}"
        assert "locked by another" not in err, "a deserialization failure is not lock contention"
        assert "newer version of DuckDB" in err, "the real cause must survive into the warning"


def _make_empty_workspace(parent: str, name: str, age_seconds: float) -> str:
    """An empty, unlocked workspace dir aged *age_seconds* — the shape reconnect churn leaves."""
    d = os.path.join(parent, name)
    os.makedirs(d)
    duckdb.connect(os.path.join(d, "workspace.duckdb")).close()
    stamp = time.time() - age_seconds
    os.utime(d, (stamp, stamp))
    return d


class TestSweepGraceWindow:
    """WSP-009: dirs younger than the 60s grace window are never touched — a sibling may be
    mid-startup with its lock not yet held. This is the ONLY protection during that race."""

    def test_age_decides_reclamation_all_else_equal(self, sqlite_file, tmp_path):
        parent = str(tmp_path / "root")
        os.makedirs(parent)
        inside = _make_empty_workspace(parent, "00000-young", age_seconds=5)
        outside = _make_empty_workspace(parent, "00000-old", age_seconds=600)

        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=parent, keep_workspaces=10)
        try:
            assert os.path.exists(inside), "a dir inside the grace window was reclaimed"
            assert not os.path.exists(outside), "a stale empty dir survived the sweep"
        finally:
            s.close()


class TestSweepDisabled:
    """WSP-010: --keep-workspaces 0 (or below) disables the sweep ENTIRELY, empty-dir
    reclamation included. The documented escape hatch for anyone who cannot afford deletion."""

    @pytest.mark.parametrize("keep", [0, -1])
    def test_nothing_is_reclaimed(self, sqlite_file, tmp_path, keep):
        parent = str(tmp_path / f"root{keep}")
        os.makedirs(parent)
        stale_empty = _make_empty_workspace(parent, "00000-stale", age_seconds=86_400)
        older_empty = _make_empty_workspace(parent, "00000-older", age_seconds=172_800)

        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=parent, keep_workspaces=keep)
        try:
            assert os.path.exists(stale_empty)
            assert os.path.exists(older_empty)
        finally:
            s.close()


class TestCheckpointSemantics:
    """WSP-012: the checkpoint is best-effort and targets the workspace catalog BY NAME."""

    def test_targets_the_workspace_catalog_not_attached_sources(self, sqlite_file, tmp_path):
        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        seen: list[str] = []
        real_con = s._con
        s._con = _ConnectionProxy(real_con, lambda sql: seen.append(str(sql)))
        try:
            s.query("SELECT 1 AS a", "r")
        finally:
            s._con = real_con
            s.close()

        checkpoints = [x for x in seen if x.strip().upper().startswith("CHECKPOINT")]
        assert checkpoints, "no checkpoint was issued"
        for stmt in checkpoints:
            assert f'"{s._catalog}"' in stmt, f"checkpoint is not catalog-scoped: {stmt!r}"
            assert "shop" not in stmt, "checkpoint reached a read-only attached source"

    def test_a_failing_checkpoint_never_fails_the_query(self, sqlite_file, tmp_path):
        """The rows are already durable in the WAL, so a no-op/abort must be swallowed."""
        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))

        def refuse_checkpoints(sql):
            if str(sql).strip().upper().startswith("CHECKPOINT"):
                raise duckdb.TransactionException("simulated concurrent reader on the WAL")

        real_con = s._con
        s._con = _ConnectionProxy(real_con, refuse_checkpoints)
        try:
            out = s.query("SELECT 42 AS a", "survivor")
            assert out["sample"] == [[42]]
        finally:
            s._con = real_con
        try:
            # The result is intact despite every checkpoint failing.
            assert s.query("SELECT a FROM survivor", "check")["sample"] == [[42]]
        finally:
            s.close()


class TestResourceGuards:
    """WSP-014: --memory-limit / --temp-dir / --max-temp-size are applied, not decorative."""

    def test_settings_reach_the_connection(self, sqlite_file, tmp_path):
        spill = tmp_path / "spill"
        s = DuckSession.open(
            [f"shop={sqlite_file}"],
            session_dir=str(tmp_path / "ws"),
            memory_limit="512MB",
            temp_dir=str(spill),
            max_temp_size="2GB",
        )
        try:
            assert _as_bytes(_setting(s, "memory_limit")) == pytest.approx(512e6, rel=0.05)
            assert os.path.abspath(_setting(s, "temp_directory")) == os.path.abspath(str(spill))
            assert _as_bytes(_setting(s, "max_temp_directory_size")) == pytest.approx(2e9, rel=0.05)
        finally:
            s.close()

    def test_default_spill_lives_inside_the_workspace(self, sqlite_file, tmp_path):
        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            temp_directory = os.path.abspath(_setting(s, "temp_directory"))
            assert temp_directory.startswith(os.path.abspath(s.workspace_dir))
        finally:
            s.close()


def _write_parquet(path, rows: int) -> tuple[str, int]:
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT i AS id, repeat('x', 32) || i AS payload FROM range({rows}) t(i)) "
        f"TO '{str(path).replace(chr(92), '/')}' (FORMAT PARQUET)"
    )
    con.close()
    return str(path), rows


def _parquet_fixture(tmp_path_factory, dirname: str, filename: str, rows: int):
    """Build a large Parquet file, then DELETE it when the module is done with it.

    tmp_path_factory alone would leave it behind: pytest retains the last three run
    directories, so ~290MB of fixture data would become ~870MB of resident garbage. The size
    itself is not negotiable — these tests mean something only while the data genuinely
    exceeds memory_limit (see TestOutOfCore) — but nothing needs it after the module ends.
    """
    path, count = _write_parquet(tmp_path_factory.mktemp(dirname) / filename, rows)
    yield path, count
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(scope="module")
def big_parquet(tmp_path_factory) -> tuple[str, int]:
    """8M rows x (BIGINT, 40-char VARCHAR) — ~72MB on disk, ~400MB to sort in memory."""
    yield from _parquet_fixture(tmp_path_factory, "bigdata", "big.parquet", 8_000_000)


@pytest.fixture(scope="module")
def huge_parquet(tmp_path_factory) -> tuple[str, int]:
    """24M rows — ~217MB on disk, ~1.2GB of uncompressed columns to stream through."""
    yield from _parquet_fixture(tmp_path_factory, "hugedata", "huge.parquet", 24_000_000)


class TestOutOfCore:
    """WSP-013: sources are read on demand with pushdown, and buffering operators spill to
    temp_directory — so a source larger than the memory limit is normal, not a failure."""

    def test_aggregate_streams_under_a_memory_limit_below_the_data(self, huge_parquet, tmp_path):
        """The pushdown/streaming half: 24M rows under a limit well below the data volume.

        512MB against ~217MB of Parquet holding ~1.2GB of uncompressed columns, so the scan is
        genuinely streaming rather than fitting. Calibrated over repeats with margin — at 8M
        rows the floor sat near 1:1 (384MB worked, 256MB OOMed), which is too close to the edge
        to be a stable assertion.
        """
        path, rows = huge_parquet
        s = DuckSession.open(
            [f"big={path}"], session_dir=str(tmp_path / "ws"), memory_limit="512MB"
        )
        try:
            out = s.query(
                "SELECT COUNT(*) AS n, SUM(id) AS total, MAX(LENGTH(payload)) AS widest FROM big",
                "agg",
            )
            # Exact, known by construction: completing is not enough, it must be right.
            assert out["sample"] == [[rows, rows * (rows - 1) // 2, len("x" * 32 + str(rows - 1))]]
        finally:
            s.close()

    def test_full_sort_out_of_core_completes(self, big_parquet, tmp_path):
        """The buffering half: materializing a fully sorted 8M-row result at 768MB."""
        path, rows = big_parquet
        s = DuckSession.open(
            [f"big={path}"], session_dir=str(tmp_path / "ws"), memory_limit="768MB"
        )
        try:
            out = s.query("SELECT id, payload FROM big ORDER BY payload", "sorted")
            assert out["row_count"] == rows
            first = s.query("SELECT payload FROM sorted LIMIT 1", "head")
            assert first["sample"][0][0].startswith("x" * 32)
        finally:
            s.close()

    def test_the_sort_really_spills_to_the_configured_temp_directory(self, big_parquet, tmp_path):
        """Proof the spill path is load-bearing, not that the data merely fit.

        The A/B: same 768MB memory limit, only max_temp_directory_size differs. Unbounded, the
        sort succeeds (see the test above); capped at 32MB it dies trying to OFFLOAD a block.
        Calibrated over 3 repeats each way, so the outcome is not a scheduling race.
        """
        path, _ = big_parquet
        spill = tmp_path / "spill"
        s = DuckSession.open(
            [f"big={path}"],
            session_dir=str(tmp_path / "ws"),
            memory_limit="768MB",
            temp_dir=str(spill),
            max_temp_size="32MB",
        )
        try:
            with pytest.raises(duckdb.Error) as excinfo:
                s.query("SELECT id, payload FROM big ORDER BY payload", "sorted")
            assert "offload" in str(excinfo.value).lower(), str(excinfo.value)
            assert os.path.isdir(spill), "the configured temp directory was never created"
        finally:
            s.close()


class TestInProcessLivenessBlindSpot:
    """WSP-016: liveness detection is CROSS-PROCESS only — two sessions in one process share
    DuckDB's cached instance, so the sweep's read-write probe cannot see an in-process holder.

    Pinning a documented limitation: if DuckDB's instance caching changes, this fails and the
    doc (and the sweep's safety argument) needs revisiting.
    """

    def test_the_probe_succeeds_against_a_live_in_process_workspace(self, sqlite_file, tmp_path):
        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            db = os.path.join(s.workspace_dir, "workspace.duckdb")
            # The sweep's liveness test IS this connect: cross-process it raises, in-process it
            # succeeds — which is exactly why the blind spot exists.
            duckdb.connect(db).close()
        finally:
            s.close()

    def test_a_separate_process_is_correctly_seen_as_live(self, sqlite_file, tmp_path):
        """The other half of the boundary: cross-process, the same probe must fail."""
        s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            db = os.path.join(s.workspace_dir, "workspace.duckdb")
            probe_code = textwrap.dedent(
                f"""
                import duckdb
                try:
                    duckdb.connect({db!r}).close()
                    print("OPENED")
                except duckdb.IOException:
                    print("LOCKED")
                """
            )
            out = subprocess.run(
                [sys.executable, "-c", probe_code], capture_output=True, text=True, timeout=120
            )
            assert out.stdout.strip() == "LOCKED", out.stdout + out.stderr
        finally:
            s.close()
