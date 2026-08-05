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
  host through ``@modelcontextprotocol/ext-apps`` and draws with ``vega-embed``.

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

import json
from typing import Any

# --------------------------------------------------------------------------------- pins --- #
# Pinned exactly, for the same reason the old Prefab renderer pinned its bundle: the version here
# selects the JavaScript users' browsers fetch. A floating major would change what renders
# without changing anything in this repo.
VEGA_VERSION = "5.30.0"
VEGA_LITE_VERSION = "5.21.0"
VEGA_EMBED_VERSION = "6.26.0"
EXT_APPS_VERSION = "0.4.0"

_JSDELIVR = "https://cdn.jsdelivr.net"
_UNPKG = "https://unpkg.com"

VEGA_URI = "ui://spelunk/vega.html"

# Vega-Lite holds every row in the browser and re-renders on interaction, so the ceiling is the
# viewer's memory, not ours. It is far above the old Prefab chart cap (200) because a spec can
# aggregate client-side, but it is still a ceiling — and, like `rows_for_display`, passing it
# ERRORS rather than truncating. A silently shortened chart is a picture that misstates the data.
VEGA_MAX_ROWS = 5000

VISUAL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "displayed": {"type": "string"},
        "rendered_by_host": {"type": "boolean"},
        "flow": {"type": "string"},
        "name": {"type": "string"},
        "row_count": {"type": "integer"},
        "columns": {"type": "array", "items": {"type": "string"}},
        "sample": {"type": "array", "items": {"type": "object"}},
        "complete": {"type": "boolean"},
        "fields": {"type": "array", "items": {"type": "string"}},
        "provenance": {"type": "object"},
    },
    "required": ["displayed", "name", "row_count", "columns"],
    "additionalProperties": True,
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


def _produced_fields(spec: dict) -> set[str]:
    """Column names the spec's own transforms invent, which therefore need no source column.

    Deliberately permissive. Every Vega-Lite transform that creates a column names it with
    ``as`` (``calculate``, ``aggregate``, ``window``, ``bin``, ``timeUnit``, ``density``,
    ``regression``, …), so collecting every ``as`` covers them without enumerating the list —
    which matters because that list grows with each Vega-Lite release. ``fold`` and ``pivot``
    get their defaults added explicitly since both can omit ``as``.
    """
    produced: set[str] = set()
    for node in _walk(spec):
        alias = node.get("as")
        if isinstance(alias, str):
            produced.add(alias)
        elif isinstance(alias, list):
            produced.update(a for a in alias if isinstance(a, str))
        if "fold" in node:
            produced.update({"key", "value"})
        if isinstance(node.get("pivot"), str):
            # pivot turns distinct VALUES into columns, which are data-dependent by definition.
            produced.add(node["pivot"])
    return produced


def _field_refs(spec: dict) -> set[str]:
    """Every source column the spec reads, as ``{field}`` names.

    Covers encoding channels and the ``field``-taking transforms alike, since both spell it
    ``field``. A ``field`` given as a dict (Vega-Lite's repeat/datum forms) is skipped — it does
    not name a column directly.
    """
    refs: set[str] = set()
    for node in _walk(spec):
        field = node.get("field")
        if isinstance(field, str):
            refs.add(field)
        for key in ("groupby", "sort"):
            value = node.get(key)
            if isinstance(value, list):
                refs.update(v for v in value if isinstance(v, str))
    return refs


# ---------------------------------------------------------------------------- validation --- #
def validate_spec(spec: Any, columns: list[dict[str, str]]) -> dict:
    """Check a data-free Vega-Lite spec against a result's real schema. Raises ``ValueError``.

    Three refusals, each one a silent failure if it were left to the renderer:

    1. **``data`` at the top level** — the server owns the data. A spec that carried its own
       would render something other than the result it claims to display.
    2. **``data.url`` anywhere** — the one genuine egress channel in an otherwise declarative
       artifact: it makes the viewer's browser fetch a host we never see.
    3. **A field that is not a column** and is not produced by the spec's own transforms.
       Vega-Lite draws an empty or subtly wrong chart for a missing field without erroring,
       which is precisely the mis-plot this refuses to ship.
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

    known = {c["name"] for c in columns}
    produced = _produced_fields(spec)
    missing = sorted(f for f in _field_refs(spec) if f not in known and f not in produced)
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


def spec_fields(spec: dict, columns: list[dict[str, str]]) -> list[str]:
    """The result columns the spec actually plots, for the text summary.

    The summary must describe what is on screen; a spec can read a subset of a wide result, and
    reporting all the columns would overstate what the reader is looking at.
    """
    known = {c["name"] for c in columns}
    return sorted(f for f in _field_refs(spec) if f in known)


# -------------------------------------------------------------------------------- the app --- #
def app_csp() -> dict[str, Any]:
    """The app page's CSP, in FastMCP's ``ResourceCSP`` wire shape.

    Belongs on the RESOURCE, not the tool: that is where the host reads it from. Declaring it on
    the tool instead yields a resource with no policy, the host blocks every bundle, and the
    frame stays blank with no error to debug.

    Only ``resource_domains`` — the page loads scripts and never calls out. There is deliberately
    no ``connect_domains``: a view that could fetch would be a view that could exfiltrate, and
    ``data.url`` is already refused server-side for the same reason.
    """
    return {"resource_domains": [_JSDELIVR, _UNPKG]}


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
#chart { width: 100%; }
#chart .vega-embed { width: 100%; }
"""

