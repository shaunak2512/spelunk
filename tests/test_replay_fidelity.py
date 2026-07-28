"""Replay as a REPRODUCIBILITY guarantee, not just a mechanism.

Claims LIN-019 (a replayed flow is bit-identical when sources are unchanged), LIN-016
(sources and cross-flow results are READ, not rebuilt) and LIN-011 (agent-authored text is
escaped in the rendered diagram).

The existing replay tests are 2-3 node toys checking mechanics. The reproducibility promise —
the thing lineage+replay exist for — is the acid test here: build a realistic DAG, rebuild it
into a fresh flow, and EXCEPT-diff every node in both directions expecting zero.
"""
from __future__ import annotations

import pytest

from spelunk.core.duck import DuckSession

# A pipeline with the shapes real analysis uses: filters, joins, aggregates, window functions,
# a self-referencing chain, and a float aggregate (the documented FP-jitter risk).
PIPELINE = [
    ("clean", 'SELECT id, name, city FROM "shop"."customers" WHERE city IS NOT NULL'),
    ("orders_clean", 'SELECT id, customer_id, amount, status FROM "shop"."orders" '
                     "WHERE amount > 0"),
    ("joined", "SELECT c.id, c.name, c.city, o.amount, o.status "
               "FROM clean c JOIN orders_clean o ON o.customer_id = c.id"),
    ("per_city", "SELECT city, COUNT(*) AS n, SUM(amount) AS total, AVG(amount) AS mean "
                 "FROM joined GROUP BY city"),
    ("ranked", "SELECT city, n, total, "
               "ROW_NUMBER() OVER (ORDER BY total DESC, city) AS rank_by_total, "
               "total / SUM(total) OVER () AS share FROM per_city"),
    ("top", "SELECT city, n, total, share FROM ranked WHERE rank_by_total <= 5 ORDER BY city"),
    ("shipped", "SELECT city, COUNT(*) AS shipped FROM joined WHERE status = 'shipped' "
                "GROUP BY city"),
    ("final", "SELECT t.city, t.n, t.total, t.share, COALESCE(s.shipped, 0) AS shipped "
              "FROM top t LEFT JOIN shipped s ON s.city = t.city ORDER BY t.city"),
]


@pytest.fixture
def built(sqlite_file, tmp_path):
    """The pipeline, materialized in flow `pipe`."""
    session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
    for name, sql in PIPELINE:
        session.query(sql, name, flow="pipe")
    yield session
    session.close()


def _diff(session: DuckSession, left_flow: str, right_flow: str, name: str) -> int:
    """Symmetric difference between the same result in two flows. Zero means identical."""
    out = session.query(
        "SELECT COUNT(*) AS n FROM ("
        f'  (SELECT * FROM "{left_flow}"."{name}" EXCEPT SELECT * FROM "{right_flow}"."{name}")'
        "  UNION ALL "
        f'  (SELECT * FROM "{right_flow}"."{name}" EXCEPT SELECT * FROM "{left_flow}"."{name}")'
        ") t",
        "diffcheck",
        flow="audit",
    )
    return out["sample"][0][0]


class TestReplayIsBitIdentical:
    """LIN-019: the reproducibility acid test."""

    def test_every_node_rebuilds_identically(self, built):
        report = built.replay("pipe", into="pipe_v2")
        assert report

        rebuilt = {node["name"] for node in built.lineage(flow="pipe_v2")["nodes"]}
        assert rebuilt == {name for name, _ in PIPELINE}

        for name, _ in PIPELINE:
            assert _diff(built, "pipe", "pipe_v2", name) == 0, f"{name} differs after replay"

    def test_the_rebuild_is_non_destructive(self, built):
        before = built.query('SELECT * FROM "pipe"."final" ORDER BY city', "before", flow="audit")
        built.replay("pipe", into="pipe_v2")
        after = built.query('SELECT * FROM "pipe"."final" ORDER BY city', "after", flow="audit")
        assert before["sample"] == after["sample"]
        assert before["row_count"] == after["row_count"]

    def test_replayed_flow_carries_the_same_dag(self, built):
        built.replay("pipe", into="pipe_v2")
        original = built.lineage(flow="pipe")
        copy = built.lineage(flow="pipe_v2")
        assert copy["missing"] == []

        def shape(graph):
            return {
                node["name"]: sorted(dep["name"] for dep in node["deps"])
                for node in graph["nodes"]
            }

        assert shape(copy) == shape(original)

    def test_in_place_replay_also_reproduces(self, built):
        before = built.query('SELECT * FROM "pipe"."final" ORDER BY city', "snap", flow="audit")
        built.replay("pipe")
        after = built.query('SELECT * FROM "pipe"."final" ORDER BY city', "snap2", flow="audit")
        assert before["sample"] == after["sample"]


