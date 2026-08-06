"""EXECUTE the app page's JavaScript, rather than asserting on it as a string.

Why this file exists: every other test treats `vega.app_html()` as text. Two real bugs shipped
through that gap — an outputSchema aimed at the wrong object, and a double-embed that killed any
spec carrying a `params` selection — because a string assertion cannot notice that the module
*misbehaves when driven*. Parsing (``node --check`` in test_visual.py) closed "it never ran".
This closes "it ran and did the wrong thing".

The harness stands in for the browser, not for Vega:

* a fake ``App`` replaces the inline ``window.SpelunkApp`` transport, so no postMessage plumbing
  is needed and the test can fire ``ontoolresult`` / ``onhostcontextchanged`` on demand (the real
  transport gets its own postMessage-driven tests below);
* a fake ``vegaEmbed`` records every call and hands back a view object with a ``finalize`` spy;
* a minimal ``window`` / ``document`` carry the handful of DOM calls the page makes.

That is enough to answer the questions that actually bite: how many views end up on the element,
whether the previous one was torn down first, and what the page does when the host delivers a
result with no ``structuredContent``. It deliberately does NOT check that Vega draws anything —
that needs a browser, and it is not where the bugs have been.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from spelunk.mcp import vega

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The fake transport, installed as `window.SpelunkApp` where the page looks for the real one.
# `connect()` resolves immediately; every handler the page assigns is captured so the scenario
# can fire it. `callServerTool` is scripted per-scenario via globalThis.
_STUB_APP = """
globalThis.window.SpelunkApp = class App {
  constructor(info, caps, opts) {
    globalThis.__appInstances = (globalThis.__appInstances || 0) + 1;
    this.info = info; this.opts = opts;
    globalThis.__app = this;
  }
  async connect() { globalThis.__connected = true; }
  getHostContext() { return globalThis.__hostContext; }
  sendSizeChanged() { globalThis.__sizeChanged = (globalThis.__sizeChanged || 0) + 1; }
  async callServerTool(params) {
    globalThis.__serverCalls = globalThis.__serverCalls || [];
    globalThis.__serverCalls.push(params);
    return globalThis.__callServerToolResult;
  }
};
"""

# Fake DOM + fake vegaEmbed. `__embeds` is the record everything is asserted against.
_PRELUDE = """
globalThis.__embeds = [];
globalThis.__finalized = 0;
globalThis.__hostContext = { theme: "light" };

