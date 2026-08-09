"""The MCP Apps layer: pure ``spec -> hydrated Vega-Lite spec`` builders plus the app page.

No DuckDB, no FastMCP — every function here takes plain dicts and returns plain dicts, so the
whole rendering surface unit-tests without a client or a browser. That property is why a
*declarative* spec was chosen over agent-authored JSX: a spec can be inspected before it runs.

Two halves:

* **Server side** (:func:`validate_spec`, :func:`hydrate`) — the guard. A Vega-Lite spec arrives
  data-free, naming only the columns it plots; the server checks it against the result's real
  schema and injects the rows. This is the display counterpart to ``guard.assert_read_only``:
  the artifact is data, so it is checked *before* it is handed to a renderer, not after it
  silently draws the wrong picture.
* **Client side** (:func:`app_html`, :func:`app_csp`) — a self-contained page that talks to the
  host over raw ``ui/`` JSON-RPC postMessage (an inline ~60-line transport, no ext-apps SDK)
  and draws with ``vega-embed``. Classic scripts throughout: the SDK was the page's one ES
  module import, and on Claude Desktop that page never completed the handshake — a failed
  module import aborts silently — while this dialect, proven by the develop-ui recovery shim
  and minimal_vega_server's ``chart_raw``, renders.

Traps worth knowing:

* **Rendering runs the viewer's browser, not ours.** The bundles come from CDNs at pinned
  versions declared in :func:`app_csp`; the *server* needs no network, the *viewer* does.
* **``ast: true`` is not optional.** Vega compiles expressions with the ``Function``
  constructor by default, which a deny-by-default sandbox CSP refuses. The AST interpreter
  bundled with vega-embed evaluates them without ``eval``, so the page needs no
  ``script-src 'unsafe-eval'``.
* **A field a transform INVENTS is not a column.** ``validate_spec`` collects every ``as``
  output in the spec before checking field references, or a perfectly good
  ``calculate``/``aggregate`` spec would be rejected for naming a column that does not exist yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

# --------------------------------------------------------------------------------- pins --- #
# Pinned exactly, for the same reason the old Prefab renderer pinned its bundle: the version here
# selects the JavaScript users' browsers fetch. A floating major would change what renders
# without changing anything in this repo.
VEGA_VERSION = "5.30.0"
VEGA_LITE_VERSION = "5.21.0"
VEGA_EMBED_VERSION = "6.26.0"
# The `ui/` protocol dialect the inline transport speaks (ui/initialize's protocolVersion).
# There is no ext-apps SDK pin any more: the page carries its own ~60-line postMessage
# transport (`_TRANSPORT_SCRIPT`) instead of importing the SDK from a CDN. That import was the
# one ES module on the page, and a failed module import aborts silently — on Claude Desktop the
# SDK-built page never completed the handshake, while this raw dialect (proven first by the
# develop-ui recovery shim, then by minimal_vega_server's chart_raw) renders.
UI_PROTOCOL_VERSION = "2026-01-26"

_JSDELIVR = "https://cdn.jsdelivr.net"

VEGA_URI = "ui://spelunk/vega.html"

# Vega-Lite holds every row in the browser and re-renders on interaction, so the ceiling is the
# viewer's memory, not ours. It is far above the old Prefab chart cap (200) because a spec can
# aggregate client-side, but it is still a ceiling — and, like `rows_for_display`, passing it
# ERRORS rather than truncating. A silently shortened chart is a picture that misstates the data.
VEGA_MAX_ROWS = 5000

# The OTHER ceiling, and the one that actually bites: Claude.ai and Claude Desktop divert a tool
# result over ~150,000 characters to their code-execution sandbox's filesystem and hand the app a
# POINTER instead of the payload, so the view never hydrates — it renders nothing, with no error
# anywhere. (See "App doesn't render when tool results are large" in Claude's MCP Apps
# troubleshooting.) A hydrated spec carries every plotted row inline, so the row cap alone does
# not bound it: 5000 rows x 3 columns is ~238,000 characters, comfortably past the threshold.
# Budgeted below the limit because the measurement here cannot be exact — the host counts the
# whole result envelope, not just the two halves we build.
#
# This is a HOST limit, not a Vega one, which is why it is invisible in MCP Inspector: no
# sandbox, no diversion, so the identical payload renders there and fails in Claude.
PAYLOAD_MAX_CHARS = 120_000

# `outputSchema` describes **structuredContent** — the half the HOST reads and validates — NOT
# the text summary the model reads. Getting that backwards ships a tool that fails on every call
# against any host that honours the schema (Claude Desktop does): FastMCP requires
# structured_content whenever an output_schema exists, the host validates one against the other,
# and the mismatch surfaces to the user as "missing a required <field> property" with nothing
# rendered. The summary's shape is a contract too, but it is prose in the tool description and is
# asserted directly in the tests — it has no business in here.
VISUAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spec": {
            "type": "object",
            "description": (
                "A complete Vega-Lite spec with the result's rows already inlined under "
                "data.values — ready to hand straight to vega-embed, with no further fetching."
            ),
        },
    },
    "required": ["spec"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------------------ walking --- #
def _walk(node: Any) -> Any:
    """Yield every dict nested anywhere in *node* (including *node* itself)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# The keys under which one view holds ANOTHER view. `layer`/`concat` hold a list of them;
