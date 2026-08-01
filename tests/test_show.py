"""Tests for the `show` tool and the Prefab view layer.

Three layers, deliberately separate:

* ``TestViewBuilders`` drives ``spelunk.mcp.views`` with plain dicts — no session, no client.
* ``TestRowsForDisplay`` drives ``DuckSession.rows_for_display`` — the read + cap behaviour.
* The rest drive the registered tool through an in-process FastMCP instance.

The whole module skips when the optional ``[ui]`` extra (``prefab-ui``) is absent, since the
tool is not registered at all in that install.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from spelunk.core.duck import DuckSession
from spelunk.mcp import views
from spelunk.mcp.server import build_server

pytestmark = pytest.mark.skipif(
    not views.PREFAB_AVAILABLE, reason="needs the [ui] extra (prefab-ui)"
)


def _run(coro):
    return asyncio.run(coro)


def _types(obj, acc=None):
    """Every ``type`` discriminator in a Prefab payload, in document order."""
    acc = [] if acc is None else acc
    if isinstance(obj, dict):
        if isinstance(obj.get("type"), str):
            acc.append(obj["type"])
        for value in obj.values():
            _types(value, acc)
    elif isinstance(obj, list):
        for value in obj:
            _types(value, acc)
    return acc


def _walk(obj):
    """Every dict node in a Prefab payload."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


COLUMNS = [
    {"name": "region", "type": "VARCHAR"},
    {"name": "revenue", "type": "BIGINT"},
    {"name": "orders", "type": "INTEGER"},
]
ROWS = [
    {"region": "AU", "revenue": 120, "orders": 9},
    {"region": "NZ", "revenue": 80, "orders": 4},
]


@pytest.fixture
def show_server(tmp_path):
    """A session over a small CSV, with one aggregated result already saved."""
    csv = tmp_path / "sales.csv"
    csv.write_text("region,revenue,orders\nAU,120,9\nNZ,80,4\nUK,200,15\n")
    session = DuckSession.open([f"sales={csv}"], session_dir=None)
    session.query(
        "SELECT region, sum(revenue) AS revenue, sum(orders) AS orders "
        "FROM sales GROUP BY region ORDER BY revenue DESC",
        "by_region",
    )
    server = build_server(session)
    yield server, session
    session.close()


def _call(server, args):
    return _run(server.call_tool("show", args))


def _summary(result) -> dict:
    """The JSON text half of a `show` result — what the model actually reads."""
    return json.loads(result.content[0].text)