const el = (id) => ({
  id, hidden: false, className: "", innerHTML: "", textContent: "",
  clientWidth: __CLIENT_WIDTH__,
  appendChild() {},
});
const nodes = { status: el("status"), chart: el("chart") };
globalThis.document = {
  getElementById: (id) => nodes[id],
  createElement: () => ({ className: "", textContent: "", appendChild() {} }),
};
globalThis.window = {
  addEventListener() {}, removeEventListener() {},
  get __spelunk() { return globalThis.__spelunk; },
  set __spelunk(v) { globalThis.__spelunk = v; },
  get __spelunkStatus() { return globalThis.__spelunkStatus; },
  set __spelunkStatus(v) { globalThis.__spelunkStatus = v; },
};
globalThis.vegaEmbed = async (element, spec, opts) => {
  globalThis.__embeds.push({
    spec: JSON.parse(JSON.stringify(spec)),
    opts: { ast: opts.ast },
    // Snapshot the teardown counter AS OF this embed: a correct implementation finalizes the
    // previous view BEFORE building the next, so embed N>0 must see N finalizes already done.
    finalizedBefore: globalThis.__finalized,
    liveViewsAtStart: globalThis.__embeds.filter((e) => !e.finalized).length,
  });
  const rec = globalThis.__embeds[globalThis.__embeds.length - 1];
  if (globalThis.__embedThrows) { throw new Error(globalThis.__embedThrows); }
  return { view: { finalize() { globalThis.__finalized += 1; rec.finalized = true; } } };
};
"""

SPEC_WITH_PARAMS = {
    "params": [{"name": "genreSel", "select": {"type": "point", "fields": ["genre"]},
                "bind": "legend"}],
    "mark": "line",
    "width": "container",
    "encoding": {"x": {"field": "year", "type": "ordinal"},
                 "y": {"field": "score", "type": "quantitative"}},
}


def _run_scenario(tmp_path, scenario: str, *, client_width: int = 800) -> dict:
    """Boot the real page scripts under the fakes, run *scenario*, return its JSON report."""
    boot = vega._BOOT_SCRIPT.replace("__APP_BUILD__", vega.app_build())

    runner = (
        _PRELUDE.replace("__CLIENT_WIDTH__", str(client_width))
        + _STUB_APP
        # Both page scripts are classic <script>s; run them the same way, for their side
        # effects. The app script reads `window.SpelunkApp`, which the stub above provides.
        + f"\nnew Function({json.dumps(boot)})();\n"
        + f"new Function({json.dumps(vega._APP_SCRIPT)})();\n"
        + "await new Promise((r) => setTimeout(r, 0));\n"  # let connect()'s .then settle
        + scenario
    )
    (tmp_path / "run.mjs").write_text(runner, encoding="utf-8")

    proc = subprocess.run(
        [shutil.which("node"), str(tmp_path / "run.mjs")],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


_REPORT = """
console.log(JSON.stringify({
  embeds: globalThis.__embeds.length,
  finalized: globalThis.__finalized,
  finalizedBefore: globalThis.__embeds.map((e) => e.finalizedBefore),
  widths: globalThis.__embeds.map((e) => e.spec.width),
  ast: globalThis.__embeds.map((e) => e.opts.ast),
  stage: globalThis.__spelunk.stage,
  build: globalThis.__spelunk.build,
  errors: globalThis.__spelunk.errors,
  status: document.getElementById("status").textContent,
  serverCalls: (globalThis.__serverCalls || []).map((c) => c.name),
  appInstances: globalThis.__appInstances,
  connected: !!globalThis.__connected,
}));
"""

SPEC_JSON = json.dumps(SPEC_WITH_PARAMS)


class TestTheModuleActuallyRuns:
    def test_connects_and_draws_one_view_for_one_result(self, tmp_path):
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        {_REPORT}
        """)
        assert report["connected"] is True
        assert report["appInstances"] == 1
        assert report["embeds"] == 1
        assert report["stage"] == "drawn"
        assert report["errors"] == []
        assert report["ast"] == [True], "the CSP-safe interpreter must be on for every embed"

    def test_theme_change_finalizes_before_re_embedding(self, tmp_path):
        """THE regression. Two views on one element collide on `<param>_tuple` signals.

        `finalizedBefore` is captured inside the fake embed, so it records teardown as of that
        call — the second embed must see exactly one finalize already done. Asserting only the
        totals would pass even if teardown ran after.
        """
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        globalThis.__hostContext = {{ theme: "dark" }};
        globalThis.__app.onhostcontextchanged();
        await globalThis.__drawing;
        await new Promise((r) => setTimeout(r, 50));
        {_REPORT}
        """)
        assert report["embeds"] == 2, "a theme flip should re-embed"
        assert report["finalizedBefore"] == [0, 1], (
            "the second embed must happen AFTER the first view was finalized; "
            f"got {report['finalizedBefore']}"
        )

    def test_a_non_theme_context_change_does_not_re_embed(self, tmp_path):
        """`onhostcontextchanged` also carries locale and display mode, and fires on connect."""
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        globalThis.__hostContext = {{ theme: "light", locale: "fr-FR" }};
        globalThis.__app.onhostcontextchanged();
        await new Promise((r) => setTimeout(r, 50));
        {_REPORT}
        """)
        assert report["embeds"] == 1

    def test_zero_width_container_falls_back_to_a_real_width(self, tmp_path):
        """`width: "container"` in an unsized frame draws a 0-wide, invisible chart."""
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        {_REPORT}
        """, client_width=0)
        assert report["widths"] == [600]

    def test_a_laid_out_container_keeps_container_width(self, tmp_path):
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        {_REPORT}
        """, client_width=800)
        assert report["widths"] == ["container"]


