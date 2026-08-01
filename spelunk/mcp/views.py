"""Prefab UI views for the ``show`` tool — pure ``data -> component`` builders.

Kept in its own module for two reasons. First, ``prefab-ui`` is an optional extra
(``pip install spelunk-mcp[ui]``), so the import lives behind one guard here instead of being
scattered through ``server.py``; ``PREFAB_AVAILABLE`` is what decides whether ``show`` gets
registered at all. Second, every builder below takes plain dicts and lists and returns a Prefab
component — no DuckDB session, no FastMCP client — so the whole rendering layer is unit-testable
without either.

The governing rule for everything here: **a view is not a result.** Nothing in this module
writes a table, records lineage, or mutates the session. ``show`` displays what ``query``,
``profile``, ``catalog`` and ``lineage`` already produced.
"""
from __future__ import annotations

import json
from typing import Any

from spelunk.core.types import is_numeric_type

try:  # pragma: no cover - exercised by both installs, only one path per environment
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        Column,
        DataTable,
        DataTableColumn,
        Elif,
        Else,
        H3,
        H4,
        If,
        Mermaid,
        Metric,
        Muted,
        Row,
        Select,
        SelectOption,
    )
    from prefab_ui.components.charts import (
        AreaChart,
        BarChart,
        ChartSeries,
        LineChart,
        PieChart,
        ScatterChart,
    )
    from prefab_ui.renderer import get_renderer_csp, get_renderer_html
    from prefab_ui.rx import Rx

    PREFAB_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on whether the [ui] extra is installed
    PREFAB_AVAILABLE = False

# A chart is capped far below a table: the payload cost is the same per row, but 200 bars, slices
# or points is already past the point where anyone can read the picture. Past this the honest
# answer is "aggregate first", which is the thing Spelunk is good at.
CHART_MAX_ROWS = 200

#: Chart kinds `show` accepts, mapped to the Prefab component that draws them.
_CARTESIAN = ("bar", "line", "area", "scatter")
CHART_KINDS = (*_CARTESIAN, "pie")
#: Every value of `show`'s `kind` discriminator. The non-chart kinds render a different Spelunk
#: surface entirely (a profile, the catalog, a lineage DAG) rather than a saved result's rows.
SHOW_KINDS = ("table", *CHART_KINDS, "profile", "catalog", "lineage")


#: What `show` puts in ``structuredContent``: the Prefab envelope. MCP Apps guidance is to
#: declare an outputSchema whenever a tool returns structured content — it lets clients validate
#: the result and gives the renderer a stable contract. FastMCP suppresses the schema it would
#: otherwise infer for app tools, so `show` states it explicitly. (This does NOT work around the
#: Claude Desktop stripping bug — that was tested both ways upstream — it is simply correct.)
SHOW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "A Prefab UI envelope: the component tree a UI host renders.",
    "properties": {
        "$prefab": {
            "type": "object",
            "description": "Envelope metadata, notably the Prefab protocol version.",
            "properties": {"version": {"type": "string"}},
        },
        "view": {"type": "object", "description": "The root component of the rendered view."},
        "state": {"type": "object", "description": "Initial client-side state, when the view "
                                                   "carries interactive controls."},
        "defs": {"type": "object", "description": "Reusable component definitions."},
    },
    "required": ["view"],
    "additionalProperties": True,
}


def to_payload(view: Any) -> dict[str, Any]:
    """Serialize a component or a composed app to the Prefab wire JSON.

    ``show`` builds its own ``ToolResult`` rather than returning a bare component, so it has to
    do this conversion itself — the automatic path in ``fastmcp.tools.base`` only fires for a
    raw component return, and taking it would replace the model-visible text with the literal
    string ``[Rendered Prefab UI]``.
    """
    app = view if isinstance(view, PrefabApp) else PrefabApp(view=view)
    return app.to_json()


# ------------------------------------------------------------------------ renderer resource --- #
#: The URI of Spelunk's own renderer resource. `show` points at this instead of the per-tool
#: renderer FastMCP would synthesize, so the recovery shim below travels with it.
RENDERER_URI = "ui://spelunk/renderer.html"

