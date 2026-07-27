"""Safety invariants that are about what an attacker-shaped INPUT cannot do.

Claim SEC-002: "the server constructs the CREATE TABLE DDL itself, so agent SQL is SELECT-only."
Result and flow names are agent-supplied and end up in that DDL, so they are the one place
agent text reaches a non-guarded statement. The guard (SEC-001) does not look at them at all.

The contract asserted here deliberately does NOT presuppose HOW a hostile name is handled —
rejecting it and quoting it safely are both fine. What must hold either way is that nothing
outside the intended object changes: existing results survive, no extra objects appear, and
the reserved schema stays untouched.

Also covers SEC-006: a credentialed DSN must be redacted before it reaches the tool log.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from spelunk.core.duck import DuckSession
from spelunk.mcp.server import build_server

# Names that would be catastrophic if interpolated raw into DDL, plus the merely awkward ones
# an agent produces by accident (unicode, whitespace, absurd length).
HOSTILE_NAMES = [
    'x"; DROP TABLE keep; --',
    "x'); DROP TABLE keep; --",
    'keep" RENAME TO gone; --',
    "x; ATTACH 'evil.db' AS evil; --",
    'x" ; COPY (SELECT 1) TO \'leak.csv\' ; --',
    "../../etc/passwd",
    "main.keep",
    "_spelunk_meta.lineage",
    "keep" + " ",  # trailing whitespace
    "keep\nDROP TABLE keep",
    "a" * 300,
    "résultat",
    "",
    " ",
]

HOSTILE_FLOWS = [
    'f"; DROP SCHEMA "default" CASCADE; --',
    "_spelunk_meta",
    "main",
    "../escape",
    "f\nDROP SCHEMA x",
    "",
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def session(sqlite_file, tmp_path):
    s = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
    # The canary: a result that must survive every hostile input below.
    s.query('SELECT id, name FROM "shop"."customers" ORDER BY id', "keep")
    yield s
    s.close()


def _world(session) -> tuple[set, int]:
    """A snapshot of everything a hostile name might damage."""
    objects = set(
        session._con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables"
        ).fetchall()
    )
    lineage_rows = session._con.execute(
        "SELECT COUNT(*) FROM _spelunk_meta.lineage"
    ).fetchone()[0]
    return objects, lineage_rows


class TestHostileResultNames:
    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_no_collateral_damage(self, session, name):
        before_objects, before_lineage = _world(session)

        try:
            session.query("SELECT 1 AS n", name)
        except Exception:
            # Rejected outright — the world must be exactly as it was.
            after_objects, after_lineage = _world(session)
            assert after_objects == before_objects
            assert after_lineage == before_lineage
        else:
            # Accepted — then it must be ONE new table under that literal name, nothing else.
            after_objects, after_lineage = _world(session)
            created = after_objects - before_objects
            assert len(created) <= 1, f"{name!r} created more than one object: {created}"
            if created:
                (_, created_name), = created
                assert created_name == name, (
                    f"{name!r} was interpreted, not quoted: created {created_name!r}"
                )
            assert not (before_objects - after_objects), f"{name!r} destroyed an object"

        # The canary survives, with its rows, in every case.
        assert session._con.execute("SELECT COUNT(*) FROM \"default\".keep").fetchone()[0] == 3

    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_reserved_schema_is_never_written(self, session, name):
        before = session._con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = '_spelunk_meta'"
        ).fetchall()
        try:
            session.query("SELECT 1 AS n", name)
        except Exception:
            pass
        after = session._con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = '_spelunk_meta'"
        ).fetchall()
        assert after == before


class TestHostileFlowNames:
    @pytest.mark.parametrize("flow", HOSTILE_FLOWS)
    def test_no_collateral_damage(self, session, flow):
        before_objects, before_lineage = _world(session)
        try:
            session.query("SELECT 1 AS n", "probe", flow=flow)
        except Exception:
            after_objects, after_lineage = _world(session)
            assert after_objects == before_objects
            assert after_lineage == before_lineage
        else:
            after_objects, _ = _world(session)
            created = after_objects - before_objects
            # A new flow legitimately adds its schema's table; nothing may be destroyed.
            assert not (before_objects - after_objects), f"flow {flow!r} destroyed an object"
            assert all(t == "probe" for _, t in created), f"flow {flow!r} created {created}"

        assert session._con.execute("SELECT COUNT(*) FROM \"default\".keep").fetchone()[0] == 3

    def test_reserved_flow_cannot_be_written(self, session):
        with pytest.raises(Exception):
            session.query("SELECT 1 AS n", "sneak", flow="_spelunk_meta")
        tables = session._con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = '_spelunk_meta'"
        ).fetchall()
        assert ("lineage",) in tables and ("sneak",) not in tables


class TestCredentialRedactionInTheToolLog:
    """SEC-006: the tool log is a file that outlives the session — a DSN password must not
    reach it, on the success path or the failure path."""

    def test_dsn_password_is_masked(self, sqlite_file, tmp_path):
        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        server = build_server(session, tool_log=str(log_path), allow_add_source=True)
        secret = "hunter2supersecret"
        try:
            # Unreachable host: this fails, and the failure path is exactly where an unredacted
            # spec would be written out with the error.
            _run(server.call_tool(
                "add_source",
                {"spec": f"pg=postgresql://admin:{secret}@127.0.0.1:1/nowhere"},
            ))
        except Exception:
            pass
        finally:
            session.close()

        written = log_path.read_text(encoding="utf-8")
        assert written.strip(), "the failed call should still have been logged"
        assert secret not in written
        record = json.loads(written.splitlines()[-1])
        assert record["tool"] == "add_source"
        assert secret not in json.dumps(record)
        # Both shapes must be masked: the arg carries the URL form the caller wrote, while the
        # driver quotes the DSN back in libpq keyword form on the error.
        assert "//***@" in record["args"]["spec"]
        assert record["outcome"] == "error"
        assert "password=***" in record["error"]
