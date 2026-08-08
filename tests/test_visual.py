"""The `visual` tool and the pure Vega-Lite layer behind it.

Everything in `spelunk/mcp/vega.py` takes plain dicts and returns plain dicts, so the whole
rendering surface is testable here with no browser, no MCP host and no rendering package
installed. That is the property the declarative-spec design was chosen for, and these tests are
what make it real: a spec can be *inspected* before it draws.

The `visual` tests drive `_dispatch_visual` directly rather than through a client, for the same
reason the rest of the suite does — it is the whole tool body, and the FastMCP registration is
covered by tests/test_cli.py asserting the tool is exposed.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from spelunk.core.duck import DuckSession
from spelunk.mcp import vega
from spelunk.mcp.server import _dispatch_visual, build_server


def _run(coro):
    return asyncio.run(coro)

BAR = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "region", "type": "nominal"},
        "y": {"field": "revenue", "type": "quantitative"},
    },
}
COLUMNS = [{"name": "region", "type": "VARCHAR"}, {"name": "revenue", "type": "BIGINT"}]


@pytest.fixture
def session():
    s = DuckSession.open(session_dir=None)
    s.query(
        "SELECT * FROM (VALUES ('north', 10), ('south', 25), ('east', 7)) t(region, revenue)",
        "sales",
    )
    yield s
    s.close()


def _summary(result) -> dict:
    return json.loads(result.content[0].text)


# ------------------------------------------------------------------------------ the guard --- #
class TestValidateSpec:
    def test_accepts_a_spec_whose_fields_are_real_columns(self):
        assert vega.validate_spec(BAR, COLUMNS) == BAR

    def test_parses_a_spec_given_as_a_json_string(self):
        """Agents routinely hand back JSON text rather than an object; both must work."""
        assert vega.validate_spec(json.dumps(BAR), COLUMNS) == BAR

    def test_rejects_a_field_that_is_not_a_column(self):
        """The whole point: Vega-Lite draws a missing field as a blank chart, silently.

        Erroring here is what stops a mis-plot reaching the user, and the message has to name
        the real columns or the agent cannot fix it without another round trip.
        """
        spec = {"mark": "bar", "encoding": {"x": {"field": "regoin", "type": "nominal"}}}
        with pytest.raises(ValueError, match="regoin") as exc:
            vega.validate_spec(spec, COLUMNS)
        assert "region" in str(exc.value)  # the available columns are named

    def test_allows_a_field_a_transform_invents(self):
        """A calculated column exists only after the transform runs, so it is not in the schema.

        Rejecting it would make the guard refuse perfectly good specs — the failure mode that
        would push an agent into avoiding transforms altogether.
        """
        spec = {
            "transform": [{"calculate": "datum.revenue * 2", "as": "doubled"}],
            "mark": "bar",
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "doubled", "type": "quantitative"},
            },
        }
        assert vega.validate_spec(spec, COLUMNS) == spec

    def test_allows_fold_output_fields(self):
        """`fold` names its outputs `key`/`value` when `as` is omitted."""
        spec = {
            "transform": [{"fold": ["revenue"]}],
            "mark": "bar",
            "encoding": {"x": {"field": "key", "type": "nominal"},
                         "y": {"field": "value", "type": "quantitative"}},
        }
        assert vega.validate_spec(spec, COLUMNS) == spec

    def test_a_column_named_only_in_an_expression_is_out_of_scope(self):
        """Pins the documented boundary of the field guard, in BOTH places that inherit it.

        A misspelling inside an expression string passes, where the same misspelling in an
        encoding is refused — and `required_columns` omits it too, so `replay`'s drift check
        cannot see it either. Asserted rather than left implicit because this is a limit worth
        noticing on purpose: if `_field_refs` ever learns to read expressions, this test fails
        and forces VIS-007's scope to be restated instead of drifting.
        """
        spec = {
            "transform": [{"filter": "datum.revnue > 0"}],
            "mark": "bar",
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        }
        assert vega.validate_spec(spec, COLUMNS) == spec
        assert vega.required_columns(spec) == ["region"]

    def test_rejects_data_url(self):
        """The one real egress channel in a declarative artifact — the viewer's browser fetching
        a host we never see. Refused server-side, where it is checkable."""
        spec = {"data": {"url": "https://evil.example/x.json"}, "mark": "bar"}
        with pytest.raises(ValueError, match="data.url"):
            vega.validate_spec(spec, COLUMNS)

    def test_rejects_data_url_nested_in_a_layer(self):
        """A layered spec has its own `data` blocks; checking only the top level would miss them."""
        spec = {"layer": [{"data": {"url": "https://evil.example/x.json"}, "mark": "line"}]}
        with pytest.raises(ValueError, match="data.url"):
            vega.validate_spec(spec, COLUMNS)

    def test_rejects_a_top_level_data_key(self):
        """The server owns the data. A spec carrying its own would draw something other than the
        result it claims to display."""
        spec = dict(BAR, data={"values": [{"region": "made up", "revenue": 999}]})
        with pytest.raises(ValueError, match="top-level `data`"):
            vega.validate_spec(spec, COLUMNS)

    def test_rejects_a_non_object_spec(self):
        with pytest.raises(ValueError, match="Vega-Lite spec object"):
            vega.validate_spec([1, 2, 3], COLUMNS)

    def test_rejects_unparseable_json(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            vega.validate_spec("{not json", COLUMNS)

    def test_image_mark_url_encoding_is_not_mistaken_for_data_url(self):
        """`url` is a legitimate encoding channel for the image mark. The data-url check is
        scoped to `data` blocks precisely so this stays valid."""
        spec = {"mark": "image", "encoding": {"url": {"field": "region", "type": "nominal"}}}
        assert vega.validate_spec(spec, COLUMNS) == spec

    def test_rejects_a_top_level_selection_param_on_a_layered_spec(self):
        """Found in the field: Vega-Lite only allows selections inside UNIT specs, and a
        top-level select param on a `layer` spec compiles to duplicate signals — the chart dies
        in the RENDERER (`Duplicate signal name: "<name>_tuple"`) after the tool has already
        returned success, so only the human sees the wreckage. Reproduced headlessly on
        vega-lite 5.21, 5.23 and 6.4: no pin bump fixes it, so the server refuses it where the
        agent can read the message and move the params."""
        spec = {
            "params": [{"name": "sel", "select": {"type": "point", "fields": ["region"]}}],
            "layer": [{"mark": "line", "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "revenue", "type": "quantitative"},
            }}],
        }
        with pytest.raises(ValueError, match="unit specs"):
            vega.validate_spec(spec, COLUMNS)

    def test_allows_selection_params_inside_the_layer_unit(self):
        """The documented placement — the refusal must name it, not block it."""
        spec = {
            "layer": [{
                "params": [{"name": "sel", "select": {"type": "point", "fields": ["region"]}}],
                "mark": "line",
                "encoding": {
                    "x": {"field": "region", "type": "nominal"},
                    "y": {"field": "revenue", "type": "quantitative"},
                },
            }],
        }
        assert vega.validate_spec(spec, COLUMNS) == spec

    def test_allows_a_top_level_selection_param_on_a_unit_spec(self):
        spec = dict(BAR, params=[
            {"name": "sel", "select": {"type": "point", "fields": ["region"]}}
        ])
        assert vega.validate_spec(spec, COLUMNS) == spec

    def test_allows_a_top_level_variable_param_on_a_layered_spec(self):
        """Variable params (no `select`) are legal at the top level of ANY spec — the refusal
        is scoped to selections, the thing the grammar actually restricts."""
        spec = {
            "params": [{"name": "cutoff", "value": 5}],
            "layer": [{"mark": "line", "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "revenue", "type": "quantitative"},
            }}],
        }
        assert vega.validate_spec(spec, COLUMNS) == spec


# --------------------------------------------------------------------------- hydration --- #
class TestHydrate:
    def test_injects_rows_as_inline_values(self):
        """No round trip: the renderer gets the rows in the same payload as the spec."""
        rows = [{"region": "north", "revenue": 10}]
        out = vega.hydrate(BAR, rows)
        assert out["data"] == {"values": rows}

    def test_does_not_mutate_the_input_spec(self):
        before = json.dumps(BAR, sort_keys=True)
        vega.hydrate(BAR, [{"region": "north", "revenue": 10}])
        assert json.dumps(BAR, sort_keys=True) == before

    def test_sets_container_width_for_a_single_view(self):
        out = vega.hydrate(BAR, [])
        assert out["width"] == "container"

    def test_leaves_width_alone_on_a_composed_spec(self):
        """Vega-Lite rejects `"container"` on facet/concat/repeat, so it must not be forced."""
        spec = {"hconcat": [BAR, BAR]}
        assert "width" not in vega.hydrate(spec, [])

    def test_respects_an_explicit_width(self):
        assert vega.hydrate(dict(BAR, width=300), [])["width"] == 300

    def test_title_fills_in_only_when_the_spec_has_none(self):
        assert vega.hydrate(BAR, [], "Revenue")["title"] == "Revenue"
        assert vega.hydrate(dict(BAR, title="Own"), [], "Revenue")["title"] == "Own"


# -------------------------------------------------------------------------- the app page --- #
class TestAppPage:
    def test_csp_declares_the_bundle_host_and_nothing_else(self):
        """Serving our own resource means declaring its policy — a resource with no CSP renders
        as a blank frame with no error to debug. jsDelivr ONLY: the transport is inline now, so
        an extra origin here would be pure attack surface."""
        assert vega.app_csp()["resource_domains"] == ["https://cdn.jsdelivr.net"]

    def test_csp_grants_no_connect_domains(self):
        """A view that could fetch would be a view that could exfiltrate. `data.url` is refused
        server-side for the same reason; this is the other half of that stance."""
        assert "connect_domains" not in vega.app_csp()

    def test_page_pins_every_bundle_version(self):
        """The version selects the JavaScript users' browsers fetch, not just what we import, so
        a floating major would change what renders without changing this repo."""
        html = vega.app_html()
        for pin in (vega.VEGA_VERSION, vega.VEGA_LITE_VERSION,
                    vega.VEGA_EMBED_VERSION, vega.UI_PROTOCOL_VERSION):
            assert pin in html

    def test_page_uses_the_csp_safe_expression_interpreter(self):
        """`ast: true` is what lets the page run under a sandbox CSP with no 'unsafe-eval'."""
        assert "ast: true" in vega.app_html()

    def test_page_is_deterministic(self):
        """Same page every call — nothing derived from a counter or a clock, so a redeploy is a
        no-op and there is something stable to assert on."""
        assert vega.app_html() == vega.app_html()

    def test_every_script_is_classic_and_boot_runs_first(self):
        """No ES modules anywhere on the page — a failed module import aborts the whole module
        SILENTLY, which is how the SDK-built page died on Claude Desktop: no handler attached,
        no message, an empty frame and nothing in DevTools that names the cause. Classic
        scripts fail loudly and independently, and the boot script (first) reports for all of
        them. The transport must also be defined before the app script that reads it.
        """
        html = vega.app_html()
        assert 'type="module"' not in html
        boot = html.index("__spelunkStatus")
        transport = html.index("window.SpelunkApp = function")
        app = html.index("const App = window.SpelunkApp")
        assert boot < transport < app

    def test_page_reports_a_failed_bundle_load_to_the_user(self):
        """`e.target.src` is the only signal separating "bundle blocked" from "bundle threw",
        and it is what turns a blank rectangle into a sentence naming the blocked URL."""
        html = vega.app_html()
        assert "failed to load" in html
        assert "e.target.src" in html

    def test_page_records_the_stage_it_reached(self):
        """Every failure here looks identical from the outside — blocked bundle, incomplete
        handshake, missing structuredContent, zero-size chart. The stage is what tells them
        apart without a developer attached."""
        html = vega.app_html()
        assert "stage: " in html
        for stage in ("connecting to host", "tool result received", "drawing", "drawn"):
            assert stage in html

    def test_page_stamps_the_build_it_was_generated_from(self):
        """The stamp the view prints must be the one this server computes, or the mismatch
        check — the only way to tell a cached page from a current one — reads backwards."""
        assert f'build: "{vega.app_build()}"' in vega.app_html()

    def test_page_has_a_static_fallback_naming_the_bundle_hosts(self):
        """The one thing that must render without the bundles: a diagnosis instead of a blank
        rectangle when the host blocks the CSP."""
        html = vega.app_html()
        assert "cdn.jsdelivr.net" in html and "did not load" in html


# ------------------------------------------------------------------------- the tool body --- #
class TestVisualTool:
    def test_returns_both_halves(self, session):
        """A text summary the model reads AND the payload a host renders — never one or the
        other, so a text-only host degrades by simply ignoring structuredContent."""
        result = _dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None)
        summary = _summary(result)
        assert summary["name"] == "sales"
        assert result.structured_content["spec"]["data"]["values"][0]["region"] == "north"

    def test_summary_mirrors_query_sample_contract(self, session):
        """Small result on both axes -> every row, with `complete: true`, exactly like `query`."""
        summary = _summary(_dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None))
        assert summary["complete"] is True
        assert len(summary["sample"]) == 3

    def test_summary_reports_only_the_plotted_fields(self, session):
        """The summary describes what is on screen; a spec can read a subset of a wide result."""
        spec = {"mark": "bar", "encoding": {"x": {"field": "region", "type": "nominal"}}}
        summary = _summary(_dispatch_visual(session, name="sales", spec=spec, title=None, flow=None))
        assert summary["fields"] == ["region"]
        assert summary["columns"] == ["region", "revenue"]

    def test_reports_provenance(self, session):
        """The build order behind the plotted result, for the model's half of the reply."""
        summary = _summary(_dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None))
        assert summary["provenance"]["steps"] == ["default.sales"]

    def test_creates_no_result_and_no_lineage_row(self, session):
        """A view is not a result: nothing to `catalog`, nothing to `drop`, no lineage node."""
        before = {r["name"] for r in session.catalog("default")["results"]}
        _dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None)
        assert {r["name"] for r in session.catalog("default")["results"]} == before

    def test_unknown_flow_is_not_provisioned(self, session):
        """The display path must not create the schema a typo names — that is precisely how
        "a view is not a result" becomes observable in `catalog`."""
        with pytest.raises(ValueError, match="Unknown flow"):
            _dispatch_visual(session, name="sales", spec=BAR, title=None, flow="typo")
        assert "typo" not in {f["flow"] for f in session.catalog()["flows"]}

    def test_bad_field_errors_before_anything_renders(self, session):
        spec = {"mark": "bar", "encoding": {"x": {"field": "nope", "type": "nominal"}}}
        with pytest.raises(ValueError, match="nope"):
            _dispatch_visual(session, name="sales", spec=spec, title=None, flow=None)

    def test_refuses_rather_than_truncates_past_the_row_cap(self, session):
        """A silently shortened chart is a picture that misstates the data."""
        session.query(
            f"SELECT i AS region, i AS revenue FROM range({vega.VEGA_MAX_ROWS + 1}) t(i)", "big"
        )
        with pytest.raises(ValueError):
            _dispatch_visual(session, name="big", spec=BAR, title=None, flow=None)

    def test_refuses_a_payload_the_host_would_divert(self, session):
        """The delivery ceiling, which the ROW cap does not bound.

        Claude writes a tool result over ~150k characters to its sandbox filesystem and hands the
        view a pointer, so the chart renders blank with no error anywhere — and it renders fine in
        MCP Inspector, which has no such sandbox. Refusing here is the only place the failure can
        be given a name.
        """
        session.query(
            "SELECT i AS region, i*1.5 AS revenue, 'a fairly long label ' || i AS note "
            "FROM range(4000) t(i)",
            "chunky",
        )
        spec = {"mark": "bar", "encoding": {
            "x": {"field": "region", "type": "nominal"},
            "y": {"field": "revenue", "type": "quantitative"},
            "tooltip": {"field": "note", "type": "nominal"}}}
        with pytest.raises(ValueError, match="characters") as exc:
            _dispatch_visual(session, name="chunky", spec=spec, title=None, flow=None)
        message = str(exc.value)
        assert "Nothing was truncated" in message
        assert "blank" in message  # names the symptom the user would otherwise just see
        assert "`query`" in message  # and the way out

    def test_reports_payload_size_on_a_chart_that_fits(self, session):
        """So the agent can see the headroom before it runs out, not after."""
        summary = _summary(_dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None))
        assert 0 < summary["payload_chars"] < vega.PAYLOAD_MAX_CHARS