# `facet` and `repeat` wrap their single child in `spec`. Everything else in a view dict —
# `transform`, `encoding`, `facet`'s own definition, `params` — belongs to the view itself.
_CHILD_VIEW_KEYS = frozenset({"layer", "hconcat", "vconcat", "concat", "spec"})


def _own_nodes(view: dict) -> list[dict]:
    """Every dict belonging to THIS view's own definition, stopping at its nested views.

    The unit of scope. Vega-Lite's data flows down a view tree — a child sees its parent's
    transforms, a parent and its siblings never see a child's — so a check that walks the whole
    spec flat cannot tell "this layer invented that column" from "some other layer did".
    """
    nodes = [view]
    for key, value in view.items():
        if key not in _CHILD_VIEW_KEYS:
            nodes.extend(_walk(value))
    return nodes


def _child_views(view: dict) -> list[dict]:
    """The views nested directly inside *view*, in no particular order."""
    children: list[dict] = []
    for key in ("layer", "hconcat", "vconcat", "concat"):
        value = view.get(key)
        if isinstance(value, list):
            children.extend(c for c in value if isinstance(c, dict))
    inner = view.get("spec")  # facet / repeat wrap their one view here
    if isinstance(inner, dict):
        children.append(inner)
    return children


def _data_blocks(node: Any) -> Any:
    """Yield every value that sits under a ``data`` key, at any depth.

    Scoped to ``data`` deliberately rather than hunting for ``url`` everywhere: the ``image``
    mark takes a legitimate ``url`` ENCODING channel, and rejecting that would be a false
    positive on a valid spec.
    """
    for holder in _walk(node):
        block = holder.get("data")
        if isinstance(block, dict):
            yield block
        elif isinstance(block, list):
            for item in block:
                if isinstance(item, dict):
                    yield item


def _produced_fields(nodes: Iterable[dict]) -> set[str]:
    """Column names the transforms in *nodes* invent, which therefore need no source column.

    Deliberately permissive. Every Vega-Lite transform that creates a column names it with
    ``as`` (``calculate``, ``aggregate``, ``window``, ``bin``, ``timeUnit``, ``density``,
    ``regression``, …), so collecting every ``as`` covers them without enumerating the list —
    which matters because that list grows with each Vega-Lite release. ``fold`` is the one
    transform that needs a special case, because it is the one that names outputs without
    ``as``.

    Missing an output here is the expensive direction: it makes the guard refuse a chart that
    would have drawn correctly. That asymmetry is why this side stays permissive while
    :func:`_transform_inputs` can afford to be incomplete.
    """
    produced: set[str] = set()
    for node in nodes:
        alias = node.get("as")
        if isinstance(alias, str):
            produced.add(alias)
        elif isinstance(alias, list):
            produced.update(a for a in alias if isinstance(a, str))
        if "fold" in node and "as" not in node:
            # `as` REPLACES these defaults rather than adding to them, so excusing key/value
            # beside a custom `as` would wave through a reference to a column fold never made.
            produced.update({"key", "value"})
    return produced


def _transform_inputs(nodes: Iterable[dict]) -> set[str]:
    """Columns a transform reads under a key OTHER than ``field``.

    Most of the grammar spells its input ``field``, which :func:`_field_refs` picks up wholesale.
    These four do not: ``fold`` and ``flatten`` take a LIST of source columns, ``pivot`` names
    the column whose values become new columns, and the ``value`` beside it names the column
    they are filled from. A typo in any of them is the same silently blank chart a misspelled
    encoding field is.

    Only keys that cannot mean anything else are read. ``value`` is taken ONLY beside a
    ``pivot`` — everywhere else in Vega-Lite it holds a literal, and ``{"color": {"value":
    "steelblue"}}`` would otherwise demand a column named ``steelblue``. ``stack`` is left out
    for the same reason: in an encoding it holds ``"normalize"``, not a column name. Being
    incomplete here only means a typo slips through; being wrong means refusing a valid chart,
    so the bar for adding a key is that it can never hold anything but a field name.
    """
    refs: set[str] = set()
    for node in nodes:
        for key in ("fold", "flatten", "groupby"):
            value = node.get(key)
            if isinstance(value, list):
                refs.update(v for v in value if isinstance(v, str))
        refs |= _pivot_inputs([node])
    return refs


