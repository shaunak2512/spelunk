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
    def test_csp_declares_both_bundle_hosts(self):
        """Serving our own resource means declaring its policy — a resource with no CSP renders
        as a blank frame with no error to debug."""
        csp = vega.app_csp()
        assert "https://cdn.jsdelivr.net" in csp["resource_domains"]
        assert "https://unpkg.com" in csp["resource_domains"]

    def test_csp_grants_no_connect_domains(self):
        """A view that could fetch would be a view that could exfiltrate. `data.url` is refused
        server-side for the same reason; this is the other half of that stance."""
        assert "connect_domains" not in vega.app_csp()

    def test_page_pins_every_bundle_version(self):
        """The version selects the JavaScript users' browsers fetch, not just what we import, so
        a floating major would change what renders without changing this repo."""
        html = vega.app_html()
        for pin in (vega.VEGA_VERSION, vega.VEGA_LITE_VERSION,
                    vega.VEGA_EMBED_VERSION, vega.EXT_APPS_VERSION):
            assert pin in html

    def test_page_uses_the_csp_safe_expression_interpreter(self):
        """`ast: true` is what lets the page run under a sandbox CSP with no 'unsafe-eval'."""
        assert "ast: true" in vega.app_html()

    def test_page_is_deterministic(self):
        """Same page every call — nothing derived from a counter or a clock, so a redeploy is a
        no-op and there is something stable to assert on."""
        assert vega.app_html() == vega.app_html()

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
        tool = next(t for t in _run(server.list_tools()) if t.name == "visual")
        assert tool.meta["ui"]["resourceUri"] == vega.VEGA_URI

    def test_app_resource_is_readable(self, server):
        uris = [str(r.uri) for r in _run(server.list_resources()) if str(r.uri).startswith("ui://")]
        assert uris == [vega.VEGA_URI]
        body = _run(server.read_resource(vega.VEGA_URI))
        assert "<!doctype html>" in body.contents[0].content.lower()


class TestOutputSchema:
    def test_every_summary_validates_against_the_declared_schema(self, session):
        """The tool declares `output_schema`; a summary that does not match it is a contract the
        host cannot rely on."""
        jsonschema = pytest.importorskip("jsonschema")
        summary = _summary(_dispatch_visual(session, name="sales", spec=BAR, title=None, flow=None))
        jsonschema.validate(summary, vega.VISUAL_OUTPUT_SCHEMA)