@pytest.fixture
def node():
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not installed")
    return exe


class TestAppScriptIsValidJavaScript:
    """Parse the page's scripts with node, when node is available.

    Nothing else in the suite executes a single line of this JavaScript, which is how a real bug
    shipped twice: a syntax or semantic error here is invisible to pytest and shows up only as a
    blank frame in a host. Parsing is cheap and catches the whole class of "the module never ran".
    """

    def _check(self, node, tmp_path, source: str, name: str):
        # .cjs everywhere, and it is load-bearing: a bare .js makes modern node AUTO-DETECT
        # module syntax and re-parse as a module, which would make top-level `await` — a syntax
        # error in the classic <script> the page actually ships — pass the check.
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        # --check parses without executing, so nothing external is ever resolved.
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{name} does not parse:\n{proc.stderr}"

    def test_boot_script_parses(self, node, tmp_path):
        self._check(node, tmp_path, vega._BOOT_SCRIPT, "boot.cjs")

    def test_transport_script_parses(self, node, tmp_path):
        source = vega._TRANSPORT_SCRIPT.replace("__UI_PROTOCOL_VERSION__", "test")
        self._check(node, tmp_path, source, "transport.cjs")

    def test_app_script_parses_as_a_classic_script(self, node, tmp_path):
        """The page ships this as a classic <script>, where top-level `await` (the way the old
        module version connected) is a SYNTAX error. Parsing it in script mode is what keeps
        that from regressing."""
        self._check(node, tmp_path, vega._APP_SCRIPT, "app.cjs")

    def test_the_parse_check_actually_fails_on_broken_source(self, node, tmp_path):
        """Guard the guard: a `node --check` that silently exits 0 would make the tests above
        decorative, which is precisely the trap the outputSchema test fell into."""
        with pytest.raises(AssertionError, match="does not parse"):
            self._check(node, tmp_path, "const x = (;", "broken.cjs")

    def test_classic_check_rejects_top_level_await(self, node, tmp_path):
        """Proves the script-mode parse would actually catch a reintroduced top-level await."""
        with pytest.raises(AssertionError, match="does not parse"):
            self._check(node, tmp_path, "await Promise.resolve(1);", "bad.cjs")