def _pivot_inputs(nodes: Iterable[dict]) -> set[str]:
    """What a ``pivot`` itself READS — checkable even when nothing after it is.

    Its own field, the ``value`` filled from, and the ``groupby`` columns that survive it. All
    three are ordinary columns of the incoming data, so a typo in one is worth catching even
    though the pivot's OUTPUT names can never be checked.
    """
    refs: set[str] = set()
    for node in nodes:
        pivot = node.get("pivot")
        if not isinstance(pivot, str):
            continue
        refs.add(pivot)
        if isinstance(node.get("value"), str):
            refs.add(node["value"])
        groupby = node.get("groupby")
        if isinstance(groupby, list):
            refs.update(g for g in groupby if isinstance(g, str))
    return refs


def _unresolved_refs(
    view: dict,
    known: set[str],
    produced: set[str] = frozenset(),  # type: ignore[assignment]
    pivoted: bool = False,
) -> set[str]:
    """Field references in *view* and its descendants that nothing can account for.

    Walks the VIEW TREE rather than the raw dict tree, because Vega-Lite's data flows down it: a
    child sees its parent's transforms, a parent and its siblings never see a child's. Checking
    flat let one layer's `as` excuse a typo in the next, and let a `pivot` anywhere switch the
    whole spec's checking off — a layered pivot dashboard went entirely unvalidated.

    Three things stop a reference being adjudicable, and each stops it only where it applies:

    * **A view with its own ``data``** reads its own rows, so the result's columns say nothing
      about it — the reference-line layer with two literal values is the ordinary case. (A
      top-level ``data`` is refused outright before this runs; the server owns that one.)
    * **A ``pivot``** mints one column per distinct VALUE of its input, so every name after it
      comes from the data rather than the grammar. What the pivot reads is still checked; the
      rest of that subtree is not, because there is no schema that could confirm it.
    * **A transform output**, which by definition has no source column — inherited downward, so
      a parent's ``calculate`` covers its children and a sibling's does not.

    Passing an empty *known* turns the same walk into "every column this spec REQUIRES", which
    is how :func:`required_columns` reuses it. That is deliberate: the guard and the drift check
    then cannot disagree about what a chart needs, and a chart `replay` calls stale is one a
    redraw would genuinely refuse.
    """
    if "data" in view:
        return set()
    nodes = _own_nodes(view)
    produced = produced | _produced_fields(nodes)
    pivoted = pivoted or any(isinstance(n.get("pivot"), str) for n in nodes)
    checkable = _pivot_inputs(nodes) if pivoted else _field_refs(nodes)
    missing = {f for f in checkable if f not in known and f not in produced}
    for child in _child_views(view):
        missing |= _unresolved_refs(child, known, produced, pivoted)
    return missing


def _field_refs(nodes: Iterable[dict]) -> set[str]:
    """Every source column *nodes* read, as ``{field}`` names.

    Covers encoding channels and the ``field``-taking transforms alike, since both spell it
    ``field``, plus the handful that spell it otherwise (:func:`_transform_inputs`). A ``field``
    given as a dict (Vega-Lite's repeat/datum forms) is skipped — it does not name a column
    directly. Nor does an encoding's ``sort`` array, which holds the VALUES to order a category
    axis by: reading ``sort: ["Jan", "Feb", "Mar"]`` as column names refused every chart with a
    custom category order, naming the months as missing columns.

    **Boundary: a column named only inside an EXPRESSION STRING is invisible here** — a
    ``{"filter": "datum.revnue > 0"}`` or ``{"calculate": "datum.regoin", "as": ...}`` passes
    :func:`validate_spec` untouched and is absent from :func:`required_columns`, so the same typo
    that is refused in an encoding renders an empty chart from a transform. Deliberate, not
    overlooked: extracting ``datum.<name>`` is easy, but deciding which of those names must exist
    is not. Vega-Lite invents field names *implicitly* — an encoding-level ``aggregate`` yields
    ``sum_revenue``, ``bin`` yields ``bin_maxbins_10_x``/``_end``, ``timeUnit`` yields
    ``yearmonth_date`` — and expressions that run after those stages (an
    ``encoding.*.condition.test``) legitimately reference them. Demanding them of the result
    would reject working charts, which VIS-007's falsifier calls out as worse than no guard,
    and the only way to avoid it is enumerating a naming scheme that changes with each release —
    exactly the fragility :func:`_produced_fields` was written to sidestep. Widening this later
    is safe rather than lossy: ``_spelunk_meta.visuals`` stores the spec verbatim beside its
    ``fields``, so the stale rows backfill with an ``UPDATE`` over the stored specs.
    """
    nodes = list(nodes)
    refs = _transform_inputs(nodes)
    for node in nodes:
        field = node.get("field")
        if isinstance(field, str):
            refs.add(field)
    return refs