# TEMPORARY — remove when modelcontextprotocol/ext-apps#696 is fixed; grep "ext-apps#696".
#
# Claude Desktop strips `structuredContent` (and `_meta`) from the `ui/notifications/tool-result`
# it forwards to an app view. The view then has nothing to draw and sits on "Waiting for
# content..." forever. Verified on the wire here: the notification arrives carrying only
# [content, isError]. claude.ai, MCP Jam and ChatGPT deliver the field intact.
#
# Recovery, the same shape Apify shipped (apify/apify-mcp-server#1187) adapted to Prefab:
#
#   1. Capture the call's arguments from `ui/notifications/tool-input`, which arrives intact.
#   2. On a tool-result missing `structuredContent`, re-issue the SAME call through the host's
#      `tools/call` proxy, which is unaffected by the bug and returns the full result.
#   3. Hand the recovered payload to the Prefab renderer.
#
# Step 3 is the delicate part. Prefab's transport ignores any message whose `event.source` is not
# `window.parent` (an identity check in the ext-apps postMessage transport), so the shim cannot
# simply post to itself. It instead calls Prefab's own message listener directly with a plain
# object carrying `source: window.parent` — a WindowProxy we legitimately hold — which satisfies
# the check without patching a single line of Prefab, and without depending on the browser to
# preserve `source` through a synthetic `MessageEvent` dispatch.
#
# Safe to run everywhere: the whole thing is behind `if (!structuredContent && !isError)`, so on
# a host that behaves it never fires. Re-calling is only sound because `show` is read-only and
# idempotent — it creates no result and records no lineage. Never copy this to a tool with side
# effects.
_RECOVERY_SHIM = """
<script>
(function () {
  var capturedArgs = null, toolName = null, recovered = false;

  // Deliver the recovered payload by calling Prefab's own message listener directly, rather
  // than dispatching a synthetic MessageEvent and trusting the browser to preserve `source`
  // through dispatchEvent. Prefab's transport reads exactly two fields — `source` (compared by
  // identity against window.parent) and `data` — so a plain object satisfies it, and the whole
  // path becomes testable outside a browser.
  //
  // Our own listeners are registered through the ORIGINAL addEventListener, captured here
  // before the wrapper is installed, so they never end up in `peerListeners` and cannot recurse.
  var nativeAdd = window.addEventListener.bind(window);
  var nativeRemove = window.removeEventListener.bind(window);
  var peerListeners = [];
  window.addEventListener = function (type, fn, opts) {
    if (type === 'message' && typeof fn === 'function') { peerListeners.push(fn); }
    return nativeAdd(type, fn, opts);
  };
  window.removeEventListener = function (type, fn, opts) {
    var i = peerListeners.indexOf(fn);
    if (i >= 0) { peerListeners.splice(i, 1); }
    return nativeRemove(type, fn, opts);
  };

  function deliver(payload) {
    var event = { data: payload, source: window.parent, origin: window.location.origin };
    for (var i = 0; i < peerListeners.length; i++) {
      try { peerListeners[i](event); } catch (e) { /* one bad listener must not stop the rest */ }
    }
  }

  function onMessage(ev) {
    var d = ev && ev.data;
    if (!d) return;

    // The initialize response names the tool this view belongs to.
    var ti = d.result && d.result.hostContext && d.result.hostContext.toolInfo;
    if (ti && ti.tool && ti.tool.name) { toolName = ti.tool.name; }

    if (d.method === 'ui/notifications/tool-input') {
      capturedArgs = (d.params && d.params.arguments) || {};
      return;
    }
    if (d.method !== 'ui/notifications/tool-result') return;

    var p = d.params || {};
    if (p.structuredContent || p.isError || recovered || !toolName) return;  // healthy host
    recovered = true;
    refetch(p.content || []);
  }

  function refetch(originalContent) {
    var id = 'spelunk-recover-1';
    function onResponse(ev) {
      // Only the host answers a `tools/call` we sent it. `id` is a fixed, guessable string, so
      // without this check any frame able to postMessage into the view could supply its own
      // `structuredContent` and have it rendered as though the host had returned it.
      if (!ev || ev.source !== window.parent) return;
      var d = ev.data;
      if (!d || d.id !== id) return;
      nativeRemove('message', onResponse);
      var sc = d.result && d.result.structuredContent;
      if (!sc) return;
      // Re-deliver as if the host had sent it properly.
      deliver({
        jsonrpc: '2.0',
        method: 'ui/notifications/tool-result',
        params: { content: originalContent, structuredContent: sc, isError: false }
      });
    }
    nativeAdd('message', onResponse);
    window.parent.postMessage({
      jsonrpc: '2.0', id: id, method: 'tools/call',
      params: { name: toolName, arguments: capturedArgs || {} }
    }, '*');
  }

  nativeAdd('message', onMessage);
})();
</script>
"""