# ------------------------------------------------------------------------- pure view builders --- #
class TestViewBuilders:
    def test_default_axes_pick_label_and_first_numeric(self):
        assert views.choose_axes(COLUMNS) == ("region", ["revenue"])

    def test_explicit_series_wins_over_y(self):
        x, measures = views.choose_axes(COLUMNS, x="region", y="orders", series=["revenue"])
        assert (x, measures) == ("region", ["revenue"])

    def test_unknown_axis_column_errors_naming_the_real_ones(self):
        with pytest.raises(ValueError, match="Available:.*region"):
            views.choose_axes(COLUMNS, x="nope")

    def test_all_numeric_result_still_charts(self):
        cols = [{"name": "a", "type": "BIGINT"}, {"name": "b", "type": "BIGINT"}]
        assert views.choose_axes(cols) == ("a", ["b"])

    def test_no_numeric_column_errors_with_a_way_out(self):
        cols = [{"name": "a", "type": "VARCHAR"}, {"name": "b", "type": "VARCHAR"}]
        with pytest.raises(ValueError, match="kind='table'"):
            views.choose_axes(cols)

    @pytest.mark.parametrize(
        "kind,expected",
        [("bar", "BarChart"), ("line", "LineChart"), ("area", "AreaChart"),
         ("scatter", "ScatterChart"), ("pie", "PieChart")],
    )
    def test_each_chart_kind_builds_its_component(self, kind, expected):
        payload = views.to_payload(views.result_chart(kind, COLUMNS, ROWS))
        assert expected in _types(payload)

    def test_unknown_chart_kind_errors(self):
        with pytest.raises(ValueError, match="Unknown chart kind"):
            views.result_chart("donut", COLUMNS, ROWS)

    def test_title_wraps_the_component_without_dropping_it(self):
        """A component built outside its container attaches to nothing and vanishes silently —
        the title branch must construct inside the context, so assert BOTH parts survive."""
        types = _types(views.to_payload(views.result_table(COLUMNS, ROWS, title="Sales")))
        assert "H3" in types and "DataTable" in types

    def test_titled_chart_keeps_both_parts(self):
        types = _types(views.to_payload(views.result_chart("bar", COLUMNS, ROWS, title="Sales")))
        assert "H3" in types and "BarChart" in types

    def test_interactive_chart_adds_a_picker_and_one_branch_per_measure(self):
        payload = views.to_payload(
            views.interactive_chart("bar", COLUMNS, ROWS, series=["revenue", "orders"])
        )
        types = _types(payload)
        assert "Select" in types
        assert types.count("SelectOption") == 2
        assert types.count("BarChart") == 2  # one per branch of the Condition
        assert "Condition" in types

    def test_interactive_chart_holds_the_rows_once_in_state(self):
        """Each branch references {{ rows }} instead of embedding its own copy — otherwise the
        payload multiplies by the number of measures for no benefit."""
        payload = views.to_payload(
            views.interactive_chart("bar", COLUMNS, ROWS, series=["revenue", "orders"])
        )
        assert payload["state"]["rows"] == ROWS
        charts = [n for n in _walk(payload) if n.get("type") == "BarChart"]
        assert charts and all(c["data"] == "{{ rows }}" for c in charts)

    def test_interactive_with_one_measure_falls_back_to_a_plain_chart(self):
        """A picker with a single option is furniture, not a control."""
        types = _types(views.to_payload(views.interactive_chart("bar", COLUMNS, ROWS)))
        assert "Select" not in types and types.count("BarChart") == 1

    def test_profile_splits_numeric_and_text_columns(self):
        profile = {
            "row_count": 1000,
            "elapsed_s": 0.03,
            "columns": {
                "revenue": {"null_rate": 0.01, "min": 1, "max": 99, "mean": 50.0,
                            "std": 3.0, "p50": 50, "p95": 95},
                "region": {"null_rate": 0.0, "unique": 4, "top": "AU", "freq": 400},
            },
        }
        types = _types(views.to_payload(views.profile_view(profile, "sales")))
        assert types.count("DataTable") == 2  # one numeric table, one text table
        assert types.count("Metric") == 5

    def test_catalog_flow_list_and_single_flow_both_render(self):
        flows = views.to_payload(views.catalog_view({"flows": [{"flow": "a", "result_count": 2}]}))
        assert "DataTable" in _types(flows)
        one = views.to_payload(views.catalog_view({
            "flow": "a",
            "results": [{"name": "r", "row_count": 3, "columns": COLUMNS, "description": None}],
        }))
        assert "DataTable" in _types(one)

    def test_lineage_view_renders_the_diagram(self):
        payload = views.to_payload(views.lineage_view({
            "nodes": [{"flow": "d", "name": "a", "kind": "query", "description": None,
                       "deps": [], "sources": []}],
            "edges": [], "missing": [], "mermaid": "flowchart TD\n    n0[\"a\"]",
        }, "d"))
        assert "Mermaid" in _types(payload)


# ---------------------------------------------------------------------------- rows_for_display --- #
class TestRowsForDisplay:
    def test_returns_columns_and_row_dicts(self, show_server):
        _, session = show_server
        columns, rows = session.rows_for_display("by_region")
        assert [c["name"] for c in columns] == ["region", "revenue", "orders"]
        # Compare order-independently: `rows_for_display` scans the table with no ORDER BY, so
        # the fixture's materialization order is not a promise DuckDB makes on read-back.
        by_region = {r["region"]: r for r in rows}
        assert by_region == {
            "UK": {"region": "UK", "revenue": 200, "orders": 15},
            "AU": {"region": "AU", "revenue": 120, "orders": 9},
            "NZ": {"region": "NZ", "revenue": 80, "orders": 4},
        }

    def test_unknown_result_names_what_the_flow_holds(self, show_server):
        _, session = show_server
        with pytest.raises(ValueError, match="by_region"):
            session.rows_for_display("nope")

    def test_refuses_past_the_row_cap_instead_of_truncating(self, show_server):
        _, session = show_server
        session.query("SELECT i FROM range(500) t(i)", "big")
        with pytest.raises(ValueError, match="too large to display") as exc:
            session.rows_for_display("big", max_rows=100)
        message = str(exc.value)
        assert "500 rows" in message  # the real count, so the agent can size the aggregation
        assert "Nothing was truncated" in message
        assert "GROUP BY" in message  # and the way out

    def test_cell_cap_catches_a_wide_result_the_row_cap_misses(self, show_server):
        _, session = show_server
        session.query(
            "SELECT i, i AS b, i AS c, i AS d FROM range(50) t(i)", "wide"
        )
        session.rows_for_display("wide", max_rows=100, max_cells=1000)  # 200 cells: fine
        with pytest.raises(ValueError, match="cells"):
            session.rows_for_display("wide", max_rows=100, max_cells=100)

    def test_reads_without_creating_anything(self, show_server):
        _, session = show_server
        before = session.catalog("default")["results"]
        session.rows_for_display("by_region")
        assert session.catalog("default")["results"] == before


