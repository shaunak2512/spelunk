"""Tests for lineage tracking + replay on DuckSession.

Every result records the SQL and dependency edges that built it (in an internal
`_spelunk_meta.lineage` table), so a flow is a reproducible pipeline: `lineage` inspects the
DAG and `replay` rebuilds it in dependency order, in place or into a fresh flow.
"""
from __future__ import annotations

import pytest

from spelunk.core.duck import DuckSession


@pytest.fixture
def session(sqlite_file, csv_file):
    """A session over the sample SQLite DB ('shop') and the orders CSV ('orders')."""
    s = DuckSession.open([f"shop={sqlite_file}", f"orders={csv_file}"])
    yield s
    s.close()


def _build_chain(session, flow="default"):
    """base <- shop.customers; mid <- base; top <- mid (+ orders source leaf)."""
    session.query('SELECT id, name, city FROM "shop"."customers"', "base", flow)
    session.query("SELECT id, name FROM base WHERE city IS NOT NULL", "mid", flow)
    session.query(
        "SELECT m.name, o.amount FROM mid m JOIN orders o ON m.id = o.customer_id",
        "top",
        flow,
    )


class TestLineageRecording:
    def test_deps_and_sources_classified(self, session):
        _build_chain(session)
        lin = session.lineage("top")
        nodes = {n["name"]: n for n in lin["nodes"]}
        # top depends on mid (a result), and reads orders (a source leaf) — not a dep.
        assert [d["name"] for d in nodes["top"]["deps"]] == ["mid"]
        assert "orders" in nodes["top"]["sources"]
        # base reads shop.customers, a source leaf, and has no result deps.
        assert nodes["base"]["deps"] == []
        assert any("customers" in s for s in nodes["base"]["sources"])

    def test_closure_is_transitive_and_ordered(self, session):
        _build_chain(session)
        lin = session.lineage("top")
        assert {n["name"] for n in lin["nodes"]} == {"base", "mid", "top"}
        # dependency-first: base before mid before top.
        order = [ref.split(".", 1)[1] for ref in lin["order"]]
        assert order.index("base") < order.index("mid") < order.index("top")
        assert {"default.base->default.mid", "default.mid->default.top"} <= {
            f'{e["from"]}->{e["to"]}' for e in lin["edges"]
        }

    def test_whole_flow_without_name(self, session):
        _build_chain(session)
        lin = session.lineage()
        assert lin["root"] is None
        assert {n["name"] for n in lin["nodes"]} == {"base", "mid", "top"}

    def test_replace_updates_lineage(self, session):
        _build_chain(session)
        # Redefine mid so it no longer depends on base.
        session.query('SELECT id, name FROM "shop"."customers"', "mid")
        lin = session.lineage("mid")
        assert {n["name"] for n in lin["nodes"]} == {"mid"}  # base no longer upstream

    def test_cte_not_recorded_as_source_leaf(self, session):
        # A WITH alias is internal to the query — it must not leak into `sources`, while the
        # real result the CTE reads (base) is still recorded as a dependency.
        session.query('SELECT id, name, city FROM "shop"."customers"', "base")
        session.query(
            "WITH hi AS (SELECT * FROM base WHERE city IS NOT NULL) "
            "SELECT COUNT(*) AS n FROM hi",
            "counted",
        )
        node = {n["name"]: n for n in session.lineage("counted")["nodes"]}["counted"]
        assert node["sources"] == []
        assert [d["name"] for d in node["deps"]] == ["base"]

    def test_cross_flow_dependency_followed(self, session):
        session.query('SELECT id, name FROM "shop"."customers"', "people", "flowA")
        session.query('SELECT * FROM "flowA"."people"', "copy", "flowB")
        lin = session.lineage("copy", "flowB")
        names = {(n["flow"], n["name"]) for n in lin["nodes"]}
        assert ("flowB", "copy") in names and ("flowA", "people") in names

    def test_missing_dependency_reported(self, session):
        _build_chain(session)
        session.drop("base")  # mid still references it, but its lineage is gone
        lin = session.lineage("mid")
        assert "default.base" in lin["missing"]

    def test_drop_flow_clears_lineage(self, session):
        _build_chain(session, flow="scratch")
        session.drop(flow="scratch")
        lin = session.lineage(flow="scratch")
        assert lin["nodes"] == []