def _bootstrap_payload() -> str:
    """A view baked into the renderer HTML so SOMETHING renders before any data arrives.

    Prefab seeds its view state from a ``prefab:initial-data`` script tag, so this shows a
    loading state immediately instead of the renderer's bare "Waiting for content..." — and if
    recovery fails on a broken host, the user sees an explanation rather than a black box.
    """
    with PrefabApp() as app:
        with Column(gap=2):
            Muted("Loading view…")
    # Escape `<` so the JSON can never close the <script> tag it gets embedded in. The payload is
    # a fixed literal today, so nothing depends on this — it is here so that giving the bootstrap
    # view real content later cannot quietly turn this line into an injection point.
    return json.dumps(app.to_json()).replace("<", "\\u003c")


# Plain HTML inside #root, replaced the moment the renderer mounts. It exists so that a failure
# to load the renderer bundle at all — a blocked CSP being the way to get there — shows a
# diagnosis instead of an empty black rectangle. Everything else in this file assumes the
# renderer is running; this is the one thing that does not.
_STATIC_FALLBACK = (
    '<div id="root">'
    '<div style="font:13px system-ui;color:#888;padding:14px;line-height:1.5">'
    "Loading view…"
    '<div style="margin-top:6px;font-size:12px">'
    "If this message stays, the renderer bundle did not load — usually the host blocking "
    "<code>cdn.jsdelivr.net</code>, which the resource declares in its CSP."
    "</div></div></div>"
)


def recovery_renderer_html(mode: str | None = None) -> str:
    """Prefab's renderer page plus Spelunk's bootstrap view, static fallback and recovery shim.

    Built from ``prefab_ui.renderer.get_renderer_html()`` rather than hand-written, so the
    renderer stays whatever the pinned prefab-ui ships and only our additions are ours.
    """
    html = get_renderer_html(mode)
    html = html.replace('<div id="root"></div>', _STATIC_FALLBACK, 1)
    addition = (
        f'<script id="prefab:initial-data" type="application/json">{_bootstrap_payload()}</script>'
        f"{_RECOVERY_SHIM}"
    )
    if "</body>" in html:
        return html.replace("</body>", f"{addition}\n</body>", 1)
    return html + addition


def renderer_csp() -> dict[str, Any]:
    """The renderer's own CSP (jsDelivr for its JS/CSS), in the AppConfig wire shape.

    Serving our own resource means FastMCP no longer attaches Prefab's CSP for us, so `show`
    must declare it or a strict host blocks the renderer bundle.
    """
    csp = get_renderer_csp()
    return {
        key: value
        for key, value in {
            "connect_domains": csp.get("connect_domains"),
            "resource_domains": csp.get("resource_domains"),
            "frame_domains": csp.get("frame_domains"),
        }.items()
        if value
    }


# --------------------------------------------------------------------------- column helpers --- #
def _numeric_columns(columns: list[dict[str, str]]) -> list[str]:
    """Names of the numeric columns, in schema order."""
    return [c["name"] for c in columns if is_numeric_type(c["type"])]


def _require_column(candidate: str, columns: list[dict[str, str]], role: str) -> str:
    """Validate a caller-supplied axis column, naming the real options when it's wrong."""
    names = [c["name"] for c in columns]
    if candidate not in names:
        raise ValueError(f"{role}={candidate!r} is not a column of this result. Available: {names}")
    return candidate