# ---------------------------------------------------------------------------- validation --- #
def validate_spec(spec: Any, columns: list[dict[str, str]]) -> dict:
    """Check a data-free Vega-Lite spec against a result's real schema. Raises ``ValueError``.

    Four refusals, each one a silent (or agent-invisible) failure if it were left to the
    renderer:

    1. **``data`` at the top level** — the server owns the data. A spec that carried its own
       would render something other than the result it claims to display.
    2. **``data.url`` anywhere** — the one genuine egress channel in an otherwise declarative
       artifact: it makes the viewer's browser fetch a host we never see.
    3. **A field that is not a column** and is not produced by the spec's own transforms.
       Vega-Lite draws an empty or subtly wrong chart for a missing field without erroring,
       which is precisely the mis-plot this refuses to ship. Checked per VIEW, so one layer's
       transforms cannot excuse another layer's typo — see :func:`_unresolved_refs` for the
       scoping and :func:`_field_refs` for which keys count as a reference at all.
    4. **A selection param at the top level of a multi-view spec** — a Vega-Lite grammar
       limitation (selections live in unit specs only) that compiles to duplicate signals and
       kills the chart in the renderer, after the tool has already returned success.
    """
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as exc:
            raise ValueError(f"`spec` is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError(f"`spec` must be a Vega-Lite spec object, got {type(spec).__name__}.")
    if not spec:
        raise ValueError("`spec` is empty — pass a Vega-Lite spec describing the chart to draw.")

    # `data.url` is checked BEFORE the top-level `data` refusal so the more specific message
    # wins: a spec with `data: {url: ...}` trips both, and "remove the data key" would send the
    # author off to fix the wrong thing.
    for block in _data_blocks(spec):
        if "url" in block:
            raise ValueError(
                "`data.url` is not allowed: it would make the viewer's browser fetch an "
                "external host. Every row must come from a saved result."
            )
    if "data" in spec:
        raise ValueError(
            "`spec` must not carry its own top-level `data` — the rows come from the result "
            "named by `name`, and the server injects them. Remove the `data` key."
        )

    # A SELECTION param at the top level of a multi-view spec is a grammar limitation, not a
    # style choice: Vega-Lite only allows selections inside UNIT specs, and the compiled chart
    # dies in the RENDERER with `Duplicate signal name: "<param>_tuple"` — after the tool has
    # already returned success, so the agent never sees the failure and only the human sees the
    # wreckage. Verified against vega-lite 5.21, 5.23 and 6.4 (a single-layer spec with one
    # top-level select param fails on the FIRST compile), so no pin bump fixes it — refuse it
    # here, where the message reaches the author. Variable params (no `select`) are legal at
    # the top level of any spec and pass untouched.
    composite = [k for k in ("layer", "hconcat", "vconcat", "concat", "facet", "repeat")
                 if k in spec]
    if composite:
        selections = sorted(
            p["name"] for p in spec.get("params", [])
            if isinstance(p, dict) and "select" in p and isinstance(p.get("name"), str)
        )
        if selections:
            raise ValueError(
                f"Selection param(s) {selections} cannot sit at the top level of a "
                f"multi-view spec (this one has `{composite[0]}`): Vega-Lite only allows "
                "selections inside unit specs, and the chart dies in the renderer with "
                "`Duplicate signal name` after the tool has already succeeded. Move the "
                "`params` array into the view that uses it — for a layered chart, the first "
                "entry of `layer`."
            )

    known = {c["name"] for c in columns}
    missing = sorted(_unresolved_refs(spec, known))
    if missing:
        raise ValueError(
            f"Spec references column(s) {missing} that the result does not have. "
            f"Available columns: {sorted(known)}."
        )
    return spec


def hydrate(
    spec: dict,
    rows: list[dict[str, Any]],
    title: str | None = None,
) -> dict:
    """Inject the rows into a validated spec and return the spec the renderer receives.

    The stored/authored spec stays data-free; hydration happens here, server-side, so the page
    never has to fetch anything. That is what keeps a view free of round trips: everything the
    renderer needs arrives in one payload.

    Width is left alone when the spec sets it, or when the spec is a multi-view composition
    (``facet``/``concat``/``repeat``), where Vega-Lite rejects ``"container"``.
    """
    major = VEGA_LITE_VERSION.split(".", maxsplit=1)[0]
    out = dict(spec)
    out.setdefault("$schema", f"https://vega.github.io/schema/vega-lite/v{major}.json")
    out["data"] = {"values": rows}
    if title and "title" not in out:
        out["title"] = title
    composed = any(k in out for k in ("facet", "hconcat", "vconcat", "concat", "repeat"))
    if not composed and "width" not in out:
        out["width"] = "container"
        out.setdefault("autosize", {"type": "fit", "contains": "padding"})
    return out


def assert_payload_fits(hydrated: dict, row_count: int, name: str) -> int:
    """Refuse a payload the host will divert instead of delivering. Returns its size in chars.

    Same stance as the row cap and for a sharper reason: past ~150k characters Claude writes the
    result to its sandbox filesystem and the app receives a pointer, so the view silently renders
    nothing. Erroring here turns an invisible host behaviour into a message that names the real
    size and the way out.
    """
    size = len(json.dumps(hydrated, default=str))
    if size > PAYLOAD_MAX_CHARS:
        raise ValueError(
            f"The chart payload for {name!r} is {size:,} characters ({row_count:,} rows inlined), "
            f"over the {PAYLOAD_MAX_CHARS:,} budget. Nothing was truncated. Claude diverts a tool "
            "result this large to its sandbox filesystem and hands the view a pointer instead of "
            "the data, so the chart would render blank with no error. Aggregate, bin, or top-N "
            "with `query` first and draw that result — or plot fewer columns."
        )
    return size


def required_columns(spec: dict) -> list[str]:
    """The result columns a spec NEEDS, for the store to compare against later.

    Field references minus whatever the spec's own ``transform`` block invents — a produced
    field must not be demanded of the result, or a perfectly good chart would be reported stale
    the moment anyone checked it.

    Unlike :func:`spec_fields`, this takes no column list: it describes what the spec requires,
    not what it happens to find. That is the difference that makes it storable — the answer must
    stay true when the result's schema changes underneath it, which is the whole point of
    checking it again after a rebuild.

    Sees exactly what :func:`validate_spec` sees, on purpose — both are :func:`_unresolved_refs`,
    one with the result's columns and one with none — so the drift `replay` reports is the same
    set of references the guard enforces, and a chart can never be reported stale for a column a
    redraw would happily draw. It therefore inherits that walk's boundaries (expression strings,
    anything downstream of a ``pivot``, any view carrying its own ``data``), and inherits them in
    *stored* form: these values are computed once at authoring time and never recomputed on
    redraw, so a later widening improves new rows only until the existing ones are backfilled
    from the specs stored beside them.
    """
    return sorted(_unresolved_refs(spec, set()))


def spec_fields(spec: dict, columns: list[dict[str, str]]) -> list[str]:
    """The result columns the spec actually plots, for the text summary.

    The summary must describe what is on screen; a spec can read a subset of a wide result, and
    reporting all the columns would overstate what the reader is looking at.
    """
    known = {c["name"] for c in columns}
    return sorted(f for f in _field_refs(_walk(spec)) if f in known)


# -------------------------------------------------------------------------------- the app --- #
def app_csp() -> dict[str, Any]:
    """The app page's CSP, in FastMCP's ``ResourceCSP`` wire shape.

    Belongs on the RESOURCE, not the tool: that is where the host reads it from. Declaring it on
    the tool instead yields a resource with no policy, the host blocks every bundle, and the
    frame stays blank with no error to debug.

    Only ``resource_domains`` — the page loads scripts and never calls out. There is deliberately
    no ``connect_domains``: a view that could fetch would be a view that could exfiltrate, and
    ``data.url`` is already refused server-side for the same reason. jsDelivr alone: the vega
    bundles are the only external load left now that the transport is inline.
    """
    return {"resource_domains": [_JSDELIVR]}


# Plain HTML shown until the app mounts. It exists so a failure to load the bundles at all — a
# host refusing the CSP being the way to get there — shows a diagnosis instead of a blank frame.
_FALLBACK = (
    '<div id="status">Loading chart…'
    '<div class="hint">If this stays, the chart bundles did not load — usually the host '
    "blocking <code>cdn.jsdelivr.net</code> or <code>unpkg.com</code>, which this view "
    "declares in its CSP.</div></div>"
)

_STYLE = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 12px; font: 13px/1.5 system-ui, sans-serif; }
#status { color: #888; }
#status .hint { margin-top: 6px; font-size: 12px; }
#status.error { color: #b00020; }
@media (prefers-color-scheme: dark) { #status.error { color: #ff6b6b; } }
/* An app iframe with zero height is invisible, and it is one of the two causes Claude's own
   troubleshooting page names first. The deadlock to avoid: `width: "container"` measures 0 in a
   frame the host has not sized yet, the chart draws at 0x0, we then report ~0 height back, and
   the host keeps the frame collapsed. A floor on both axes breaks the cycle before it starts. */
#chart { width: 100%; min-width: 320px; min-height: 320px; }
#chart .vega-embed { width: 100%; }
"""

# A CLASSIC script, deliberately, and loaded FIRST: it runs even when the module below never
# does. A failed `import` — blocked origin, wrong MIME, network error — aborts the whole module
# silently: no handler is ever attached, the page just sits on its fallback text, and there is
# nothing obviously wrong to see. This captures that case and every later uncaught error into the
# visible status line, so the page diagnoses ITSELF instead of needing someone with DevTools open.
_BOOT_SCRIPT = """
window.__spelunk = { build: "__APP_BUILD__", stage: "loading chart bundles", errors: [] };
window.__spelunkStatus = function (text, isError) {
  var el = document.getElementById("status");
  if (!el) { return; }
  el.hidden = false;
  el.className = isError ? "error" : "";
  el.textContent = text;
  if (isError) {
    var d = document.createElement("div");
    d.className = "hint";
    d.textContent = "build " + window.__spelunk.build
      + " | stage: " + window.__spelunk.stage
      + (window.__spelunk.errors.length ? " | " + window.__spelunk.errors.join(" | ") : "");
    el.appendChild(d);
  }
};
window.addEventListener("error", function (e) {
  // A failed <script src> or module import surfaces here with the element as the target, which
  // is the only signal that distinguishes "blocked bundle" from "bundle ran and then threw".
  var what = (e && e.target && e.target.src) ? ("failed to load " + e.target.src)
           : (e && e.message ? e.message : "script error");
  window.__spelunk.errors.push(what);
  window.__spelunkStatus("This view could not start.", true);
}, true);
window.addEventListener("unhandledrejection", function (e) {
  var r = e && e.reason;
  window.__spelunk.errors.push("unhandled: " + (r && r.message ? r.message : String(r)));
  window.__spelunkStatus("This view could not start.", true);
});
"""

# The transport: a raw `ui/` JSON-RPC client over postMessage, presenting the same surface the
# app script used when it came from the ext-apps SDK (`connect` / `getHostContext` /
# `sendSizeChanged` / `callServerTool` / `ontoolinput` / `ontoolresult` /
# `onhostcontextchanged`). Inline and CLASSIC, deliberately: the SDK arrived as the page's one
# ES module import, and a failed module import aborts silently — which on Claude Desktop is
# exactly what happened, the handshake never completing while this dialect (the same one the
# develop-ui recovery shim verified on the wire, message name for message name) renders. The
# `ev.source !== window.parent` identity check is the security half: a sandboxed frame can be
# postMessage'd by anything, and without it a hostile frame could feed the view a payload or
# answer its `tools/call`.
_TRANSPORT_SCRIPT = """
window.SpelunkApp = function (appInfo) {
  var self = this;
  this.ontoolinput = null;
  this.ontoolresult = null;
  this.onhostcontextchanged = null;
  this._hostContext = null;
  this._hostInfo = null;
  this._pending = {};
  this._nextId = 2;

  window.addEventListener("message", function (ev) {
    if (ev.source !== window.parent) { return; }
    var d = ev.data;
    if (!d || d.jsonrpc !== "2.0") { return; }
    if (d.id !== undefined && (d.result !== undefined || d.error !== undefined)) {
      var waiter = self._pending[d.id];
      if (!waiter) { return; }
      delete self._pending[d.id];
      if (d.error) { waiter.reject(new Error(d.error.message || "host error")); }
      else { waiter.resolve(d.result); }
      return;
    }
    var params = d.params || {};
    if (d.method === "ui/notifications/tool-input") {
      if (self.ontoolinput) { self.ontoolinput(params); }
    } else if (d.method === "ui/notifications/tool-result") {
      if (self.ontoolresult) { self.ontoolresult(params); }
    } else if (d.method === "ui/notifications/host-context-changed") {
      // The SDK spreads the params straight into its stored context; some hosts nest the
      // partial under `hostContext`. Accept both, so a theme flip lands either way.
      var partial = params.hostContext || params;
      self._hostContext = Object.assign({}, self._hostContext, partial);
      if (self.onhostcontextchanged) { self.onhostcontextchanged(params); }
    }
  });

  this._post = function (msg) { window.parent.postMessage(msg, "*"); };

  this._request = function (method, params) {
    var id = self._nextId++;
    return new Promise(function (resolve, reject) {
      self._pending[id] = { resolve: resolve, reject: reject };
      self._post({ jsonrpc: "2.0", id: id, method: method, params: params });
      setTimeout(function () {
        if (self._pending[id]) {
          delete self._pending[id];
          reject(new Error(method + ": no response from the host after 30s"));
        }
      }, 30000);
    });
  };

  this.connect = function () {
    return self._request("ui/initialize", {
      appInfo: appInfo,
      appCapabilities: {},
      protocolVersion: "__UI_PROTOCOL_VERSION__",
    }).then(function (result) {
      self._hostContext = (result && result.hostContext) || null;
      self._hostInfo = (result && result.hostInfo) || null;
      self._post({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
    });
  };

  this.getHostContext = function () { return self._hostContext; };

  this.callServerTool = function (call) {
    return self._request("tools/call", { name: call.name, arguments: call.arguments || {} });
  };

  this.sendSizeChanged = function () {
    var root = document.documentElement;
    self._post({
      jsonrpc: "2.0", method: "ui/notifications/size-changed",
      params: { width: root ? root.scrollWidth : undefined,
                height: (root && root.scrollHeight) || 360 },
    });
  };
};
"""

# `ast: true` routes Vega's expression evaluation through the bundled AST interpreter instead of
# the Function constructor, so the page needs no `script-src 'unsafe-eval'` from the host.
#
# The structuredContent recovery branch covers ext-apps#696 (Claude Desktop strips
# structuredContent from the result it forwards to a view) AND the >150k-character sandbox
# diversion, which present identically to the app; the tools/call proxy is unaffected by both.
# `ontoolinput` captures the arguments and `callServerTool` re-fetches — both methods of the
# inline transport above. It is sound only because `visual` is idempotent and read-only.
# Temporary: `grep -rn "ext-apps#696"` finds every line to delete.
#
# Every branch records WHICH stage it reached, because these failures are otherwise
# indistinguishable from one another AND from success-into-an-invisible-frame: a blocked bundle,
# a handshake that never completes, a result that arrives without structuredContent, and a chart
# drawn at zero size all look the same to someone staring at an empty rectangle.
_APP_SCRIPT = """
// A CLASSIC script, like everything else on the page — the transport is inline
// (window.SpelunkApp), so there is no module import whose silent failure could take the whole
// script down. The boot script still owns the "still loading" stage: a blocked vega bundle
// surfaces there, not here.
const App = window.SpelunkApp;

const boot = window.__spelunk;
const setStatus = window.__spelunkStatus;
const chartEl = document.getElementById("chart");
let lastArgs = null, lastSpec = null, recovered = false;
let currentView = null, drawing = null, lastTheme = null;

boot.stage = "transport ready";
if (typeof vegaEmbed !== "function") {
  setStatus("The chart library did not load.", true);
}

const app = new App({ name: "Spelunk Vega", version: "1.0.0" });

function fail(message) { setStatus(message, true); }

function hostTheme() {
  try { return app.getHostContext()?.theme ?? null; } catch (e) { return null; }
}

function themeConfig() {
  const dark = hostTheme() === "dark";
  if (!dark) return {};
  return {
    background: "transparent",
    title: { color: "#e6e6e6" },
    style: { "guide-label": { fill: "#b8b8b8" }, "guide-title": { fill: "#e6e6e6" } },
    axis: { domainColor: "#555", gridColor: "#333", tickColor: "#555" },
    legend: { labelColor: "#b8b8b8", titleColor: "#e6e6e6" },
    view: { stroke: "#444" },
  };
}

// Draws are SERIALIZED through one promise chain, and each embed tears the previous view down
// first. Both halves are needed, and skipping either breaks any spec carrying a `params`
// selection:
//
//   Vega registers a signal per selection (`<name>_tuple` and friends) in a GLOBAL-per-element
//   namespace. Put a second view on the same element and the names collide — vegaEmbed throws
//   `Duplicate signal name: "<name>_tuple"` and the chart dies, having rendered nothing. A plain
//   chart with no params survives the same double-embed silently, which is what makes this a
//   bug you only meet once someone writes an interactive spec.
//
// Two callers race here: the tool result draws once, and `onhostcontextchanged` (theme, locale)
// can fire again while that first embed is still awaiting.
async function draw(spec) {
  if (!spec) { fail("No chart spec arrived from the server."); return; }
  lastSpec = spec;
  drawing = (drawing || Promise.resolve()).then(() => embed(spec), () => embed(spec));
  return drawing;
}

async function embed(spec) {
  boot.stage = "drawing";
  try {
    // Tear down the previous view before building another. `finalize()` releases its signals and
    // listeners; clearing the container drops the DOM vega-embed left behind.
    if (currentView) {
      try { currentView.finalize(); } catch (e) { /* already gone */ }
      currentView = null;
    }
    chartEl.innerHTML = "";
    // `width: "container"` needs a container that has already been laid out. In a frame the host
    // has not sized yet it measures 0 and the chart draws invisibly, so fall back to a concrete
    // width for this embed rather than shipping a 0x0 picture.
    if (spec.width === "container" && !chartEl.clientWidth) {
      spec = Object.assign({}, spec, { width: 600 });
    }
    lastTheme = hostTheme();
    const result = await vegaEmbed(chartEl, spec, {
      ast: true,                 // CSP-safe expression interpreter; see module docstring
      actions: { export: true, source: false, compiled: false, editor: false },
      config: themeConfig(),
    });
    currentView = (result && result.view) || null;
    document.getElementById("status").hidden = true;
    boot.stage = "drawn";
    try { app.sendSizeChanged(); } catch (e) { /* host may not accept a size hint */ }
  } catch (err) {
    boot.errors.push(err && err.message ? err.message : String(err));
    fail("Could not render the chart.");
  }
}

app.ontoolinput = (params) => { lastArgs = (params && params.arguments) || null; };

app.ontoolresult = async (result) => {
  boot.stage = "tool result received";
  if (result?.structuredContent?.spec) { await draw(result.structuredContent.spec); return; }
  if (result?.isError) { fail("The server reported an error building this chart."); return; }
  // ext-apps#696, or the >150k-character sandbox diversion — identical from here. Ask for the
  // payload again through the tools/call proxy, which neither affects.
  boot.stage = "result arrived WITHOUT structuredContent; re-fetching";
  if (recovered || !lastArgs) {
    fail("The chart data did not reach this view."
      + (lastArgs ? "" : " No tool arguments arrived either, so it cannot be re-fetched."));
    return;
  }
  recovered = true;
  try {
    const again = await app.callServerTool({ name: "visual", arguments: lastArgs });
    if (!again?.structuredContent?.spec) {
      fail("Re-fetching the chart data returned nothing. If the result is large, aggregate it "
         + "with `query` first — Claude diverts oversized tool results away from the view.");
      return;
    }
    await draw(again.structuredContent.spec);
  } catch (err) {
    boot.errors.push("re-fetch: " + (err && err.message ? err.message : String(err)));
    fail("Could not recover the chart data from the server.");
  }
};

app.onhostcontextchanged = () => {
  // Theme lives in the host context, and the config is baked in at embed time, so a theme flip
  // means re-embedding rather than restyling in place. Gate on the theme ACTUALLY changing:
  // this notification also carries locale and display-mode changes, and the host fires one on
  // connect, so redrawing unconditionally means re-embedding for no reason.
  if (lastSpec && hostTheme() !== lastTheme) { draw(lastSpec); }
};

boot.stage = "connecting to host";
app.connect().then(() => {
  // Only while still waiting: connect resolves asynchronously, and on a host that delivered
  // the tool result first this would otherwise overwrite a later stage (or a failure status)
  // with "waiting".
  if (boot.stage === "connecting to host") {
    boot.stage = "connected; waiting for the tool result";
    setStatus("Connected. Waiting for chart data\\u2026", false);
  }
}, (err) => {
  boot.errors.push("connect: " + (err && err.message ? err.message : String(err)));
  fail("Could not connect to the host.");
});
"""


def app_build() -> str:
    """A short fingerprint of the exact page this server would serve.

    The single most expensive unknown when a view misbehaves is whether the host is running the
    code you just changed or a cached copy of the last one. Stamping the page and ALSO reporting
    the stamp in the tool summary settles it in one look: same stamp on both sides means the host
    is current, different means it cached and no amount of editing will change what you see.
    """
    material = "|".join(
        [_STYLE, _BOOT_SCRIPT, _TRANSPORT_SCRIPT, _APP_SCRIPT, VEGA_VERSION,
         VEGA_LITE_VERSION, VEGA_EMBED_VERSION, UI_PROTOCOL_VERSION]
    )
    return hashlib.sha256(material.encode()).hexdigest()[:8]


def app_html() -> str:
    """The complete app page: a Vega-Lite renderer wired to the host through the ext-apps SDK.

    Self-contained apart from the pinned CDN bundles named in :func:`app_csp`. Hand-written
    rather than generated, because unlike the Prefab renderer it replaces there is no upstream
    page to inherit — which is also why it is far shorter.
    """
    # `.replace`, not `%` — the script is JavaScript, and a stray `%` in it (a CSS width, a
    # modulo) would blow up percent-formatting at import time.
    transport = _TRANSPORT_SCRIPT.replace("__UI_PROTOCOL_VERSION__", UI_PROTOCOL_VERSION)
    boot = _BOOT_SCRIPT.replace("__APP_BUILD__", app_build())
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>Spelunk chart</title>\n"
        f"<style>{_STYLE}</style>\n"
        f"<script>{boot}</script>\n"
        f"<script>{transport}</script>\n"
        f'<script src="{_JSDELIVR}/npm/vega@{VEGA_VERSION}"></script>\n'
        f'<script src="{_JSDELIVR}/npm/vega-lite@{VEGA_LITE_VERSION}"></script>\n'
        f'<script src="{_JSDELIVR}/npm/vega-embed@{VEGA_EMBED_VERSION}"></script>\n'
        "</head>\n<body>\n"
        f"{_FALLBACK}\n"
        '<div id="chart"></div>\n'
        f"<script>{_APP_SCRIPT}</script>\n"
        "</body>\n</html>\n"
    )