class TestDescriptions:
    def test_description_recorded_in_lineage_and_catalog(self, session):
        session.query(
            'SELECT id, name FROM "shop"."customers"',
            "people",
            description="List of every customer with their name.",
        )
        node = {n["name"]: n for n in session.lineage("people")["nodes"]}["people"]
        assert node["description"] == "List of every customer with their name."
        # catalog surfaces the same label alongside the result.
        cat = {r["name"]: r for r in session.catalog("default")["results"]}
        assert cat["people"]["description"] == "List of every customer with their name."

    def test_description_optional_defaults_to_none(self, session):
        session.query("SELECT 1 AS x", "plain")
        node = {n["name"]: n for n in session.lineage("plain")["nodes"]}["plain"]
        assert node["description"] is None
        cat = {r["name"]: r for r in session.catalog("default")["results"]}
        assert cat["plain"]["description"] is None

    def test_blank_description_normalised_to_none(self, session):
        session.query("SELECT 1 AS x", "blank", description="   ")
        node = {n["name"]: n for n in session.lineage("blank")["nodes"]}["blank"]
        assert node["description"] is None

    def test_replace_updates_description(self, session):
        session.query("SELECT 1 AS x", "r", description="first label")
        session.query("SELECT 2 AS x", "r", description="second label")
        node = {n["name"]: n for n in session.lineage("r")["nodes"]}["r"]
        assert node["description"] == "second label"

    def test_batch_step_descriptions(self, session):
        session.query_steps([
            {"sql": 'SELECT * FROM "shop"."customers"', "name": "base",
             "description": "All customers, raw."},
            {"sql": "SELECT COUNT(*) AS n FROM base", "name": "agg",
             "description": "How many customers there are."},
        ])
        nodes = {n["name"]: n for n in session.lineage("agg")["nodes"]}
        assert nodes["base"]["description"] == "All customers, raw."
        assert nodes["agg"]["description"] == "How many customers there are."

    def test_replay_preserves_descriptions(self, session):
        session.query('SELECT id, name FROM "shop"."customers"', "base",
                      description="Raw customer list.")
        session.query("SELECT COUNT(*) AS n FROM base", "cnt",
                      description="Customer count.")
        session.replay(into="v2")
        nodes = {n["name"]: n for n in session.lineage("cnt", "v2")["nodes"]}
        assert nodes["base"]["description"] == "Raw customer list."
        assert nodes["cnt"]["description"] == "Customer count."


class TestLineageErrors:
    def test_unknown_result_raises(self, session):
        with pytest.raises(ValueError, match="No lineage"):
            session.lineage("nope")

    def test_reserved_flow_rejected(self, session):
        with pytest.raises(ValueError, match="reserved"):
            session.lineage(flow="_spelunk_meta")