def choose_axes(
    columns: list[dict[str, str]],
    x: str | None = None,
    y: str | None = None,
    series: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Resolve ``(x, measures)`` for a chart, defaulting to the obvious pick.

    Default ``x`` is the first non-numeric column (a label, date or category) and falls back to
    the first column; default ``y`` is the first numeric column. Explicit ``series`` (several
    measures) wins over ``y`` (one). Anything the caller names must actually exist — a chart
    silently plotted against the wrong column is worse than an error.
    """
    if not columns:
        raise ValueError("Cannot chart a result with no columns.")
    names = [c["name"] for c in columns]
    numerics = _numeric_columns(columns)

    if x is not None:
        x_col = _require_column(x, columns, "x")
    else:
        non_numeric = [n for n in names if n not in numerics]
        x_col = non_numeric[0] if non_numeric else names[0]

    if series:
        measures = [_require_column(s, columns, "series") for s in series]
    elif y is not None:
        measures = [_require_column(y, columns, "y")]
    else:
        candidates = [n for n in numerics if n != x_col]
        if not candidates:
            raise ValueError(
                f"No numeric column to plot: this result's columns are {names}. Pass `y=` "
                "explicitly, use kind='table', or aggregate to a numeric measure first."
            )
        measures = [candidates[0]]
    return x_col, measures


# ------------------------------------------------------------------------------ result views --- #
def _titled(title: str | None, build: Any) -> Any:
    """Return ``build()``'s component, wrapped under a heading when *title* is given.

    ``build`` is a callable, not a component, and that is load-bearing: a Prefab component
    attaches itself to whichever container is open **at construction time**. Building it first
    and merely naming it inside a ``with`` block registers nothing and the component vanishes
    from the payload silently — no error, just a missing table.
    """
    if title is None:
        return build()
    with PrefabApp() as app:
        with Column(gap=3):
            H3(title)
            build()
    return app


def result_table(
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    title: str | None = None,
) -> Any:
    """A saved result as a sortable, searchable, paginated table.

    Search and pagination are the renderer's own — they run client-side over rows already in the
    payload, so browsing costs no round trip and creates no result.
    """
    return _titled(title, lambda: DataTable(
        columns=[DataTableColumn(key=c["name"], header=c["name"], sortable=True) for c in columns],
        rows=rows,
        search=len(rows) > 10,
        paginated=len(rows) > 25,
        page_size=25,
    ))


def result_chart(
    kind: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    x: str | None = None,
    y: str | None = None,
    series: list[str] | None = None,
    title: str | None = None,
) -> Any:
    """A saved result as a bar / line / area / scatter / pie chart."""
    if kind not in CHART_KINDS:
        raise ValueError(f"Unknown chart kind {kind!r}. Choose one of {list(CHART_KINDS)}.")
    x_col, measures = choose_axes(columns, x, y, series)
    if kind == "pie" and len(measures) > 1:
        # A pie has one measure by construction — slices of a single whole. Refuse rather than
        # plot measures[0] and leave the rest off: the summary would report every measure as
        # displayed, which is the same silent misstatement the row caps exist to prevent.
        raise ValueError(
            f"A pie chart shows ONE measure, but {len(measures)} were named: {measures}. "
            "Pick one (`y='<column>'`), or use kind='bar'/'line' to compare several."
        )

    def build() -> Any:
        if kind == "pie":
            return PieChart(data=rows, name_key=x_col, data_key=measures[0], height=320)
        if kind == "scatter":
            return ScatterChart(
                data=rows,
                x_axis=x_col,
                y_axis=measures[0],
                series=[ChartSeries(data_key=m, label=m) for m in measures],
                height=320,
            )
        component = {"bar": BarChart, "line": LineChart, "area": AreaChart}[kind]
        return component(
            data=rows,
            x_axis=x_col,
            series=[ChartSeries(data_key=m, label=m) for m in measures],
            height=320,
            show_legend=len(measures) > 1,
        )

    return _titled(title, build)


def interactive_chart(
    kind: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    x: str | None = None,
    y: str | None = None,
    series: list[str] | None = None,
    title: str | None = None,
) -> Any:
    """A chart with a measure picker — switch which column is plotted, client-side.

    The whole interaction runs in the renderer: the picker writes to app state and a ``Condition``
    node swaps which chart is shown. No tool call, no round trip, no new result — so it stays
    within the "a view is not a result" rule even while the user is driving it.

    Rows are put in app ``state`` ONCE and every chart references them as ``{{ rows }}``, rather
    than each chart embedding its own copy: with N measures the naive form multiplies the payload
    by N for no benefit, since all measures already live in the same rows.
    """
    if kind not in CHART_KINDS:
        raise ValueError(f"Unknown chart kind {kind!r}. Choose one of {list(CHART_KINDS)}.")
    x_col, measures = choose_axes(columns, x, y, series)
    if len(measures) < 2:
        # Nothing to switch between — a picker with one option is furniture, not a control.
        return result_chart(kind, columns, rows, x, y, series, title)

    picked = Rx("measure").default(measures[0])

    def one(measure: str) -> Any:
        if kind == "pie":
            return PieChart(data="{{ rows }}", name_key=x_col, data_key=measure, height=320)
        if kind == "scatter":
            return ScatterChart(
                data="{{ rows }}", x_axis=x_col, y_axis=measure,
                series=[ChartSeries(data_key=measure, label=measure)], height=320,
            )
        component = {"bar": BarChart, "line": LineChart, "area": AreaChart}[kind]
        return component(
            data="{{ rows }}", x_axis=x_col,
            series=[ChartSeries(data_key=measure, label=measure)],
            height=320, show_legend=False,
        )

    with PrefabApp(state={"rows": rows, "measure": measures[0]}) as app:
        with Column(gap=3):
            if title:
                H3(title)
            with Select(name="measure"):
                for measure in measures:
                    SelectOption(value=measure, label=measure)
            # One branch per measure: If / Elif... / Else, so exactly one chart is ever shown.
            with If(picked == measures[0]):
                one(measures[0])
            for measure in measures[1:-1]:
                with Elif(picked == measure):
                    one(measure)
            with Else():
                one(measures[-1])
    return app


# ----------------------------------------------------------------------------- profile view --- #
def profile_view(profile: dict, subject: str) -> Any:
    """``profile()`` output as a dashboard: headline metrics, then per-column statistics.

    Numeric and text columns get separate tables — their statistics genuinely differ, and one
    merged table would be half empty on every row.
    """
    stats: dict[str, dict[str, Any]] = profile.get("columns", {})
    numeric_names = [c for c, s in stats.items() if "mean" in s]
    text_names = [c for c in stats if c not in numeric_names]

    def _numeric_row(col: str) -> dict[str, Any]:
        s = stats[col]
        return {
            "column": col, "nulls": s.get("null_rate"), "min": s.get("min"), "max": s.get("max"),
            "mean": s.get("mean"), "std": s.get("std"), "p50": s.get("p50"), "p95": s.get("p95"),
        }

    def _text_row(col: str) -> dict[str, Any]:
        s = stats[col]
        return {
            "column": col, "nulls": s.get("null_rate"), "unique": s.get("unique"),
            "top": s.get("top"), "freq": s.get("freq"),
        }

    num_cols = [
        DataTableColumn(key="column", header="Column", sortable=True),
        DataTableColumn(key="nulls", header="Null rate", sortable=True, format="percent:1"),
        DataTableColumn(key="min", header="Min", sortable=True, format="number:2"),
        DataTableColumn(key="max", header="Max", sortable=True, format="number:2"),
        DataTableColumn(key="mean", header="Mean", sortable=True, format="number:2"),
        DataTableColumn(key="std", header="Std", sortable=True, format="number:2"),
        DataTableColumn(key="p50", header="p50", sortable=True, format="number:2"),
        DataTableColumn(key="p95", header="p95", sortable=True, format="number:2"),
    ]
    txt_cols = [
        DataTableColumn(key="column", header="Column", sortable=True),
        DataTableColumn(key="nulls", header="Null rate", sortable=True, format="percent:1"),
        DataTableColumn(key="unique", header="Distinct", sortable=True, format="number"),
        DataTableColumn(key="top", header="Most common", sortable=True),
        DataTableColumn(key="freq", header="Count", sortable=True, format="number"),
    ]

    with PrefabApp() as app:
        with Column(gap=4):
            H3(f"Profile — {subject}")
            with Row(gap=4):
                Metric(label="Rows", value=f"{profile.get('row_count', 0):,}")
                Metric(label="Columns", value=len(stats))
                Metric(label="Numeric", value=len(numeric_names))
                Metric(label="Text", value=len(text_names))
                Metric(label="Computed in", value=f"{profile.get('elapsed_s', 0)}s")
            if numeric_names:
                with Column(gap=2):
                    H4("Numeric columns")
                    DataTable(
                        columns=num_cols,
                        rows=[_numeric_row(c) for c in numeric_names],
                        search=len(numeric_names) > 10,
                    )
            if text_names:
                with Column(gap=2):
                    H4("Text columns")
                    DataTable(
                        columns=txt_cols,
                        rows=[_text_row(c) for c in text_names],
                        search=len(text_names) > 10,
                    )
    return app


# ----------------------------------------------------------------------------- catalog view --- #
def catalog_view(catalog: dict) -> Any:
    """``catalog()`` output as a browser — the flow list, or one flow's results."""
    if "flows" in catalog:
        flows = catalog["flows"]
        with PrefabApp() as app:
            with Column(gap=4):
                H3("Flows")
                with Row(gap=4):
                    Metric(label="Flows", value=len(flows))
                    Metric(label="Results", value=sum(f["result_count"] for f in flows))
                DataTable(
                    columns=[
                        DataTableColumn(key="flow", header="Flow", sortable=True),
                        DataTableColumn(
                            key="result_count", header="Results", sortable=True, format="number"
                        ),
                    ],
                    rows=flows,
                    search=len(flows) > 10,
                )
                Muted("Show one flow's results with show(kind='catalog', flow='<name>').")
        return app

    results = catalog.get("results", [])
    rows = [
        {
            "name": r["name"],
            "rows": r["row_count"],
            "columns": len(r["columns"]),
            "schema": ", ".join(c["name"] for c in r["columns"]),
            "description": r.get("description") or "",
        }
        for r in results
    ]
    with PrefabApp() as app:
        with Column(gap=4):
            H3(f"Flow — {catalog.get('flow', '?')}")
            with Row(gap=4):
                Metric(label="Results", value=len(results))
                Metric(label="Total rows", value=f"{sum(r['row_count'] for r in results):,}")
            DataTable(
                columns=[
                    DataTableColumn(key="name", header="Result", sortable=True),
                    DataTableColumn(key="rows", header="Rows", sortable=True, format="number"),
                    DataTableColumn(key="columns", header="Cols", sortable=True, format="number"),
                    DataTableColumn(key="description", header="Description"),
                    DataTableColumn(key="schema", header="Schema"),
                ],
                rows=rows,
                search=len(rows) > 10,
                paginated=len(rows) > 25,
            )
    return app


# ----------------------------------------------------------------------------- lineage view --- #
def lineage_view(lineage: dict, subject: str) -> Any:
    """``lineage()`` output as a rendered DAG plus the step table underneath.

    The diagram string is the one ``DuckSession._to_mermaid`` already produces — the same bytes
    ``lineage(render='mermaid')`` hands back as text. Rendering it is a display concern; the
    graph itself stays server-side and deterministic.
    """
    nodes = lineage.get("nodes", [])
    edges = lineage.get("edges", [])
    missing = lineage.get("missing", [])
    rows = [
        {
            "step": i + 1,
            "name": n["name"],
            "kind": n["kind"],
            "description": n.get("description") or "",
            "depends_on": ", ".join(f"{d['flow']}.{d['name']}" for d in n.get("deps", [])),
            "sources": ", ".join(n.get("sources", [])),
        }
        for i, n in enumerate(nodes)
    ]
    with PrefabApp() as app:
        with Column(gap=4):
            H3(f"Lineage — {subject}")
            with Row(gap=4):
                Metric(label="Steps", value=len(nodes))
                Metric(label="Edges", value=len(edges))
                Metric(label="Missing deps", value=len(missing))
            if lineage.get("mermaid"):
                Mermaid(chart=lineage["mermaid"])
            if rows:
                with Column(gap=2):
                    H4("Build order")
                    DataTable(
                        columns=[
                            DataTableColumn(key="step", header="#", sortable=True),
                            DataTableColumn(key="name", header="Result", sortable=True),
                            DataTableColumn(key="kind", header="Kind", sortable=True),
                            DataTableColumn(key="description", header="Description"),
                            DataTableColumn(key="depends_on", header="Depends on"),
                            DataTableColumn(key="sources", header="Sources"),
                        ],
                        rows=rows,
                        search=len(rows) > 10,
                    )
            if missing:
                Muted(f"Dependencies with no recorded lineage: {', '.join(missing)}")
    return app