# --------------------------------------------------------------------------------- the tool --- #
class TestRegistration:
    def test_show_is_registered_with_ui_metadata(self, show_server):
        server, _ = show_server
        tool = next(t for t in _run(server.list_tools()) if t.name == "show")
        assert tool.meta["ui"]["resourceUri"].startswith("ui://")

    def test_renderer_resource_is_readable(self, show_server):
        server, _ = show_server
        uris = [str(r.uri) for r in _run(server.list_resources()) if str(r.uri).startswith("ui://")]
        assert uris, "no ui:// renderer resource was synthesized"
        body = _run(server.read_resource(uris[0]))
        assert "<!doctype html>" in body.contents[0].content.lower()

    def test_absent_extra_means_no_show_tool(self, show_server, monkeypatch):
        """Without prefab-ui the tool must not appear at all — a `show` that always errors is
        worse than a server that never offers it."""
        _, session = show_server
        monkeypatch.setattr(views, "PREFAB_AVAILABLE", False)
        names = {t.name for t in _run(build_server(session).list_tools())}
        assert "show" not in names
        assert "query" in names  # the rest of the surface is unaffected


class TestRendererResource:
    """Spelunk serves its own renderer so the ext-apps#696 recovery shim ships with the view."""

    def test_show_points_at_spelunks_renderer(self, show_server):
        server, _ = show_server
        tool = next(t for t in _run(server.list_tools()) if t.name == "show")
        assert tool.meta["ui"]["resourceUri"] == views.RENDERER_URI

    def test_the_RESOURCE_declares_the_prefab_cdn_in_its_csp(self, show_server):
        """The host reads the CSP from the RESOURCE, not the tool — FastMCP's own synthesized
        renderer puts it there and the tool carries only resourceUri.

        Getting this wrong is invisible from the server: the tool looks correctly configured, the
        resource is served, and the app frame is simply blank because the host blocked the
        renderer bundle. Assert the layer the host actually reads.
        """
        server, _ = show_server
        resource = next(
            r for r in _run(server.list_resources()) if str(r.uri) == views.RENDERER_URI
        )
        domains = resource.meta["ui"]["csp"]["resourceDomains"]
        assert any("jsdelivr" in d for d in domains)

    def test_static_fallback_survives_a_blocked_bundle(self, show_server):
        """If the renderer never loads, the frame must explain itself rather than go black.
        Plain HTML inside #root, replaced when the renderer mounts."""
        server, _ = show_server
        html = _run(server.read_resource(views.RENDERER_URI)).contents[0].content
        assert '<div id="root"></div>' not in html  # the empty div was replaced
        assert "cdn.jsdelivr.net" in html and "did not load" in html

    def test_renderer_carries_a_bootstrap_view(self, show_server):
        """A view baked into the page renders immediately, so a host that never delivers
        structuredContent shows a loading state rather than a black box."""
        server, _ = show_server
        html = _run(server.read_resource(views.RENDERER_URI)).contents[0].content
        assert 'id="prefab:initial-data"' in html
        start = html.index('id="prefab:initial-data"')
        payload = html[html.index(">", start) + 1: html.index("</script>", start)]
        assert json.loads(payload)["view"]  # parses, and actually has a view to render

    def test_recovery_shim_refetches_through_the_host_proxy(self, show_server):
        server, _ = show_server
        html = _run(server.read_resource(views.RENDERER_URI)).contents[0].content
        assert "ui/notifications/tool-input" in html  # captures the args
        assert '"tools/call"' in html or "'tools/call'" in html  # re-calls via the proxy
        assert "window.parent" in html  # and re-delivers with a source Prefab accepts

    def test_recovery_is_a_no_op_on_a_healthy_host(self, show_server):
        """The shim must never fire where structuredContent arrives intact."""
        server, _ = show_server
        html = _run(server.read_resource(views.RENDERER_URI)).contents[0].content
        assert "p.structuredContent || p.isError" in html

    def test_shim_runs_before_the_renderer_registers_its_listener(self, show_server):
        """Load-bearing ordering: the shim wraps addEventListener to capture Prefab's listener,
        so it must run FIRST. It does because Prefab's renderer is a `type="module"` script
        (deferred until after parsing) while the shim is a classic inline script that executes
        during parsing — regardless of which tag appears first in the document."""
        server, _ = show_server
        html = _run(server.read_resource(views.RENDERER_URI)).contents[0].content
        assert 'type="module"' in html          # the renderer: deferred
        assert "<script>\n(function ()" in html  # the shim: classic, runs during parsing

    def test_renderer_is_still_prefabs_own_page(self, show_server):
        """Built from prefab-ui's HTML, not hand-written — only our two additions are ours."""
        server, _ = show_server
        html = _run(server.read_resource(views.RENDERER_URI)).contents[0].content
        assert "renderer.js" in html and '<div id="root">' in html