class TestSingleViewPerElement:
    """A second Vega view on one element collides with the first's signals.

    Vega names a selection's signals `<param>_tuple` etc. in a per-element namespace, so
    re-embedding without tearing down throws `Duplicate signal name: "<param>_tuple"` and renders
    nothing — but ONLY for a spec with `params`. A plain chart survives the same double-embed, so
    this stays hidden until someone writes an interactive spec.
    """

    def test_previous_view_is_finalized_before_re_embedding(self):
        script = vega._APP_SCRIPT
        assert "currentView.finalize()" in script
        assert 'chartEl.innerHTML = ""' in script

    def test_draws_are_serialized(self):
        """The tool result and a host-context change can both draw; overlapping embeds put two
        views on the element even with teardown, because teardown runs before the first await."""
        assert "drawing = (drawing || Promise.resolve())" in vega._APP_SCRIPT

    def test_theme_change_redraws_only_on_an_actual_theme_change(self):
        """`onhostcontextchanged` also carries locale and display-mode, and fires on connect."""
        assert "hostTheme() !== lastTheme" in vega._APP_SCRIPT


# ------------------------------------------------------------------- the read side of it --- #
class TestRowsForDisplay:
    """`DuckSession.rows_for_display` — the read + cap behaviour `visual` sits on.

    Carried over from the deleted test_show.py: the method is unchanged by the Prefab-to-Vega
    swap, and it is where the refuse-rather-than-truncate stance is actually implemented.
    """

    def test_returns_columns_and_row_dicts(self, session):
        columns, rows = session.rows_for_display("sales")
        assert [c["name"] for c in columns] == ["region", "revenue"]
        # Compare order-independently: `rows_for_display` scans with no ORDER BY, so the
        # fixture's materialization order is not a promise DuckDB makes on read-back.
        assert {r["region"]: r["revenue"] for r in rows} == {"north": 10, "south": 25, "east": 7}

    def test_unknown_result_names_what_the_flow_holds(self, session):
        with pytest.raises(ValueError, match="sales"):
            session.rows_for_display("nope")

    def test_refuses_past_the_row_cap_instead_of_truncating(self, session):
        session.query("SELECT i FROM range(500) t(i)", "big")
        with pytest.raises(ValueError, match="too large to display") as exc:
            session.rows_for_display("big", max_rows=100)
        message = str(exc.value)
        assert "500 rows" in message  # the real count, so the agent can size the aggregation
        assert "Nothing was truncated" in message
        assert "GROUP BY" in message  # and the way out

    def test_cell_cap_catches_a_wide_result_the_row_cap_misses(self, session):
        session.query("SELECT i, i AS b, i AS c, i AS d FROM range(50) t(i)", "wide")
        session.rows_for_display("wide", max_rows=100, max_cells=1000)  # 200 cells: fine
        with pytest.raises(ValueError, match="cells"):
            session.rows_for_display("wide", max_rows=100, max_cells=100)

    def test_reads_without_creating_anything(self, session):
        before = session.catalog("default")["results"]
        session.rows_for_display("sales")
        assert session.catalog("default")["results"] == before


