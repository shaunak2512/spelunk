"""The visual STORE: authoring a chart once, then redrawing it against live data.

The store is the half of `visual` that persists. Everything here is about the boundary it must
not cross: a stored spec is a *definition* — it holds no data, records no lineage node, creates
no table — and a redraw re-runs every guard an authored spec runs rather than trusting what it
wrote. Those two properties are what make the store safe to add, so they are what these tests
pin down.

`tests/test_visual.py` covers the drawing path and the pure Vega layer; this file covers storage,
redraw, lifecycle (`catalog`/`drop`) and the replay carry.
"""
from __future__ import annotations

import json

import pytest

from spelunk.core.duck import DuckSession
from spelunk.mcp import vega
from spelunk.mcp.server import _dispatch_visual

BAR = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "region", "type": "nominal"},
        "y": {"field": "revenue", "type": "quantitative"},
    },
}


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


def _rows(session: DuckSession, sql: str) -> list[dict]:
    """Run *sql* and return dict rows — `query`'s sample is positional, so zip it back.

    Reading the meta tables through `query` rather than the connection is deliberate: it is
    exactly how an agent would inspect the store, so these tests exercise that path too.
    """
    out = session.query(sql, "tmp_probe")
    names = [c["name"] for c in out["columns"]]
    return [dict(zip(names, row)) for row in out["sample"]]


def _stored(session: DuckSession, flow: str = "default") -> list[dict]:
    """The raw store rows for *flow*, read the way an agent would."""
    return _rows(
        session,
        'SELECT name, reads, spec, fields FROM "_spelunk_meta".visuals '
        f"WHERE flow = '{flow}' ORDER BY name",
    )


# --------------------------------------------------------------------------------- writing --- #
class TestSaveAs:
    def test_stores_the_spec_and_still_draws(self, session):
        result = _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        summary = _summary(result)
        assert summary["saved_as"] == "rev_chart"
        assert summary["row_count"] == 3
        assert result.structured_content["spec"]["data"]["values"]  # it drew, too

        loaded = session.load_visual("default", "rev_chart")
        assert loaded["reads"] == ["sales"]
        assert json.loads(loaded["spec"]) == BAR

    def test_the_stored_spec_is_data_free(self, session):
        """The trap: `hydrate` shallow-copies, so `validated` stays clean — persist THAT.

        Storing the hydrated spec instead would write every drawn row into the meta table as
        text and leave a redraw ignoring the live result entirely. One line apart, and invisible
        until the data changes.
        """
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        stored = json.loads(session.load_visual("default", "rev_chart")["spec"])
        assert "data" not in stored
        assert "10" not in json.dumps(stored)  # no row values leaked in

    def test_saving_replaces_rather_than_appends(self, session):
        """Definitions, not versions. Re-authoring leaves exactly one row."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        other = {**BAR, "mark": "point"}
        _dispatch_visual(session, name="sales", spec=other, save_as="rev_chart")

        rows = _stored(session)
        assert len(rows) == 1
        assert json.loads(rows[0]["spec"])["mark"] == "point"

    def test_records_the_columns_the_spec_needs(self, session):
        """Stored so `replay` can check drift with set arithmetic instead of parsing Vega."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        assert session.load_visual("default", "rev_chart")["fields"] == ["region", "revenue"]

    def test_a_transform_output_is_not_demanded_of_the_result(self, session):
        """A field the spec INVENTS must not be recorded as a column requirement.

        Otherwise a perfectly good chart is reported stale the moment anyone checks it.
        """
        spec = {
            "transform": [{"calculate": "datum.revenue * 2", "as": "doubled"}],
            "mark": "bar",
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "doubled", "type": "quantitative"},
            },
        }
        _dispatch_visual(session, name="sales", spec=spec, save_as="doubled_chart")
        assert session.load_visual("default", "doubled_chart")["fields"] == ["region"]

    def test_stores_nothing_when_the_draw_fails(self, session):
        """A spec that could not be delivered must never enter the store."""
        bad = {"mark": "bar", "encoding": {"x": {"field": "nope", "type": "nominal"}}}
        with pytest.raises(ValueError, match="nope"):
            _dispatch_visual(session, name="sales", spec=bad, save_as="never")
        assert _stored(session) == []

    def test_creates_no_result_and_no_lineage_row(self, session):
        """VIS-001 holds unchanged: a stored spec is a definition, not a result.

        This is the concrete payoff of keeping visuals out of the lineage DAG — the claim did
        not need amending to add a durable store.
        """
        names_before = {r["name"] for r in session.catalog("default")["results"]}
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        names_after = {r["name"] for r in session.catalog("default")["results"]}

        assert names_after == names_before  # nothing new materialized
        assert "rev_chart" not in names_after
        # Assert the absence by NAME rather than by a row count: reading the count needs a
        # query, and a query materializes a result that records a lineage row of its own —
        # the counter would be measuring the measurement.
        recorded = {r["name"] for r in _rows(session, 'SELECT name FROM "_spelunk_meta".lineage')}
        assert "rev_chart" not in recorded