# `ast: true` routes Vega's expression evaluation through the bundled AST interpreter instead of
# the Function constructor, so the page needs no `script-src 'unsafe-eval'` from the host.
#
# The structuredContent recovery branch is ext-apps#696: Claude Desktop strips structuredContent
# from the result it forwards to a view, while the tools/call proxy is unaffected. Unlike the
# hand-rolled shim this replaces, the SDK exposes both halves as documented API — `ontoolinput`
# for the arguments and `callServerTool` for the re-fetch — so there is no transport to reach
# into. It is sound only because `visual` is idempotent and read-only. Temporary:
# `grep -rn "ext-apps#696"` finds every line to delete.
_APP_SCRIPT = """
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@%(ext_apps)s/app-with-deps";

const statusEl = document.getElementById("status");
const chartEl = document.getElementById("chart");
let lastArgs = null, lastSpec = null, recovered = false;

const app = new App({ name: "Spelunk Vega", version: "1.0.0" }, {}, { autoResize: true });

function fail(message) {
  statusEl.className = "error";
  statusEl.textContent = message;
}

function themeConfig() {
  let dark = false;
  try { dark = app.getHostContext()?.theme === "dark"; } catch (e) { /* host may not say */ }
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

async function draw(spec) {
  if (!spec) { fail("No chart spec arrived from the server."); return; }
  lastSpec = spec;
  try {
    statusEl.hidden = true;
    await vegaEmbed(chartEl, spec, {
      ast: true,                 // CSP-safe expression interpreter; see module docstring
      actions: { export: true, source: false, compiled: false, editor: false },
      config: themeConfig(),
    });
    try { app.sendSizeChanged(); } catch (e) { /* host may not accept a size hint */ }
  } catch (err) {
    statusEl.hidden = false;
    fail("Could not render the chart: " + (err && err.message ? err.message : String(err)));
  }
}

app.ontoolinput = ({ arguments: args }) => { lastArgs = args || null; };

app.ontoolresult = async (result) => {
  if (result?.structuredContent?.spec) { await draw(result.structuredContent.spec); return; }
  if (result?.isError) { fail("The server reported an error building this chart."); return; }
  // ext-apps#696: structuredContent was stripped in transit — ask for it again through the
  // tools/call proxy, which the host leaves intact.
  if (recovered || !lastArgs) { fail("No chart spec arrived from the server."); return; }
  recovered = true;
  try {
    const again = await app.callServerTool({ name: "visual", arguments: lastArgs });
    await draw(again?.structuredContent?.spec);
  } catch (err) {
    fail("Could not recover the chart spec from the server.");
  }
};

app.onhostcontextchanged = () => {
  // Theme lives in the host context, and the config is baked in at embed time, so a theme flip
  // means re-embedding rather than restyling in place.
  if (lastSpec) { draw(lastSpec); }
};

await app.connect();
"""


def app_html() -> str:
    """The complete app page: a Vega-Lite renderer wired to the host through the ext-apps SDK.

    Self-contained apart from the pinned CDN bundles named in :func:`app_csp`. Hand-written
    rather than generated, because unlike the Prefab renderer it replaces there is no upstream
    page to inherit — which is also why it is far shorter.
    """
    script = _APP_SCRIPT % {"ext_apps": EXT_APPS_VERSION}
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>Spelunk chart</title>\n"
        f"<style>{_STYLE}</style>\n"
        f'<script src="{_JSDELIVR}/npm/vega@{VEGA_VERSION}"></script>\n'
        f'<script src="{_JSDELIVR}/npm/vega-lite@{VEGA_LITE_VERSION}"></script>\n'
        f'<script src="{_JSDELIVR}/npm/vega-embed@{VEGA_EMBED_VERSION}"></script>\n'
        "</head>\n<body>\n"
        f"{_FALLBACK}\n"
        '<div id="chart"></div>\n'
        f'<script type="module">{script}</script>\n'
        "</body>\n</html>\n"
    )
