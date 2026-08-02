"""The CLI entry point, driven as a real subprocess over both MCP transports.

`main()` is otherwise never executed by the suite: every other test builds a server in-process.
That leaves the whole startup path — argument parsing, source wiring, transport, tool-log
resolution, shutdown — unexercised.

Covers MCP-004 (log output NEVER reaches stdout, which is the protocol stream), MCP-009
(--source is repeatable, --dsn is an alias), MCP-001 (the registered tool surface matches the
documented one), the tool-log sink modes, and `--transport http` serving the same surface on a
bound port.

The stdio JSON-RPC client here is hand-rolled on purpose: it asserts what is actually on the
wire, which is the point of MCP-004. The HTTP test uses `fastmcp.Client` instead — there the
claim is "a real MCP client can connect to the URL", not "these bytes are on the pipe".
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

DOCUMENTED_TOOLS = {"query", "profile", "export", "catalog", "drop", "lineage", "replay"}
# `show` is registered only when the optional [ui] extra (prefab-ui) is installed. Decide that
# from whether prefab-ui is INSTALLED — deliberately NOT from views.PREFAB_AVAILABLE, which is
# the very flag the server consults: deriving the expectation from the implementation's own
# signal means a broken detection (prefab-ui present, import guard wrongly False) drops `show`
# from both sides at once and the assertion passes on a server that lost the tool. find_spec is
# an independent answer, so the two can disagree and the test says so. The subprocess under
# test runs this same interpreter, so what is importable here is importable there.
PREFAB_INSTALLED = importlib.util.find_spec("prefab_ui") is not None
if PREFAB_INSTALLED:
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

    @pytest.mark.skipif(not PREFAB_INSTALLED, reason="prefab-ui not installed (lean install)")
    def test_show_is_registered_when_prefab_is_installed(self, server):
        """The UI-enabled half, stated outright rather than folded into DOCUMENTED_TOOLS.

        Named directly so the [ui] expectation cannot be satisfied by the same guard that
        registers the tool: if prefab-ui is importable and `show` is still missing, that is a
        detection or registration failure and it must fail HERE, loudly.
        """
        assert "show" in _tool_names(server)

    @pytest.mark.skipif(PREFAB_INSTALLED, reason="prefab-ui is installed (ui/dev install)")
    def test_show_is_absent_without_prefab(self, server):
        """The lean-install half: no prefab-ui means no `show`, and no import error either."""
        assert "show" not in _tool_names(server)

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


def _free_port() -> int:
    """Bind port 0, read what the OS handed out, release it. Racy in principle; the window is
    microseconds and the alternative (a fixed port) collides with whatever else is running."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpServerProc:
    """`main() --transport http` as a subprocess, with both pipes drained.

    Same ~4KB Windows pipe-buffer trap as the stdio client: uvicorn logs to stderr on every
    request, so an undrained stderr would eventually block the server mid-response.
    """

    def __init__(self, args: list[str], cwd: Path, port: int, path: str = "/mcp"):
        self.port = port
        self.path = path
        self.url = f"http://127.0.0.1:{port}{path}"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "spelunk.mcp.server", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(cwd),
        )
        self.raw_stdout: list[str] = []
        self.raw_stderr: list[str] = []
        self._readers = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, self.raw_stdout), daemon=True),
            threading.Thread(target=self._pump, args=(self.proc.stderr, self.raw_stderr), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    @staticmethod
    def _pump(stream, sink: list[str]) -> None:
        for line in stream:
            sink.append(line)

    def wait_until_listening(self, timeout: float = STARTUP_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"server exited with {self.proc.returncode}; stderr: {self.stderr_tail()}"
                )
            with socket.socket() as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.2)
        raise TimeoutError(f"port {self.port} never opened; stderr: {self.stderr_tail()}")

    def stderr_tail(self, limit: int = 1200) -> str:
        return "".join(self.raw_stderr)[-limit:]

    def wait_for_stderr(self, needle: str, timeout: float = 30.0) -> None:
        """Block until `needle` appears on stderr.

        `wait_until_listening` only proves the port accepts connections; it says nothing about
        the pump THREAD having appended a line to `raw_stderr` yet. Asserting on the buffer
        straight after it is therefore a race — rare, but real, and a flake here reads as a
        broken announcement rather than a slow reader.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in "".join(self.raw_stderr):
                return
            time.sleep(0.05)
        raise AssertionError(
            f"{needle!r} never appeared on stderr within {timeout}s; tail: {self.stderr_tail()}"
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        for reader in self._readers:
            reader.join(timeout=5)


class TestHttpTransport:
    """`--transport http` serves the same tool surface over a bound port."""

    @staticmethod
    @contextlib.contextmanager
    def _serve(tmp_path, sources: list[str], extra: list[str] = (), path: str = "/mcp"):
        """Start the HTTP server, retrying if `_free_port`'s handout was taken in between.

        `_free_port` releases the port before the server binds it, so another process can win the
        race and the server dies with EADDRINUSE. The window is microseconds, but a CI flake here
        would look like a transport bug — so retry on a *startup* failure rather than pretend the
        race can't happen. A server that starts and then fails is a real failure and propagates.
        """
        last: Exception | None = None
        for _ in range(4):
            port = _free_port()
            server = HttpServerProc(
                ["--transport", "http", "--port", str(port),
                 *([] if path == "/mcp" else ["--http-path", path]),
                 *[arg for src in sources for arg in ("--source", src)],
                 "--session-dir", str(tmp_path / "session"), *extra],
                cwd=tmp_path, port=port, path=path,
            )
            try:
                server.wait_until_listening()
            except (RuntimeError, TimeoutError) as exc:
                server.close()
                if "in use" not in server.stderr_tail().lower() and "10048" not in server.stderr_tail():
                    raise
                last = exc
                continue
            try:
                yield server
            finally:
                server.close()
            return
        raise AssertionError(f"server never bound a free port: {last}")

    @pytest.fixture
    def http_server(self, tmp_path, sqlite_file, csv_file):
        with self._serve(tmp_path, [f"shop={sqlite_file}", f"orders={csv_file}"]) as server:
            yield server

    def test_same_surface_and_a_cross_source_query_over_http(self, http_server):
        from fastmcp import Client

        async def exercise() -> tuple[set[str], set[str], dict]:
            async with Client(http_server.url) as client:
                names = {t.name for t in await client.list_tools()}
                # Resource parity too: MCP-015 claims the TOOL AND RESOURCE surface matches, and
                # a tools-only assertion would let a transport serve half of it and still pass.
                resources = {str(r.uri) for r in await client.list_resources()}
                result = await client.call_tool(
                    "query",
                    {
                        "sql": 'SELECT c.name, o.amount FROM "shop"."customers" c '
                               "JOIN orders o ON c.id = o.customer_id ORDER BY o.amount",
                        "name": "joined",
                    },
                )
                return names, resources, json.loads(result.content[0].text)

        names, resources, payload = asyncio.run(exercise())
        assert names == DOCUMENTED_TOOLS
        assert any("tables" in uri for uri in resources), resources
        assert payload["row_count"] == 3
        assert payload["name"] == "joined"

    def test_bound_url_is_announced_on_stderr(self, http_server):
        # wait_for_stderr, not a bare assert: the port opening does not mean the pump thread has
        # appended the line yet.
        http_server.wait_for_stderr(f"http://127.0.0.1:{http_server.port}/mcp")

    def test_a_non_default_http_path_is_where_the_endpoint_lands(self, tmp_path, csv_file):
        """--http-path is forwarded to FastMCP, not just printed."""
        from fastmcp import Client

        with self._serve(tmp_path, [f"orders={csv_file}"], path="/spelunk/mcp") as server:
            server.wait_for_stderr(f":{server.port}/spelunk/mcp")

            async def exercise() -> int:
                async with Client(server.url) as client:
                    return len(await client.list_tools())

            assert asyncio.run(exercise()) == len(DOCUMENTED_TOOLS)

    def test_http_path_without_a_leading_slash_is_refused(self, tmp_path, csv_file):
        """Fail fast at parse time — the announced URL would otherwise read `...:8080mcp`."""
        proc = subprocess.run(
            [sys.executable, "-m", "spelunk.mcp.server", "--transport", "http",
             "--http-path", "mcp", "--source", f"orders={csv_file}"],
            capture_output=True, text=True, cwd=str(tmp_path), timeout=STARTUP_TIMEOUT,
        )
        assert proc.returncode != 0
        assert "--http-path must start with '/'" in proc.stderr


class TestEndpointAuthorities:
    """The allowed-authority set itself, unit-level.

    Bound at this level deliberately: `--host 127.0.0.2` is the interesting case and binding a
    non-.1 loopback address is not portable enough to assert on in a subprocess.
    """

    def test_the_bound_host_always_names_itself(self):
        # The regression: 127.0.0.0/8 is all loopback, so check_host is on, and a set built only
        # from the standard aliases would 403 a client whose Host is exactly what it dialled.
        from spelunk.mcp.server import _endpoint_authorities

        assert "127.0.0.2:8080" in _endpoint_authorities("127.0.0.2", 8080)

    def test_loopback_aliases_come_too(self):
        from spelunk.mcp.server import _endpoint_authorities

        allowed = _endpoint_authorities("127.0.0.1", 8080)
        assert {"127.0.0.1:8080", "localhost:8080", "[::1]:8080"} <= allowed

    def test_an_ipv6_bind_is_bracketed(self):
        from spelunk.mcp.server import _endpoint_authorities

        assert "[::1]:8080" in _endpoint_authorities("::1", 8080)

    def test_a_non_loopback_bind_names_only_itself_and_extras(self):
        from spelunk.mcp.server import _endpoint_authorities

        allowed = _endpoint_authorities("0.0.0.0", 8080, ["https://app.example"])
        assert "0.0.0.0:8080" in allowed
        assert "app.example" in allowed
        assert "localhost:8080" not in allowed


class TestHttpOriginGuard:
    """MCP-016: the HTTP transport refuses cross-origin and DNS-rebound requests.

    A loopback bind is not a boundary — any page the user visits can POST to 127.0.0.1, and
    rebinding lets it arrive under a hostname the attacker controls. FastMCP 3.4 ships no such
    guard, so these tests pin ours.
    """

    @staticmethod
    def _post(port: int, headers: dict[str, str], path: str = "/mcp") -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method="POST",
            data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        )
        req.add_header("content-type", "application/json")
        req.add_header("accept", "application/json, text/event-stream")
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    @pytest.fixture
    def guarded(self, tmp_path, csv_file):
        with TestHttpTransport._serve(tmp_path, [f"orders={csv_file}"]) as server:
            yield server

    @pytest.mark.parametrize("origin", ["http://evil.example", "null", "https://127.0.0.1:1"])
    def test_a_foreign_origin_is_refused(self, guarded, origin):
        assert self._post(guarded.port, {"Origin": origin}) == 403

    def test_a_rebound_host_is_refused(self, guarded):
        # The rebinding case proper: the browser resolved attacker.example to 127.0.0.1, so the
        # request reaches us carrying a Host we never bound.
        assert self._post(guarded.port, {"Host": "attacker.example"}) == 403

    def test_a_normal_client_sending_no_origin_is_untouched(self, guarded):
        # Not 403: it reaches the MCP layer, which answers on its own terms (400 for a raw ping
        # with no session). What matters is that the guard did not eat it.
        assert self._post(guarded.port, {}) != 403

    def test_the_endpoints_own_origin_is_allowed(self, guarded):
        assert self._post(guarded.port, {"Origin": f"http://127.0.0.1:{guarded.port}"}) != 403

    def test_allowed_origin_opens_a_named_extra(self, tmp_path, csv_file):
        with TestHttpTransport._serve(
            tmp_path, [f"orders={csv_file}"], extra=["--allowed-origin", "https://app.example"]
        ) as server:
            assert self._post(server.port, {"Origin": "https://app.example"}) != 403
            assert self._post(server.port, {"Origin": "https://other.example"}) == 403


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