class TestExternalInputsAreReadNotRebuilt:
    """LIN-016: sources and cross-flow results are inputs — replay reads them."""

    def test_a_source_leaf_is_not_rebuilt(self, built):
        """The flow's roots read `shop`, which replay must not try to reconstruct."""
        plan = built.replay("pipe", into="pipe_v3", dry_run=True)
        planned = {step["name"] if isinstance(step, dict) else step for step in plan["order"]}
        assert "shop" not in planned
        assert "shop.customers" not in planned
        assert planned == {name for name, _ in PIPELINE}

    def test_a_cross_flow_dependency_is_read_not_rebuilt(self, built):
        """A flow depending on ANOTHER flow's result rebuilds only its own nodes."""
        built.query("SELECT 1 AS k, 'ref' AS label", "shared", flow="common")
        built.query(
            'SELECT f.city, f.n, r.label FROM "pipe"."final" f CROSS JOIN "common"."shared" r',
            "uses_shared",
            flow="downstream",
        )

        before = built.query(
            'SELECT * FROM "common"."shared"', "shared_before", flow="audit"
        )["sample"]

        report = built.replay("downstream", into="downstream_v2")
        rebuilt = {node["name"] for node in built.lineage(flow="downstream_v2")["nodes"]}
        assert rebuilt == {"uses_shared"}, f"replay rebuilt more than its own flow: {rebuilt}"
        assert "shared" not in rebuilt
        assert report is not None

        # The external result is untouched, and the rebuild used it correctly.
        after = built.query('SELECT * FROM "common"."shared"', "shared_after", flow="audit")
        assert after["sample"] == before
        assert _diff(built, "downstream", "downstream_v2", "uses_shared") == 0


HOSTILE_TEXT = [
    'has "double quotes"',
    "has 'single quotes'",
    "arrow --> injection",
    "semi; colon",
    "pipe | bar",
    "brackets [square] {curly} (round)",
    "new\nline",
    "back\\slash",
    "<html> & entities",
    "subgraph end classDef",
    "unicode — em dash · ★",
    "%%{init: {'theme':'dark'}}%%",
]


class TestRenderEscaping:
    """LIN-011: descriptions and names are agent-authored strings rendered into two grammars.

    A description that breaks the diagram is a broken deliverable; one that INJECTS diagram
    syntax is worse.
    """

    BENIGN = "a plain description"

    def _render(self, sqlite_file, tmp_path, text, render):
        session = DuckSession.open([f"shop={sqlite_file}"], session_dir=str(tmp_path / "ws"))
        try:
            session.query("SELECT 1 AS a", "base", flow="f", description=text)
            session.query("SELECT a + 1 AS a FROM base", "child", flow="f", description=text)
            return session.lineage(flow="f", render=render)[render]
        finally:
            session.close()

    @pytest.mark.parametrize("text", HOSTILE_TEXT)
    @pytest.mark.parametrize("render", ["mermaid", "dot"])
    def test_hostile_text_cannot_change_the_diagram_structure(
        self, sqlite_file, tmp_path, text, render
    ):
        """The implementation-agnostic invariant: description content must not alter SHAPE.

        Same graph, same number of lines, same header — whatever the description contains. A
        newline that splits a label or a metacharacter that opens a new statement shows up here
        without this test needing to know either grammar's escape syntax.
        """
        hostile = self._render(sqlite_file, tmp_path / "a", text, render)
        benign = self._render(sqlite_file, tmp_path / "b", self.BENIGN, render)

        assert hostile.strip(), "empty diagram"
        assert len(hostile.splitlines()) == len(benign.splitlines()), (
            f"{text!r} changed the diagram's line structure"
        )
        header = "flowchart" if render == "mermaid" else "digraph"
        assert hostile.count(header) == benign.count(header) == 1
        assert "base" in hostile and "child" in hostile

    @pytest.mark.parametrize("text", HOSTILE_TEXT)
    @pytest.mark.parametrize("render", ["mermaid", "dot"])
    def test_markup_metacharacters_are_escaped(self, sqlite_file, tmp_path, text, render):
        """Both renderers emit HTML-ish labels, so <, > and & must arrive escaped."""
        diagram = self._render(sqlite_file, tmp_path, text, render)
        label_area = diagram
        if "<" in text:
            assert "&lt;" in label_area, f"raw '<' survived from {text!r}"
        if ">" in text and "-->" not in text:
            assert "&gt;" in label_area, f"raw '>' survived from {text!r}"
        if "&" in text:
            assert "&amp;" in label_area, f"raw '&' survived from {text!r}"

    @pytest.mark.parametrize("text", HOSTILE_TEXT)
    def test_no_description_can_start_a_mermaid_directive(self, sqlite_file, tmp_path, text):
        """The real injection vector: mermaid reads `%%{init:...}` only at the START of a line,
        so what matters is that a description can never reach one — newlines are collapsed."""
        diagram = self._render(sqlite_file, tmp_path, text, "mermaid")
        for line in diagram.splitlines():
            assert not line.lstrip().startswith("%%"), f"{text!r} reached the start of a line"
        assert diagram.count("flowchart") == 1, "a description started a second graph"