# ----------------------------------------------------------------------- the registration --- #
class TestRegistration:
    """The MCP Apps wiring: a tool pointing at a resource the server actually serves.

    A tool whose resourceUri names nothing readable fails silently — the host fetches, gets
    nothing back, and renders an empty frame with no error anywhere.
    """

    @pytest.fixture
    def server(self, session):
        return build_server(session)

    def test_visual_is_registered_with_ui_metadata(self, server):
        """BOTH meta keys, deliberately. FastMCP writes the nested `ui.resourceUri` (the current
        MCP Apps wire form), but Claude Desktop / claude.ai key on the deprecated flat
        `ui/resourceUri` and ignore the nested one — a tool carrying only the nested key gets
        its resource fetched and its call answered, and the iframe never mounts, with no error
        anywhere. The official ext-apps SDK emits both for exactly this reason; so do we."""
        tool = next(t for t in _run(server.list_tools()) if t.name == "visual")
        assert tool.meta["ui"]["resourceUri"] == vega.VEGA_URI
        assert tool.meta["ui/resourceUri"] == vega.VEGA_URI

    def test_declared_output_schema_accepts_the_real_payload(self, server, session):
        """The end-to-end version of the outputSchema contract, done the way a HOST does it:
        read the schema off the registered tool, call the tool, validate one against the other.

        The unit test alone missed a shipped bug — the schema described the text summary while
        structuredContent carried `{spec: ...}`, so Claude Desktop rejected every call with
        "missing a required displayed property" and rendered nothing. Nothing failed locally
        because the test validated the summary, the object the schema was wrongly written for.
        Going through `list_tools` + `call_tool` is what makes the two halves meet.
        """
        jsonschema = pytest.importorskip("jsonschema")
        tool = next(t for t in _run(server.list_tools()) if t.name == "visual")
        result = _run(server.call_tool("visual", {"name": "sales", "spec": BAR}))
        jsonschema.validate(result.structured_content, tool.output_schema)

    def test_app_resource_is_readable(self, server):
        uris = [str(r.uri) for r in _run(server.list_resources()) if str(r.uri).startswith("ui://")]
        assert uris == [vega.VEGA_URI]
        body = _run(server.read_resource(vega.VEGA_URI))
        assert "<!doctype html>" in body.contents[0].content.lower()


