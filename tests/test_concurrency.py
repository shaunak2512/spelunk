"""Concurrency and cross-session isolation — the claims the multi-agent pitch rests on.

Claims QRY-014 (calls within a flow serialize; across flows they are parallel-safe),
SRC-017 (a source added at runtime is visible in EVERY flow, not scoped to one) and
SEC-011 (an add/remove touches only that session's connection — the reason
--allow-add-source is sound under process-per-agent).

Nothing exercised any of these before: the whole suite was single-threaded.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from spelunk.core.duck import DuckSession

FLOWS = [f"agent{i}" for i in range(4)]
STEPS = 6


@pytest.fixture
def session(sqlite_file, csv_file, tmp_path):
    s = DuckSession.open(
        [f"shop={sqlite_file}", f"orders={csv_file}"], session_dir=str(tmp_path / "ws")
    )
    yield s
    s.close()


class TestParallelFlows:
    """QRY-014: concurrent flows must not corrupt each other's results or lose lineage."""

    def test_concurrent_flows_keep_their_own_results(self, session):
        """Every flow builds a chain under the SAME result names, all at once."""

        def work(flow_index: int) -> None:
            flow = FLOWS[flow_index]
            for step in range(STEPS):
                # Same names in every flow — a namespace leak shows up as a wrong value.
                session.query(f"SELECT {flow_index} AS agent, {step} AS step", "head", flow=flow)
                session.query(
                    "SELECT agent, step, agent * 100 + step AS token FROM head", "derived",
                    flow=flow,
                )

        with ThreadPoolExecutor(max_workers=len(FLOWS)) as pool:
            list(pool.map(work, range(len(FLOWS))))

        for index, flow in enumerate(FLOWS):
            out = session.query(f'SELECT agent, step, token FROM "{flow}"."derived"', "check")
            agent, step, token = out["sample"][0]
            assert agent == index, f"{flow} saw another flow's rows"
            assert token == index * 100 + step

    def test_every_concurrent_result_gets_a_lineage_row(self, session):
        """The audit trail must survive contention — a dropped row is a hole in provenance."""

        def work(flow_index: int) -> None:
            flow = FLOWS[flow_index]
            session.query("SELECT 1 AS a", "base", flow=flow)
            for step in range(STEPS):
                session.query(f"SELECT a + {step} AS a FROM base", f"step{step}", flow=flow)

        with ThreadPoolExecutor(max_workers=len(FLOWS)) as pool:
            list(pool.map(work, range(len(FLOWS))))

        for flow in FLOWS:
            graph = session.lineage(flow=flow)
            names = {node["name"] for node in graph["nodes"]}
            assert names == {"base", *(f"step{i}" for i in range(STEPS))}
            assert graph["missing"] == []
            for node in graph["nodes"]:
                if node["name"] != "base":
                    # deps are (flow, name) pairs — a dep recorded against the WRONG flow is
                    # exactly the corruption contention could cause.
                    assert node["deps"] == [{"flow": flow, "name": "base"}]

    def test_reads_and_writes_interleave_without_deadlock(self, session):
        """Mixed traffic across tools: the single lock must not deadlock or starve."""
        errors: list[Exception] = []

        def hammer(index: int) -> None:
            flow = FLOWS[index % len(FLOWS)]
            try:
                for step in range(STEPS):
                    session.query(f"SELECT {step} AS n", f"r{index}", flow=flow)
                    session.catalog(flow)
                    session.profile(f'SELECT n FROM "{flow}"."r{index}"', flow)
                    session.lineage(flow=flow)
            except Exception as exc:  # noqa: BLE001 — the point is to surface any failure
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
            assert not thread.is_alive(), "a worker never finished — likely a deadlock"
        assert not errors, f"concurrent calls raised: {errors[:3]}"

    def test_connection_touches_never_overlap(self, session):
        """The safety property behind "one connection, one lock".

        Probed at the CONNECTION, not at _materialize_query — that method takes the lock
        internally, so instrumenting it would sample outside the critical section and see
        overlaps that are not real. A DuckDB connection is not safe for concurrent use, so two
        overlapping execute() calls would be the actual bug this claim rules out.
        """
        overlaps = {"n": 0}
        inside = {"n": 0}
        guard = threading.Lock()
        real_con = session._con

        class CountingConnection:
            def execute(self, sql, *args, **kwargs):
                with guard:
                    inside["n"] += 1
                    if inside["n"] > 1:
                        overlaps["n"] += 1
                try:
                    return real_con.execute(sql, *args, **kwargs)
                finally:
                    with guard:
                        inside["n"] -= 1

            def __getattr__(self, item):
                return getattr(real_con, item)

        session._con = CountingConnection()
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(
                    lambda i: session.query(
                        f"SELECT {i} AS a", f"n{i}", flow=FLOWS[i % len(FLOWS)]
                    ),
                    range(12),
                ))
        finally:
            session._con = real_con

        assert overlaps["n"] == 0, (
            f"{overlaps['n']} overlapping connection touches — calls are not serialized"
        )


class TestRuntimeSourceVisibility:
    """SRC-017: a source is connection-global — visible in every flow, not flow-scoped."""

    def test_a_source_added_while_working_in_one_flow_is_visible_from_another(
        self, session, parquet_file
    ):
        session.query("SELECT 1 AS a", "anchor", flow="first")
        session.add_source(f"regions={parquet_file}")
        try:
            # Queried from a DIFFERENT flow than the one that was active when it was added.
            out = session.query("SELECT COUNT(*) AS n FROM regions", "seen", flow="second")
            assert out["sample"] == [[2]]
            # And from a flow that did not exist at add time.
            later = session.query("SELECT state FROM regions ORDER BY state", "later", flow="third")
            assert later["sample"] == [["NSW"], ["VIC"]]
        finally:
            session.remove_source("regions")

    def test_removal_is_equally_global(self, session, parquet_file):
        session.add_source(f"regions={parquet_file}")
        session.query("SELECT COUNT(*) AS n FROM regions", "before", flow="first")
        session.remove_source("regions")
        with pytest.raises(Exception):
            session.query("SELECT COUNT(*) AS n FROM regions", "after", flow="second")


class TestSessionIsolation:
    """SEC-011: add/remove touches only its own session — the process-per-agent argument.

    This is the load-bearing half of the --allow-add-source security note: the flag is sound
    *because* one agent's server is its own process and its own connection.
    """

    def test_a_source_added_in_one_session_is_invisible_in_another(
        self, sqlite_file, parquet_file, tmp_path
    ):
        a = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "a"))
        b = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "b"))
        try:
            a.add_source(f"regions={parquet_file}")

            assert "regions" in {s.name for s in a.sources}
            assert "regions" not in {s.name for s in b.sources}
            assert not any(o.name.startswith("regions") for o in b.list_objects())
            with pytest.raises(Exception):
                b.query("SELECT * FROM regions", "leak")
        finally:
            a.close()
            b.close()

    def test_results_do_not_leak_between_sessions(self, sqlite_file, tmp_path):
        a = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "a"))
        b = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "b"))
        try:
            a.query("SELECT 'secret' AS v", "private", flow="work")
            assert b.catalog()["flows"] == [] or "work" not in {
                f["flow"] for f in b.catalog()["flows"]
            }
            with pytest.raises(Exception):
                b.query('SELECT v FROM "work"."private"', "leak")
        finally:
            a.close()
            b.close()