_SHIM_HARNESS = r"""
// Minimal stand-ins for the browser globals the shim touches, so its LOGIC can be executed
// outside a browser. What this cannot prove is the one browser-native step — whether the real
// Prefab transport's listener, reached through a real page, accepts the plain event object the
// shim hands it. Everything up to and including that call is exercised for real.
const listeners = [], posted = [], delivered = [];
const parentWindow = { postMessage: (m) => posted.push(m) };
globalThis.window = {
  parent: parentWindow,
  location: { origin: 'https://app.local' },
  addEventListener: (t, fn) => { if (t === 'message') listeners.push(fn); },
  removeEventListener: (t, fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
};

__SHIM__

// Stand in for Prefab's transport: registered AFTER the shim (as the deferred renderer module
// is), and applying the same source check the real one does — an identity comparison against
// window.parent. Anything it accepts here, Prefab accepts.
window.addEventListener('message', function prefabListener(ev) {
  if (ev.source !== window.parent) return;         // the real transport's only guard
  const d = ev.data;
  if (d && d.method === 'ui/notifications/tool-result') { delivered.push(d.params); }
});

function send(data) { for (const fn of [...listeners]) fn({ data, source: parentWindow, origin: 'https://host' }); }

const INIT = { jsonrpc: '2.0', id: 1, result: { hostContext: { toolInfo: { tool: { name: 'show' } } } } };
const INPUT = { jsonrpc: '2.0', method: 'ui/notifications/tool-input',
                params: { arguments: { name: 'by_region', kind: 'bar' } } };
const STRIPPED = { jsonrpc: '2.0', method: 'ui/notifications/tool-result',
                   params: { content: [{ type: 'text', text: '{"row_count":3}' }], isError: false } };

const scenario = process.argv[2];
if (scenario === 'healthy') {
  send(INIT); send(INPUT);
  send({ jsonrpc: '2.0', method: 'ui/notifications/tool-result',
         params: { content: [], structuredContent: { view: { type: 'Div' } }, isError: false } });
} else if (scenario === 'error') {
  send(INIT); send(INPUT);
  send({ jsonrpc: '2.0', method: 'ui/notifications/tool-result',
         params: { content: [], isError: true } });
} else if (scenario === 'stripped') {
  send(INIT); send(INPUT); send(STRIPPED);
  const req = posted[0];
  if (req) {
    send({ jsonrpc: '2.0', id: req.id,
           result: { content: [], structuredContent: { $prefab: { version: '0.3' },
                                                       view: { type: 'BarChart' } } } });
  }
} else if (scenario === 'twice') {
  send(INIT); send(INPUT); send(STRIPPED); send(STRIPPED);
} else if (scenario === 'spoofed') {
  // A frame that is NOT the host answers the recovery call first, with the same guessable id.
  send(INIT); send(INPUT); send(STRIPPED);
  const req = posted[0];
  const attacker = { postMessage: () => {} };
  for (const fn of [...listeners]) {
    fn({ data: { jsonrpc: '2.0', id: req.id,
                 result: { structuredContent: { view: { type: 'Evil' } } } },
         source: attacker, origin: 'https://evil.local' });
  }
}

console.log(JSON.stringify({ posted, delivered }));
"""