class TestModeRefusals:
    def test_saved_with_name_is_refused(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="c")
        with pytest.raises(ValueError, match="`name`"):
            _dispatch_visual(session, saved="c", name="sales")

    def test_saved_with_spec_is_refused(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="c")
        with pytest.raises(ValueError, match="`spec`"):
            _dispatch_visual(session, saved="c", spec=BAR)

    def test_saved_with_save_as_is_refused(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="c")
        with pytest.raises(ValueError, match="cannot be combined"):
            _dispatch_visual(session, saved="c", save_as="d")

    def test_a_spec_without_a_name_is_refused(self, session):
        with pytest.raises(ValueError, match="`name`"):
            _dispatch_visual(session, spec=BAR)

    def test_a_name_without_a_spec_is_refused(self, session):
        with pytest.raises(ValueError, match="`spec`"):
            _dispatch_visual(session, name="sales")

    def test_an_unknown_visual_names_the_ones_that_exist(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        with pytest.raises(ValueError, match="typo") as exc:
            _dispatch_visual(session, saved="typo")
        assert "rev_chart" in str(exc.value)

    def test_an_unknown_flow_is_refused_not_provisioned(self, session):
        with pytest.raises(ValueError, match="Unknown flow"):
            _dispatch_visual(session, name="sales", spec=BAR, save_as="c", flow="nope")
        assert "nope" not in {f["flow"] for f in session.catalog()["flows"]}

    def test_multi_result_reads_are_refused_clearly(self, session):
        with pytest.raises(ValueError, match="datasets"):
            session.record_visual("default", "c", ["a", "b"], json.dumps(BAR), [])


# ------------------------------------------------------------------------------ redrawing --- #
class TestRedraw:
    def test_draws_a_stored_spec_with_no_name_or_spec(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        summary = _summary(_dispatch_visual(session, saved="rev_chart"))
        assert summary["saved"] == "rev_chart"
        assert summary["name"] == "sales"
        assert summary["row_count"] == 3

    def test_picks_up_data_the_result_did_not_have_at_authoring(self, session):
        """The live-dashboard property, without a live connection."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        session.query(
            "SELECT * FROM (VALUES ('north', 1), ('south', 2), ('east', 3), ('west', 4)) "
            "t(region, revenue)",
            "sales",
        )
        summary = _summary(_dispatch_visual(session, saved="rev_chart"))
        assert summary["row_count"] == 4

    def test_re_validates_against_the_current_schema(self, session):
        """A column renamed since authoring is an ERROR naming the field, not a blank chart.

        This is the staleness check falling out of the redraw for free: the store is not a way
        around a guard, because the redraw re-runs the guard.
        """
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        session.query("SELECT 'north' AS region, 10 AS total_revenue", "sales")
        with pytest.raises(ValueError, match="revenue"):
            _dispatch_visual(session, saved="rev_chart")

    def test_a_missing_read_target_names_the_result(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        session.drop("sales")
        with pytest.raises(ValueError, match="sales"):
            _dispatch_visual(session, saved="rev_chart")

    def test_the_row_cap_applies_to_a_redraw_too(self, session, monkeypatch):
        """The result may have grown since authoring; every guard runs on both paths."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        monkeypatch.setattr(vega, "VEGA_MAX_ROWS", 2)
        with pytest.raises(ValueError, match="too large"):
            _dispatch_visual(session, saved="rev_chart")

    def test_the_stored_title_is_used_and_can_be_overridden(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart", title="Revenue")
        assert _dispatch_visual(
            session, saved="rev_chart"
        ).structured_content["spec"]["title"] == "Revenue"
        assert _dispatch_visual(
            session, saved="rev_chart", title="Override"
        ).structured_content["spec"]["title"] == "Override"


# ------------------------------------------------------------------------------ lifecycle --- #
class TestCatalog:
    def test_lists_stored_visuals_without_inlining_the_spec(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart", title="Revenue")
        entry = session.catalog("default")["visuals"][0]
        assert entry == {
            "name": "rev_chart", "reads": ["sales"],
            "title": "Revenue", "description": None,
        }

    def test_omits_the_section_when_a_flow_has_none(self, session):
        assert "visuals" not in session.catalog("default")

    def test_the_flow_listing_counts_them(self, session):
        """A flow holding only charts must not read as empty."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        entry = next(f for f in session.catalog()["flows"] if f["flow"] == "default")
        assert entry["visual_count"] == 1


class TestDrop:
    def test_removes_a_visual_and_reports_it(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        out = session.drop("rev_chart")
        assert out["dropped_visual"] is True
        assert out["dropped"] is False  # no table of that name
        assert "visuals" not in session.catalog("default")

    def test_a_result_and_a_visual_of_one_name_go_together(self, session):
        """Names may collide; `drop` resolves the ambiguity by REPORTING, not prohibiting.

        Enforcing one namespace would need guards on three separate materialize paths, and
        partial enforcement of an invariant is worse than none.
        """
        session.query("SELECT 1 AS region, 2 AS revenue", "shared")
        _dispatch_visual(session, name="sales", spec=BAR, save_as="shared")
        out = session.drop("shared")
        assert out == {
            "flow": "default", "name": "shared",
            "dropped": True, "dropped_visual": True,
        }

    def test_dropping_a_flow_counts_its_visuals(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="a")
        _dispatch_visual(session, name="sales", spec=BAR, save_as="b")
        out = session.drop(flow="default")
        assert out["dropped_visuals"] == 2

    def test_dropping_an_unrelated_name_leaves_the_visual(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        assert session.drop("sales")["dropped_visual"] is False
        assert session.catalog("default")["visuals"][0]["name"] == "rev_chart"


# --------------------------------------------------------------------------------- replay --- #
class TestReplayCarry:
    def test_in_place_carries_without_touching_the_spec(self, session):
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        before = session.load_visual("default", "rev_chart")["spec"]
        out = session.replay("default")
        assert out["visuals"]["carried"] == [{"name": "rev_chart", "reads": ["sales"]}]
        assert session.load_visual("default", "rev_chart")["spec"] == before

    def test_into_copies_the_definition_and_it_redraws_there(self, session):
        """Without this, a 'complete' rebuild has the data and none of the charts."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        out = session.replay("default", into="rebuilt")
        assert out["visuals"]["carried"] == [{"name": "rev_chart", "reads": ["sales"]}]

        summary = _summary(_dispatch_visual(session, saved="rev_chart", flow="rebuilt"))
        assert summary["flow"] == "rebuilt"
        assert summary["row_count"] == 3
        # The original is untouched — a rebuild `into` is non-destructive for charts too.
        assert session.load_visual("default", "rev_chart")["name"] == "rev_chart"

    def test_a_renamed_column_is_reported_stale_not_raised(self, session):
        """Replay succeeded at its job; failing the rebuild over one chart would be wrong."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        session.query("SELECT 'north' AS region, 10 AS total_revenue", "sales")

        out = session.replay("default", into="rebuilt")
        assert out["rebuilt"]  # the data rebuild still succeeded
        assert out["visuals"]["carried"] == []
        assert out["visuals"]["stale"] == [{
            "name": "rev_chart", "reads": ["sales"],
            "reason": "missing_fields", "missing_fields": ["revenue"],
        }]
        # Reported AND copied: fixing it is a re-author, not a recovery.
        assert session.load_visual("rebuilt", "rev_chart")["name"] == "rev_chart"

    def test_a_missing_read_target_is_reported_not_raised(self, session):
        """The unguarded schema lookup would raise MID-replay, after results are rebuilt."""
        session.query("SELECT 1 AS x", "keeper")
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        session.drop("sales")

        out = session.replay("default")
        assert out["visuals"]["stale"] == [{
            "name": "rev_chart", "reads": ["sales"], "reason": "missing_result",
        }]

    def test_dry_run_reports_visuals_but_cannot_predict_drift(self, session):
        """The honest limit: field drift needs a post-rebuild schema, so a plan cannot see it."""
        _dispatch_visual(session, name="sales", spec=BAR, save_as="rev_chart")
        session.query("SELECT 'north' AS region, 10 AS total_revenue", "sales")

        out = session.replay("default", dry_run=True)
        assert out["visuals"]["carried"] == [{"name": "rev_chart", "reads": ["sales"]}]
        assert out["visuals"]["stale"] == []  # drift is invisible here, by construction

    def test_omits_the_section_when_a_flow_has_no_visuals(self, session):
        assert "visuals" not in session.replay("default")