class TestMermaidRender:
    def test_render_returns_deterministic_flowchart(self, session):
        _build_chain(session)
        lin = session.lineage("top", render="mermaid")
        diagram = lin["mermaid"]
        assert diagram.startswith("flowchart TD")
        # A result node per built table, name in bold.
        for nm in ("base", "mid", "top"):
            assert f"<b>{nm}</b>" in diagram
        # Sources and results are visually distinct: cylinder+source class vs box+result class.
        assert "classDef source" in diagram and "classDef result" in diagram
        assert '[("<b>orders</b>")]' in diagram
        # At least one arrow per recorded result edge (base->mid, mid->top); source leaves
        # (orders, shop.customers) add more.
        assert diagram.count(" --> ") >= len(lin["edges"])
        assert "orders" in diagram
        # Byte-identical on re-render — no LLM in the loop, so it's reproducible.
        assert session.lineage("top", render="mermaid")["mermaid"] == diagram

    def test_source_leaves_have_no_dangling_edges(self, session):
        _build_chain(session)
        lin = session.lineage("top", render="mermaid")
        # Every id referenced by an edge must be declared as a node line.
        import re

        declared = set(re.findall(r"^\s+(n\d+)[\[(]", lin["mermaid"], re.MULTILINE))
        endpoints = set(re.findall(r"(n\d+) --> (n\d+)", lin["mermaid"]))
        used = {a for a, _ in endpoints} | {b for _, b in endpoints}
        assert used <= declared

    def test_path_writes_file_and_reports_absolute_path(self, session, tmp_path):
        import os

        _build_chain(session)
        out = tmp_path / "sub" / "lineage.mmd"
        lin = session.lineage("top", path=str(out))
        # path implies mermaid render, writes the file, and echoes the absolute path.
        assert lin["mermaid"].startswith("flowchart TD")
        assert lin["rendered_to"] == os.path.abspath(str(out))
        assert out.read_text(encoding="utf-8").rstrip("\n") == lin["mermaid"]

    def test_unknown_render_rejected(self, session):
        _build_chain(session)
        with pytest.raises(ValueError, match="Unsupported render"):
            session.lineage("top", render="graphviz")

    def test_name_and_description_are_styled_differently(self, session):
        session.query('SELECT id FROM "shop"."customers"', "base", description="all customers")
        mmd = session.lineage("base", render="mermaid")["mermaid"]
        assert "<b>base</b><br/><i>all customers</i>" in mmd
        dot = session.lineage("base", render="dot")["dot"]
        assert "<B>base</B><BR/><I>all customers</I>" in dot

    def test_markup_in_description_is_escaped(self, session):
        session.query(
            'SELECT id FROM "shop"."customers"', "base", description='a <b>& "x"</b>'
        )
        mmd = session.lineage("base", render="mermaid")["mermaid"]
        # User text can't break out of the label or inject its own markup.
        assert "&lt;b&gt;&amp; &quot;x&quot;&lt;/b&gt;" in mmd
        dot = session.lineage("base", render="dot")["dot"]
        assert "&lt;b&gt;&amp; \"x\"&lt;/b&gt;" in dot

    def test_dot_render_is_valid_digraph(self, session):
        _build_chain(session)
        lin = session.lineage("top", render="dot")
        dot = lin["dot"]
        assert dot.startswith("digraph lineage {")
        assert dot.rstrip().endswith("}")
        for nm in ("base", "mid", "top"):
            assert f"<B>{nm}</B>" in dot
        assert " -> " in dot
        assert "orders" in dot
        # Results are boxes, sources are cylinders.
        assert dot.count("shape=box") == 3 and "shape=cylinder" in dot
        # Reproducible with no LLM in the loop.
        assert session.lineage("top", render="dot")["dot"] == dot

    def test_dot_and_mermaid_share_node_ids(self, session):
        _build_chain(session)
        import re

        mmd = session.lineage("top", render="mermaid")["mermaid"]
        dot = session.lineage("top", render="dot")["dot"]
        mmd_edges = set(re.findall(r"(n\d+) --> (n\d+)", mmd))
        dot_edges = set(re.findall(r"(n\d+) -> (n\d+)", dot))
        assert mmd_edges == dot_edges

    def test_path_defaults_to_mermaid_not_dot(self, session, tmp_path):
        _build_chain(session)
        lin = session.lineage("top", path=str(tmp_path / "g.mmd"))
        assert "mermaid" in lin and "dot" not in lin


class TestReplay:
    def test_dry_run_plans_without_executing(self, session):
        _build_chain(session)
        plan = session.replay(into="rebuilt", dry_run=True)
        assert plan["dry_run"] is True
        assert plan["order"] == ["base", "mid", "top"]
        assert session.catalog("rebuilt")["results"] == []  # nothing built

    def test_replay_into_new_flow_is_nondestructive(self, session):
        _build_chain(session)
        res = session.replay(into="v2")
        assert res["target_flow"] == "v2"
        assert [r["name"] for r in res["rebuilt"]] == ["base", "mid", "top"]
        # v2 reproduces the same top, and the original default flow is untouched.
        orig = session.query('SELECT * FROM "default"."top" ORDER BY name', "a", "probe")["sample"]
        rebuilt = session.query('SELECT * FROM "v2"."top" ORDER BY name', "b", "probe")["sample"]
        assert rebuilt == orig
        assert {n["name"] for n in session.lineage(flow="default")["nodes"]} == {
            "base",
            "mid",
            "top",
        }

    def test_replay_in_place_refreshes(self, session):
        _build_chain(session)
        res = session.replay()
        assert res["source_flow"] == res["target_flow"] == "default"
        assert session.query("SELECT COUNT(*) AS n FROM top", "c")["sample"][0][0] == 3

    def test_replayed_flow_has_lineage(self, session):
        _build_chain(session)
        session.replay(into="v2")
        lin = session.lineage("top", "v2")
        top = next(n for n in lin["nodes"] if n["name"] == "top")
        # deps were re-derived against the rebuilt v2 siblings, not the original flow.
        assert top["deps"] == [{"flow": "v2", "name": "mid"}]

    def test_empty_flow_raises(self, session):
        with pytest.raises(ValueError, match="no recorded lineage"):
            session.replay(flow="never_used")

    def test_dependency_cycle_detected(self, session):
        # a <- source; b <- a; then redefine a to read b -> logical cycle a<->b.
        session.query("SELECT 1 AS x", "a")
        session.query("SELECT x FROM a", "b")
        session.query("SELECT x FROM b", "a")
        with pytest.raises(ValueError, match="cycle"):
            session.replay()
