"""Credential containment and API confinement — invariants proved over many inputs.

Claims SEC-004 (specs carry env var NAMES, never values), SEC-005 (injected values scrubbed
from errors), SEC-013 (--env-file values are equally contained), and CONN-011 / SEC-010
(a fetch reaches only its own API).

Both halves were previously "proved" by single examples. The credential claim is about EVERY
output channel, so it is swept with one canary; the confinement claim is about EVERY path and
bound value, so it is asserted as a property of the resolved URL rather than by trusting that
each refusal message happened to be the right one.
"""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import pytest

from spelunk.core import apifetch
from spelunk.core.duck import DuckSession
from spelunk.mcp.server import _load_env_file, build_server

CANARY = "cAnAry-9f3b7e21-tOkEn-value"


def _run(coro):
    return asyncio.run(coro)


class TestCredentialNeverEscapesItsEnvVar:
    """The value behind auth_env=/header=/param= must not surface anywhere.

    Error paths are included deliberately: that is where scrubbing gets forgotten — SEC-006 was
    refuted in exactly that spot.
    """

    def _attach(self, session, api) -> None:
        session.add_source(f"canary=api:{api.base}/rows auth_env=CANARY_TOKEN")

    def test_no_output_channel_carries_the_value(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("CANARY_TOKEN", CANARY)
        api.handlers["/rows"] = lambda nth, query: (200, [{"id": 1}, {"id": 2}], {})

        log_path = tmp_path / "tool-calls.jsonl"
        session = DuckSession.open([], session_dir=str(tmp_path / "ws"))
        server = build_server(session, tool_log=str(log_path), allow_add_source=True)
        try:
            _run(server.call_tool(
                "add_source", {"spec": f"canary=api:{api.base}/rows auth_env=CANARY_TOKEN"}
            ))
            _run(server.call_tool("query", {"sql": "SELECT * FROM canary", "name": "rows"}))
            _run(server.call_tool("catalog", {}))
            _run(server.call_tool("lineage", {}))

            # The credential really did reach the wire — otherwise this test proves nothing.
            sent = [c["headers"].get("authorization", "") for c in api.calls]
            assert any(CANARY in header for header in sent), "the canary never authenticated anything"

            channels = {
                "source specs": json.dumps([vars(src) for src in session.sources], default=str),
                "list_objects": json.dumps(
                    [o.model_dump() for o in session.list_objects()], default=str
                ),
                "catalog": json.dumps(session.catalog(), default=str),
                "catalog(flow)": json.dumps(session.catalog("default"), default=str),
                "lineage": json.dumps(session.lineage(flow="default"), default=str),
                "describe": json.dumps(session.describe("canary").model_dump(), default=str),
            }
            for name, blob in channels.items():
                assert CANARY not in blob, f"credential leaked into {name}"
        finally:
            session.close()

        assert CANARY not in log_path.read_text(encoding="utf-8"), "credential leaked into the tool log"

        # And nowhere on disk — the snapshot files are written by the fetch itself.
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert CANARY.encode() not in path.read_bytes(), f"credential leaked into {path}"

    def test_not_even_when_the_api_rejects_it(self, api, tmp_path, monkeypatch):
        """The error path: a 401 makes many clients quote the whole request back."""
        monkeypatch.setenv("CANARY_TOKEN", CANARY)
        api.handlers["/rows"] = lambda nth, query: (401, {"error": "bad token"}, {})

        session = DuckSession.open([], session_dir=str(tmp_path / "ws"))
        try:
            with pytest.raises(Exception) as excinfo:
                self._attach(session, api)
            assert CANARY not in str(excinfo.value), "credential leaked into the failure message"
        finally:
            session.close()

    def test_a_query_param_credential_is_also_contained(self, api, tmp_path, monkeypatch):
        """param=<name>:<ENV> puts the secret in the URL — the easiest thing to echo back."""
        monkeypatch.setenv("CANARY_TOKEN", CANARY)
        api.handlers["/rows"] = lambda nth, query: (200, [{"id": 1}], {})

        session = DuckSession.open([], session_dir=str(tmp_path / "ws"))
        try:
            session.add_source(f"canary=api:{api.base}/rows param=api_key:CANARY_TOKEN")
            assert any(c["query"].get("api_key") == CANARY for c in api.calls)
            blob = json.dumps([vars(src) for src in session.sources], default=str)
            assert CANARY not in blob, "a URL-embedded credential leaked into the source info"
        finally:
            session.close()

    def test_env_file_values_are_contained_too(self, api, tmp_path, monkeypatch):
        """SEC-013: --env-file is the documented way credentials reach the server."""
        monkeypatch.delenv("CANARY_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(f"CANARY_TOKEN={CANARY}\n", encoding="utf-8")
        _load_env_file(str(env_file))
        assert os.environ["CANARY_TOKEN"] == CANARY

        api.handlers["/rows"] = lambda nth, query: (200, [{"id": 1}], {})
        session = DuckSession.open([], session_dir=str(tmp_path / "ws"))
        try:
            self._attach(session, api)
            blob = json.dumps([vars(src) for src in session.sources], default=str)
            assert CANARY not in blob
        finally:
            session.close()
            os.environ.pop("CANARY_TOKEN", None)


BASE_URL = "https://api.example.com/v3/base"

ESCAPE_ATTEMPTS = [
    # Absolute and scheme-relative
    "https://evil.com/steal",
    "http://evil.com/steal",
    "//evil.com/steal",
    "HTTPS://EVIL.COM/steal",
    "https:/evil.com/steal",
    # Traversal, plain and encoded
    "../../../etc/passwd",
    "..",
    "../secret",
    "a/../../secret",
    "%2e%2e/%2e%2e/secret",
    "%2E%2E%2Fsecret",
    "..%2fsecret",
    "....//secret",
    # Backslash and mixed separators
    "..\\..\\secret",
    "\\\\evil.com\\share",
    # Userinfo / fragment host confusion
    "https://api.example.com@evil.com/steal",
    "https://evil.com#api.example.com",
    # Whitespace and absolute-from-root
    " https://evil.com/steal",
    "\thttps://evil.com/x",
    "/absolute/from/root",
]

TEMPLATE_ESCAPES = [
    "../../secret",
    "..",
    "x/../../y",
    "https://evil.com/",
    "//evil.com",
    "a/b",
    "%2e%2e%2f",
    "?leak=1",
    "#frag",
    "\\other",
]


def _resolved_url(path: str, params: dict | None = None) -> str:
    conn = apifetch.ApiConnection(base_url=BASE_URL)
    spec, _ = apifetch.resolve_request(conn, apifetch.ApiRequest(path=path, params=params or {}))
    return spec.url


class TestFetchStaysOnItsOwnApi:
    """CONN-011 / SEC-010: a source reaches only its own API — host AND base path."""

    @pytest.mark.parametrize("path", ESCAPE_ATTEMPTS)
    def test_a_path_can_never_leave_the_base(self, path):
        try:
            url = _resolved_url(path)
        except (ValueError, KeyError):
            return  # refused outright — the other acceptable outcome
        split = urlsplit(url)
        assert split.netloc.lower() == "api.example.com", f"{path!r} escaped to {url}"
        assert split.scheme == "https", f"{path!r} downgraded the scheme: {url}"
        assert split.path.startswith("/v3/base"), f"{path!r} escaped the base path: {url}"

    @pytest.mark.parametrize("value", TEMPLATE_ESCAPES)
    def test_a_bound_placeholder_can_never_leave_the_base(self, value):
        """The subtler vector: the path is fine, the DATA bound into it is hostile."""
        try:
            url = _resolved_url("/movies/{movie_id}", {"movie_id": value})
        except (ValueError, KeyError):
            return
        split = urlsplit(url)
        assert split.netloc.lower() == "api.example.com", f"{value!r} escaped to {url}"
        assert split.path.startswith("/v3/base/movies/"), f"{value!r} escaped the base path: {url}"
        # Always exactly one segment: a bound value must not introduce another path level.
        tail = split.path[len("/v3/base/movies/"):]
        assert "/" not in tail, f"{value!r} became more than one path segment: {url}"

    def test_the_happy_path_still_resolves(self):
        """A confinement rule that refused everything would pass the two tests above."""
        assert _resolved_url("/movies", {"page": 2}).startswith(
            "https://api.example.com/v3/base/movies"
        )
        assert _resolved_url("/movies/{movie_id}", {"movie_id": 42}) == (
            "https://api.example.com/v3/base/movies/42"
        )