class TestMissingStructuredContent:
    """ext-apps#696 and the >150k sandbox diversion — identical from inside the view."""

    def test_re_fetches_through_the_tool_proxy_and_draws(self, tmp_path):
        report = _run_scenario(tmp_path, f"""
        globalThis.__callServerToolResult = {{ structuredContent: {{ spec: {SPEC_JSON} }} }};
        globalThis.__app.ontoolinput({{ arguments: {{ name: "r", spec: {{}} }} }});
        await globalThis.__app.ontoolresult({{ content: [] }});
        await new Promise((r) => setTimeout(r, 50));
        {_REPORT}
        """)
        assert report["serverCalls"] == ["visual"]
        assert report["embeds"] == 1
        assert report["stage"] == "drawn"

    def test_says_so_when_there_are_no_arguments_to_re_fetch_with(self, tmp_path):
        """No tool-input means no way to re-ask — the page must name that, not hang."""
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ content: [] }});
        await new Promise((r) => setTimeout(r, 50));
        {_REPORT}
        """)
        assert report["serverCalls"] == []
        assert "cannot be re-fetched" in report["status"]

    def test_does_not_loop_when_the_re_fetch_also_comes_back_empty(self, tmp_path):
        """A recovery that re-entered would hammer the server through the host proxy."""
        report = _run_scenario(tmp_path, f"""
        globalThis.__callServerToolResult = {{ content: [] }};
        globalThis.__app.ontoolinput({{ arguments: {{ name: "r" }} }});
        await globalThis.__app.ontoolresult({{ content: [] }});
        await globalThis.__app.ontoolresult({{ content: [] }});
        await new Promise((r) => setTimeout(r, 50));
        {_REPORT}
        """)
        assert report["serverCalls"] == ["visual"], "must re-fetch at most once"
        assert report["embeds"] == 0


class TestFailuresAreVisible:
    def test_an_embed_error_is_reported_with_its_message(self, tmp_path):
        """The duplicate-signal crash surfaced this way; the message must survive to the user."""
        report = _run_scenario(tmp_path, f"""
        globalThis.__embedThrows = 'Duplicate signal name: "genreSel_tuple"';
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        await new Promise((r) => setTimeout(r, 50));
        {_REPORT}
        """)
        assert "Duplicate signal name" in " ".join(report["errors"])
        assert report["stage"] == "drawing"

    def test_the_page_reports_the_build_it_is_running(self, tmp_path):
        """The stamp is what proves a host is running current code rather than a cached page."""
        report = _run_scenario(tmp_path, f"""
        await globalThis.__app.ontoolresult({{ structuredContent: {{ spec: {SPEC_JSON} }} }});
        {_REPORT}
        """)
        assert report["build"] == vega.app_build()


# --------------------------------------------------------------------- the real transport --- #
# Everything above stubs the transport to drive the page; these drive the TRANSPORT itself,
# with a fake `window.parent` capturing what it posts and `__deliver` playing the host. This is
# the layer that used to be the ext-apps SDK — now that it is ours, its handshake, its message
# routing, and above all its source-identity check need their own coverage.
_TRANSPORT_PRELUDE = """
const listeners = [];
globalThis.__posted = [];
globalThis.window = {
  parent: { postMessage(msg) { globalThis.__posted.push(msg); } },
  addEventListener(type, fn) { if (type === "message") listeners.push(fn); },
  removeEventListener(type, fn) {
    const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1);
  },
};
globalThis.document = { documentElement: { scrollWidth: 700, scrollHeight: 500 } };
// source defaults to window.parent (the host); pass anything else to spoof.
globalThis.__deliver = (msg, source) => {
  const ev = { data: msg, source: source === undefined ? globalThis.window.parent : source };
  for (const fn of [...listeners]) fn(ev);
};
"""


def _run_transport(tmp_path, scenario: str) -> dict:
    """Run the real transport script under the fake window, then the scenario."""
    transport = vega._TRANSPORT_SCRIPT.replace(
        "__UI_PROTOCOL_VERSION__", vega.UI_PROTOCOL_VERSION
    )
    runner = (
        _TRANSPORT_PRELUDE
        + f"new Function({json.dumps(transport)})();\n"
        + scenario
    )
    (tmp_path / "run.mjs").write_text(runner, encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), str(tmp_path / "run.mjs")],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60, check=False,
    )
    assert proc.returncode == 0, f"transport harness failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestTransportHandshake:
    """The `ui/initialize` → response → `ui/notifications/initialized` sequence, verified
    message by message — the exact dialect the develop-ui shim proved against Claude."""

    def test_connect_performs_the_documented_handshake(self, tmp_path):
        report = _run_transport(tmp_path, """
        const app = new window.SpelunkApp({ name: "t", version: "1" });
        const done = app.connect();
        const init = globalThis.__posted[0];
        globalThis.__deliver({ jsonrpc: "2.0", id: init.id,
          result: { hostContext: { theme: "dark" }, hostInfo: { name: "claude" } } });
        await done;
        console.log(JSON.stringify({
          init: { method: init.method, protocolVersion: init.params.protocolVersion,
                  app: init.params.appInfo.name },
          then: globalThis.__posted[1] || null,
          theme: app.getHostContext().theme,
        }));
        """)
        assert report["init"]["method"] == "ui/initialize"
        assert report["init"]["protocolVersion"] == vega.UI_PROTOCOL_VERSION
        assert report["then"]["method"] == "ui/notifications/initialized"
        assert report["theme"] == "dark"

    def test_notifications_route_to_their_handlers(self, tmp_path):
        report = _run_transport(tmp_path, """
        const app = new window.SpelunkApp({ name: "t", version: "1" });
        const got = { input: null, result: null, contextEvents: 0 };
        app.ontoolinput = (p) => { got.input = p; };
        app.ontoolresult = (p) => { got.result = p; };
        app.onhostcontextchanged = () => { got.contextEvents += 1; };
        globalThis.__deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-input",
                               params: { arguments: { name: "r" } } });
        globalThis.__deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                               params: { structuredContent: { spec: { mark: "bar" } } } });
        globalThis.__deliver({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed",
                               params: { theme: "dark" } });
        console.log(JSON.stringify({ ...got, theme: app.getHostContext().theme }));
        """)
        assert report["input"]["arguments"] == {"name": "r"}
        assert report["result"]["structuredContent"]["spec"] == {"mark": "bar"}
        assert report["contextEvents"] == 1
        assert report["theme"] == "dark", "a flat context partial must merge"

    def test_nested_host_context_partial_also_merges(self, tmp_path):
        """Some hosts nest the partial under `hostContext`; both shapes must land."""
        report = _run_transport(tmp_path, """
        const app = new window.SpelunkApp({ name: "t", version: "1" });
        globalThis.__deliver({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed",
                               params: { hostContext: { theme: "dark" } } });
        console.log(JSON.stringify({ theme: app.getHostContext().theme }));
        """)
        assert report["theme"] == "dark"

    def test_call_server_tool_correlates_request_and_response(self, tmp_path):
        report = _run_transport(tmp_path, """
        const app = new window.SpelunkApp({ name: "t", version: "1" });
        const pending = app.callServerTool({ name: "visual", arguments: { name: "r" } });
        const req = globalThis.__posted[0];
        globalThis.__deliver({ jsonrpc: "2.0", id: 999, result: { wrong: true } });
        globalThis.__deliver({ jsonrpc: "2.0", id: req.id,
                               result: { structuredContent: { spec: { mark: "bar" } } } });
        const result = await pending;
        console.log(JSON.stringify({
          method: req.method, name: req.params.name, args: req.params.arguments,
          spec: result.structuredContent.spec,
        }));
        """)
        assert report["method"] == "tools/call"
        assert report["name"] == "visual"
        assert report["spec"] == {"mark": "bar"}

    def test_messages_not_from_the_host_are_ignored(self, tmp_path):
        """THE security property. A sandboxed frame can be postMessage'd by anything; without
        the `ev.source !== window.parent` check, a hostile frame could feed the view a payload
        or answer its `tools/call` with its own `structuredContent`."""
        report = _run_transport(tmp_path, """
        const app = new window.SpelunkApp({ name: "t", version: "1" });
        let result = null, settled = false;
        app.ontoolresult = (p) => { result = p; };
        globalThis.__deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result",
                               params: { spoofed: true } }, { not: "the host" });
        const pending = app.callServerTool({ name: "visual", arguments: {} });
        pending.then(() => { settled = true; }, () => { settled = true; });
        globalThis.__deliver({ jsonrpc: "2.0", id: globalThis.__posted[0].id,
                               result: { spoofed: true } }, { not: "the host" });
        await new Promise((r) => setTimeout(r, 10));
        console.log(JSON.stringify({ result, settled }));
        process.exit(0);  // the ignored call's 30s no-response timer would hold node open
        """)
        assert report["result"] is None
        assert report["settled"] is False

    def test_a_jsonrpc_error_rejects_the_pending_request(self, tmp_path):
        report = _run_transport(tmp_path, """
        const app = new window.SpelunkApp({ name: "t", version: "1" });
        const pending = app.connect();
        globalThis.__deliver({ jsonrpc: "2.0", id: globalThis.__posted[0].id,
                               error: { code: -32600, message: "nope" } });
        const outcome = await pending.then(() => "resolved", (e) => e.message);
        console.log(JSON.stringify({ outcome }));
        """)
        assert report["outcome"] == "nope"