class TestOutputSchema:
    """`outputSchema` governs structuredContent — the half the HOST validates.

    Aiming it at the text summary instead is not a cosmetic error: FastMCP requires
    structured_content whenever an output_schema exists, and a host that honours the schema
    (Claude Desktop does) rejects every call with "missing a required <field> property" and
    renders nothing. These tests validate the object that is actually sent.
    """

    def test_structured_content_validates_against_the_declared_schema(self, session):
        jsonschema = pytest.importorskip("jsonschema")
        result = _dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None)
        jsonschema.validate(result.structured_content, vega.VISUAL_OUTPUT_SCHEMA)

    def test_schema_describes_the_spec_envelope_not_the_summary(self):
        """A regression guard with a name: the schema's required key must be the one
        structuredContent actually carries."""
        assert vega.VISUAL_OUTPUT_SCHEMA["required"] == ["spec"]

    def test_summary_carries_what_the_tool_description_promises(self, session):
        """The summary's shape is a contract too — just not an MCP-declared one, so it is
        asserted here rather than smuggled into outputSchema."""
        summary = _summary(_dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None))
        assert {"displayed", "name", "flow", "row_count", "columns", "fields",
                "sample", "complete"} <= set(summary)

    def test_structured_content_is_json_serialisable(self, session):
        """FastMCP serialises structuredContent itself, with no `default=str` to fall back on.

        DuckDB hands back real `date`/`Decimal` objects, which the text half survives only
        because `json.dumps(..., default=str)` covers for them. The payload has no such
        cover, so a result with a date column would fail at the transport with a
        "Could not serialize structured content" error rather than anywhere useful.
        """
        session.query(
            "SELECT DATE '2024-01-01' AS d, CAST(1.5 AS DECIMAL(4,2)) AS amt", "typed"
        )
        spec = {"mark": "line", "encoding": {"x": {"field": "d", "type": "temporal"},
                                             "y": {"field": "amt", "type": "quantitative"}}}
        result = _dispatch_visual(session, name="typed", spec=spec, title=None, flow=None)
        json.dumps(result.structured_content)  # must not raise