def _run_shim(tmp_path, renderer_html: str, scenario: str) -> dict:
    """Execute the shipped shim in Node under a stubbed browser and return what it did."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("needs node to execute the renderer shim")
    # The LAST bare <script> block is ours: `recovery_renderer_html` appends the shim to Prefab's
    # own page, so a bare tag Prefab happens to emit (today it does not, but that is a version
    # away) would otherwise be executed in its place — silently testing the wrong code.
    blocks = re.findall(r"<script>(.*?)</script>", renderer_html, re.S)
    assert blocks, "no bare <script> block in the renderer HTML — the shim is not being embedded"
    js = blocks[-1]
    harness = tmp_path / "harness.mjs"
    harness.write_text(_SHIM_HARNESS.replace("__SHIM__", js), encoding="utf-8")
    out = subprocess.run(
        [node, str(harness), scenario], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class TestRecoveryShimBehaviour:
    """Execute the shim itself, rather than asserting on the source text of the page."""

    @pytest.fixture
    def html(self, show_server):
        server, _ = show_server
        return _run(server.read_resource(views.RENDERER_URI)).contents[0].content

    def test_healthy_host_triggers_nothing(self, tmp_path, html):
        result = _run_shim(tmp_path, html, "healthy")
        assert result["posted"] == []  # no re-call
        # The renderer still gets exactly what the host sent — one result, already complete.
        assert len(result["delivered"]) == 1
        assert result["delivered"][0]["structuredContent"] == {"view": {"type": "Div"}}

    def test_error_result_is_left_alone(self, tmp_path, html):
        """An errored tool must not be silently re-run."""
        assert _run_shim(tmp_path, html, "error")["posted"] == []

    def test_stripped_result_refetches_with_the_captured_args(self, tmp_path, html):
        result = _run_shim(tmp_path, html, "stripped")
        assert len(result["posted"]) == 1
        request = result["posted"][0]
        assert request["method"] == "tools/call"
        assert request["params"]["name"] == "show"
        # the args from tool-input, not a guess
        assert request["params"]["arguments"] == {"name": "by_region", "kind": "bar"}

    def test_recovered_payload_reaches_the_renderer(self, tmp_path, html):
        """End to end through a stand-in transport applying Prefab's real source check: the
        payload the renderer would draw from actually arrives."""
        result = _run_shim(tmp_path, html, "stripped")
        # Two results reach the renderer: the host's stripped one (which it ignores, having no
        # structuredContent to draw), then ours carrying the recovered payload.
        assert len(result["delivered"]) == 2
        assert "structuredContent" not in result["delivered"][0]
        recovered = result["delivered"][1]
        assert recovered["structuredContent"]["view"]["type"] == "BarChart"
        assert recovered["isError"] is False

    def test_recovery_runs_at_most_once(self, tmp_path, html):
        """Two stripped results must not produce two re-calls."""
        assert len(_run_shim(tmp_path, html, "twice")["posted"]) == 1

    def test_a_response_from_another_frame_is_ignored(self, tmp_path, html):
        """The recovery id is fixed and guessable, so only the host may answer with it.

        Without the `ev.source === window.parent` check, any frame able to postMessage into the
        view could hand the renderer a payload of its choosing and have it drawn as the result.
        """
        result = _run_shim(tmp_path, html, "spoofed")
        assert len(result["posted"]) == 1  # the recovery call still went out
        # Only the host's own stripped result reached the renderer; the spoofed one did not.
        assert [d.get("structuredContent") for d in result["delivered"]] == [None]


class TestOutputSchema:
    def test_show_declares_one(self, show_server):
        # server.list_tools() yields FastMCP's own tool objects (`output_schema`); the
        # MCP wire form (`outputSchema`) is what a client sees. Assert on both spellings'
        # single source of truth rather than guessing which layer this fixture returns.
        server, _ = show_server
        tool = next(t for t in _run(server.list_tools()) if t.name == "show")
        assert tool.output_schema["required"] == ["view"]

    @pytest.mark.parametrize(
        "args",
        [{"name": "by_region", "kind": "table"}, {"name": "by_region", "kind": "bar"},
         {"kind": "lineage"}, {"kind": "catalog"}],
    )
    def test_every_payload_validates_against_it(self, show_server, args):
        """A declared schema the payload violates is worse than none — clients reject the result."""
        jsonschema = pytest.importorskip("jsonschema")
        server, _ = show_server
        result = _call(server, args)
        jsonschema.validate(result.structured_content, views.SHOW_OUTPUT_SCHEMA)


class TestShowResult:
    def test_returns_both_halves(self, show_server):
        """The model reads `content`; a UI host renders `structuredContent`. Both, always —
        there is no negotiation branch to get wrong."""
        server, _ = show_server
        result = _call(server, {"name": "by_region", "kind": "bar"})
        assert _summary(result)["displayed"] == "bar"
        assert "BarChart" in _types(result.structured_content)

    def test_text_half_is_never_the_prefab_placeholder(self, show_server):
        """Returning a bare component would hand the model '[Rendered Prefab UI]' and blind it."""
        server, _ = show_server
        result = _call(server, {"name": "by_region", "kind": "table"})
        assert "[Rendered Prefab UI]" not in result.content[0].text

    def test_small_result_carries_every_row_like_query_does(self, show_server):
        server, _ = show_server
        summary = _summary(_call(server, {"name": "by_region", "kind": "table"}))
        assert summary["complete"] is True
        assert len(summary["sample"]) == summary["row_count"] == 3

    def test_large_result_carries_a_head(self, show_server):
        server, session = show_server
        session.query("SELECT i FROM range(300) t(i)", "many")
        summary = _summary(_call(server, {"name": "many", "kind": "table"}))
        assert summary["complete"] is False
        assert len(summary["sample"]) == 5 and summary["row_count"] == 300

    def test_chart_reports_the_columns_it_plotted(self, show_server):
        server, _ = show_server
        summary = _summary(_call(server, {"name": "by_region", "kind": "bar"}))
        assert summary["x"] == "region" and summary["series"] == ["revenue"]

    def test_explicit_multi_series(self, show_server):
        server, _ = show_server
        summary = _summary(_call(server, {
            "name": "by_region", "kind": "bar", "x": "region",
            "series": ["revenue", "orders"], "title": "Sales",
        }))
        assert summary["series"] == ["revenue", "orders"]

    def test_interactive_chart_reports_what_is_on_screen(self, show_server):
        """The picker shows one measure at a time, so the summary must not imply otherwise."""
        server, _ = show_server
        summary = _summary(_call(server, {
            "name": "by_region", "kind": "bar",
            "series": ["revenue", "orders"], "interactive": True,
        }))
        assert summary["interactive"] is True
        assert summary["showing"] == "revenue"
        assert summary["series"] == ["revenue", "orders"]

    def test_interactive_is_off_by_default(self, show_server):
        server, _ = show_server
        summary = _summary(_call(server, {
            "name": "by_region", "kind": "bar", "series": ["revenue", "orders"],
        }))
        assert "interactive" not in summary

    @pytest.mark.parametrize("kind", ["profile", "catalog", "lineage"])
    def test_the_other_surfaces_render(self, show_server, kind):
        server, _ = show_server
        args = {"kind": kind}
        if kind == "profile":
            args["name"] = "by_region"
        result = _call(server, args)
        assert _summary(result)["displayed"] == kind
        assert "DataTable" in _types(result.structured_content)

    def test_lineage_shows_the_diagram(self, show_server):
        server, _ = show_server
        result = _call(server, {"kind": "lineage"})
        assert "Mermaid" in _types(result.structured_content)
        assert _summary(result)["node_count"] == 1

    def test_catalog_without_flow_lists_flows(self, show_server):
        server, _ = show_server
        assert "flows" in _summary(_call(server, {"kind": "catalog"}))

    def test_catalog_with_flow_lists_its_results(self, show_server):
        server, _ = show_server
        summary = _summary(_call(server, {"kind": "catalog", "flow": "default"}))
        assert summary["results"] == ["by_region"]


class TestShowIsAView:
    """The load-bearing invariant: `show` displays, it never builds."""

    def test_creates_no_result(self, show_server):
        server, session = show_server
        before = {r["name"] for r in session.catalog("default")["results"]}
        for args in ({"name": "by_region", "kind": "table"},
                     {"name": "by_region", "kind": "bar"},
                     {"name": "by_region", "kind": "profile"},
                     {"kind": "catalog"},
                     {"kind": "lineage"}):
            _call(server, args)
        assert {r["name"] for r in session.catalog("default")["results"]} == before

    def test_records_no_lineage_node(self, show_server):
        server, session = show_server
        before = len(session.lineage()["nodes"])
        _call(server, {"name": "by_region", "kind": "bar"})
        _call(server, {"name": "by_region", "kind": "profile"})
        assert len(session.lineage()["nodes"]) == before


class TestShowErrors:
    def test_chart_over_the_cap_errors_rather_than_truncating(self, show_server):
        server, session = show_server
        session.query("SELECT i, i * 2 AS v FROM range(400) t(i)", "toomany")
        with pytest.raises(Exception, match="too large to display"):
            _call(server, {"name": "toomany", "kind": "bar"})

    def test_a_table_accepts_what_a_chart_refuses(self, show_server):
        """The caps differ on purpose: 400 rows is a fine table and an unreadable bar chart."""
        server, session = show_server
        session.query("SELECT i, i * 2 AS v FROM range(400) t(i)", "toomany")
        assert _summary(_call(server, {"name": "toomany", "kind": "table"}))["row_count"] == 400

    def test_missing_name_says_which_argument(self, show_server):
        server, _ = show_server
        with pytest.raises(Exception, match="needs the `name`"):
            _call(server, {"kind": "table"})

    def test_unknown_kind_lists_the_valid_ones(self, show_server):
        server, _ = show_server
        with pytest.raises(Exception, match="Unknown kind"):
            _call(server, {"name": "by_region", "kind": "donut"})

    def test_unknown_result_names_what_exists(self, show_server):
        server, _ = show_server
        with pytest.raises(Exception, match="by_region"):
            _call(server, {"name": "ghost", "kind": "table"})

    def test_pie_refuses_several_measures_instead_of_dropping_them(self, show_server):
        """A pie can draw one measure. Plotting the first and reporting all of them as
        `series` is the same silent misstatement the row caps exist to prevent."""
        server, _ = show_server
        with pytest.raises(Exception, match="pie chart shows ONE measure"):
            _call(server, {"name": "by_region", "kind": "pie",
                           "series": ["revenue", "orders"]})

    def test_interactive_pie_still_takes_several_measures(self, show_server):
        """The refusal is about showing one and claiming several — a picker shows one at a
        time and SAYS so, which is honest."""
        server, _ = show_server
        summary = _summary(_call(server, {"name": "by_region", "kind": "pie", "interactive": True,
                                          "series": ["revenue", "orders"]}))
        assert summary["showing"] == "revenue"

    @pytest.mark.parametrize("kind", ["profile", "table"])
    def test_a_name_that_is_not_an_identifier_is_rejected(self, show_server, kind):
        """Every display path validates the same way. `profile` builds its own SQL, so an
        unvalidated name there would surface as a DuckDB parse error instead of this."""
        server, _ = show_server
        with pytest.raises(Exception, match="Invalid result name"):
            _call(server, {"name": 'by_region" AS x --', "kind": kind})

    def test_a_flow_that_is_not_an_identifier_is_rejected(self, show_server):
        server, _ = show_server
        with pytest.raises(Exception, match="Invalid flow name"):
            _call(server, {"name": "by_region", "kind": "profile", "flow": 'a" AS x --'})


class TestShowLogging:
    def test_one_json_line_summarising_the_view(self, show_server, tmp_path):
        """The tool log must record the summary, not the Prefab component tree."""
        _, session = show_server
        log = tmp_path / "tools.jsonl"
        server = build_server(session, tool_log=str(log))
        _run(server.call_tool("show", {"name": "by_region", "kind": "bar"}))
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        record = next(e for e in entries if e["tool"] == "show")
        assert record["outcome"] == "ok"
        assert record["args"]["kind"] == "bar"
        assert record["result"]["displayed"] == "bar"
        assert record["result"]["row_count"] == 3
        assert "BarChart" not in log.read_text()
