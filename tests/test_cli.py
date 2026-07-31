"""The CLI entry point, driven as a real subprocess over the stdio MCP transport.

`main()` is otherwise never executed by the suite: every other test builds a server in-process.
That leaves the whole startup path — argument parsing, source wiring, transport, tool-log
resolution, shutdown — unexercised.

Covers MCP-004 (log output NEVER reaches stdout, which is the protocol stream), MCP-009
(--source is repeatable, --dsn is an alias), MCP-001 (the registered tool surface matches the
documented one), and the tool-log sink modes.

The JSON-RPC client here is hand-rolled on purpose: it asserts what is actually on the wire,
which is the point of MCP-004.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from spelunk.mcp import views

DOCUMENTED_TOOLS = {"query", "profile", "export", "catalog", "drop", "lineage", "replay"}
# `show` is registered only when the optional [ui] extra (prefab-ui) is installed. Fold it in
# conditionally rather than dropping the set-equality assertion: in a [ui] install a MISSING
# `show` must still fail here, and in a lean install an unexpected `show` must too. The
# subprocess under test runs the same interpreter, so this import sees the same answer it will.
if views.PREFAB_AVAILABLE:
    DOCUMENTED_TOOLS |= {"show"}
GATED_TOOLS = {"add_source", "remove_source", "fetch"}

STARTUP_TIMEOUT = 90.0  # cold DuckDB + extension load on Windows CI is not fast
CALL_TIMEOUT = 60.0


class StdioClient:
    """A minimal MCP stdio client: newline-delimited JSON-RPC over the child's stdin/stdout.

    Every stdout line is retained verbatim in ``self.raw_stdout`` so a test can assert the
    stream carries protocol and nothing else.
    """

    def __init__(self, args: list[str], cwd: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "spelunk.mcp.server", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(cwd),
        )
        self.raw_stdout: list[str] = []
        self.raw_stderr: list[str] = []
        self._lines: list[str] = []
        self._lock = threading.Condition()
        # BOTH streams must be drained concurrently. A Windows pipe buffer is ~4KB, and one
        # rich traceback on an error path fills it — the child then blocks on its stderr write
        # and never answers, which looks exactly like a server hang.
        self._readers = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        self._next_id = 0

    def _pump_stdout(self) -> None:
        for line in self.proc.stdout:
            with self._lock:
                self.raw_stdout.append(line)
                if line.strip():
                    self._lines.append(line.strip())
                self._lock.notify_all()

    def _pump_stderr(self) -> None:
        for line in self.proc.stderr:
            self.raw_stderr.append(line)

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _await_id(self, want: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            with self._lock:
                while seen < len(self._lines):
                    msg = json.loads(self._lines[seen])
                    seen += 1
                    if msg.get("id") == want:
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"no response to id={want} in {timeout}s; "
                        f"stderr tail: {self.stderr_tail()}"
                    )
                self._lock.wait(min(remaining, 0.5))

    def request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self._await_id(rid, timeout)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "spelunk-tests", "version": "0"},
            },
            timeout=STARTUP_TIMEOUT,
        )
        self.notify("notifications/initialized")
        return result

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def stderr_tail(self, limit: int = 800) -> str:
        return "".join(self.raw_stderr)[-limit:]

    def close(self) -> tuple[str, int]:
        """Close stdin (the documented clean-shutdown signal) and reap the child."""
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        for reader in self._readers:
            reader.join(timeout=5)
        return "".join(self.raw_stderr), self.proc.returncode


@pytest.fixture
def server(tmp_path, sqlite_file, csv_file):
    """The CLI, launched the documented way: two sources and a session dir."""
    session_dir = tmp_path / "session"
    client = StdioClient(
        [
            "--source", f"shop={sqlite_file}",
            "--source", f"orders={csv_file}",
            "--session-dir", str(session_dir),
        ],
        cwd=tmp_path,
    )
    client.session_dir = session_dir
    try:
        client.initialize()
        yield client
    finally:
        client.close()


def _tool_names(client: StdioClient) -> set[str]:
    return {t["name"] for t in client.request("tools/list")["result"]["tools"]}


class TestStdioTransport:
    def test_registered_surface_is_exactly_the_documented_one(self, server):
        """MCP-001 as set EQUALITY: an undocumented tool fails here too."""
        assert _tool_names(server) == DOCUMENTED_TOOLS

    def test_gated_tools_absent_without_the_flag(self, server):
        assert _tool_names(server) & GATED_TOOLS == set()

    def test_query_spans_both_sources_through_the_transport(self, server):
        resp = server.call_tool(
            "query",
            {
                "sql": 'SELECT c.name, o.amount FROM "shop"."customers" c '
                       "JOIN orders o ON c.id = o.customer_id ORDER BY o.amount",
                "name": "joined",
            },
        )
        assert "error" not in resp, resp
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["row_count"] == 3
        assert payload["name"] == "joined"

    def test_resources_are_served(self, server):
        resp = server.request("resources/list")
        assert any("tables" in str(r["uri"]) for r in resp["result"]["resources"])


class TestStdoutPurity:
    """MCP-004: nothing but JSON-RPC may reach stdout — stdout IS the transport."""

    def test_every_stdout_line_is_protocol(self, tmp_path, sqlite_file):
        client = StdioClient(
            ["--source", f"shop={sqlite_file}",
             "--session-dir", str(tmp_path / "s"),
             "--tool-log", "-"],  # logs to stderr, actively writing during the run
            cwd=tmp_path,
        )
        try:
            client.initialize()
            client.call_tool("query", {"sql": "SELECT 1 AS n", "name": "one"})
            client.call_tool("catalog", {})
            # A failing call: error paths are where stray prints hide.
            client.call_tool("query", {"sql": "DROP TABLE shop.customers", "name": "bad"})
        finally:
            stderr, _ = client.close()

        for line in client.raw_stdout:
            if not line.strip():
                continue
            msg = json.loads(line)  # a log line on stdout fails here
            assert msg.get("jsonrpc") == "2.0", f"non-protocol line on stdout: {line!r}"

        assert "tool" in stderr, "expected the tool log on stderr with --tool-log -"


class TestSourceArguments:
    def test_source_is_repeatable_and_dsn_is_an_alias(self, tmp_path, sqlite_file, csv_file, sample_db):
        """MCP-009: three sources arriving by two different flags."""
        client = StdioClient(
            ["--source", f"orders={csv_file}",
             "--source", f"shop={sqlite_file}",
             "--dsn", f"alias={sample_db}",
             "--session-dir", str(tmp_path / "s")],
            cwd=tmp_path,
        )
        try:
            client.initialize()
            resp = client.request("resources/read", {"uri": "db://tables"})
            listed = json.dumps(resp["result"])
            assert "orders" in listed
            assert "shop.customers" in listed
            assert "alias.customers" in listed
        finally:
            client.close()

    def test_allow_add_source_registers_the_gated_tools(self, tmp_path, sqlite_file):
        client = StdioClient(
            ["--source", f"shop={sqlite_file}",
             "--session-dir", str(tmp_path / "s"),
             "--allow-add-source"],
            cwd=tmp_path,
        )
        try:
            client.initialize()
            assert GATED_TOOLS <= _tool_names(client)
        finally:
            client.close()


class TestToolLogSink:
    def test_default_log_lands_in_the_per_process_workspace(self, server):
        server.call_tool("query", {"sql": "SELECT 1 AS n", "name": "one"})
        server.close()  # flush + release the handler before reading
        logs = list(server.session_dir.glob("*/tool-calls.jsonl"))
        assert len(logs) == 1, f"expected one per-process log, found {logs}"
        records = [json.loads(x) for x in logs[0].read_text().splitlines() if x.strip()]
        assert any(r["tool"] == "query" and r["outcome"] == "ok" for r in records)

    def test_off_writes_no_log(self, tmp_path, sqlite_file):
        session_dir = tmp_path / "s"
        client = StdioClient(
            ["--source", f"shop={sqlite_file}",
             "--session-dir", str(session_dir),
             "--tool-log", "off"],
            cwd=tmp_path,
        )
        try:
            client.initialize()
            client.call_tool("query", {"sql": "SELECT 1 AS n", "name": "one"})
        finally:
            client.close()
        assert list(session_dir.glob("*/tool-calls.jsonl")) == []

    def test_explicit_path_is_honoured(self, tmp_path, sqlite_file):
        target = tmp_path / "elsewhere" / "calls.jsonl"
        target.parent.mkdir(parents=True)
        client = StdioClient(
            ["--source", f"shop={sqlite_file}",
             "--session-dir", str(tmp_path / "s"),
             "--tool-log", str(target)],
            cwd=tmp_path,
        )
        try:
            client.initialize()
            client.call_tool("query", {"sql": "SELECT 1 AS n", "name": "one"})
        finally:
            client.close()
        assert target.exists() and target.read_text().strip()
