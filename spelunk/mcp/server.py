"""MCP front-end for Spelunk — a multi-source DuckDB query + transformation-pipeline server.

One DuckDB session ([core/duck.py]) is both the query engine and the workspace: files and
attached databases live in it alongside the flow results, so a single ``query`` can join any
of them. No model, no loop — Claude Code is the agent.

Usage (stdio, the default — for Claude Code via .mcp.json)::

    python -m spelunk.mcp.server --source ./data/sales.parquet --source sqlite:///app.db \
        --session-dir .spelunk_session

Usage (streamable HTTP, for a client that connects to a URL)::

    python -m spelunk.mcp.server --transport http --source ./data/sales.parquet
    # -> http://127.0.0.1:8080/mcp
"""
from __future__ import annotations

import argparse
import functools
import inspect
import ipaddress
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.apps.config import AppConfig, ResourceCSP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel, Field

from spelunk import __version__
from spelunk.core.duck import DuckSession, _full_sample_fits, _SAMPLE_ROWS, _validate_name
from spelunk.mcp import vega

# Shared help text for the plain-English `description` — kept identical on the single-query
# parameter and the per-step field so the agent sees one consistent instruction.
_DESCRIPTION_HELP = (
    "One-line, plain-English summary of what this query does for a NON-TECHNICAL reader "
    "(keep to ~20 words, one line). Stored with the result and surfaced by `lineage` and "
    "`catalog` so the pipeline reads as a clear, sequential story."
)


class QueryStep(BaseModel):
    """One step of a batch `query` call: a SELECT and the result name it materializes as."""

    sql: str = Field(description="Read-only DuckDB SELECT; may reference earlier steps' names.")
    name: str = Field(description="Result table name this step materializes as (SQL identifier).")


class DescribedQueryStep(QueryStep):
    """A batch `query` step for a server run with `--require-descriptions`: adds a required
    one-line, plain-English `description` on top of `sql` + `name`."""

    description: str = Field(description=_DESCRIPTION_HELP)

# One JSON line per tool call lands here so agent usage can be analysed offline. Handlers are
# (re)attached by _configure_tool_logging; until then a NullHandler keeps library/test use silent.
_tool_logger = logging.getLogger("spelunk.toolcalls")
_tool_logger.addHandler(logging.NullHandler())
_tool_logger.setLevel(logging.INFO)
_tool_logger.propagate = False

# Args worth recording verbatim (SQL kept full — that's the point of the log); long head samples
# and row payloads are summarised, never dumped. `steps` is a batch of {sql, name} — full SQL kept.
_LOGGED_ARGS = (
    "sql", "name", "flow", "target", "format", "path", "spec", "steps", "into", "dry_run",
    "description", "source", "params", "rows_from", "records", "paginate", "max_pages",
    "max_rows", "max_urls",
    "kind", "title",  # visual
)
_LOGGED_RESULT_FIELDS = (
    "name", "flow", "row_count", "format", "path", "dropped_results", "kind",
    "step_count", "completed", "failed_step",
    "root", "source_flow", "target_flow", "dry_run",  # lineage / replay
    "displayed", "rendered_by_host",  # visual
)
# lineage/replay result lists carry the full SQL of every node — log their size, not their body.
_COUNTED_RESULT_FIELDS = ("columns", "nodes", "edges", "order", "missing", "rebuilt", "plan")

# add_source accepts DSNs that can embed credentials (postgresql://user:pw@host/db); strip the
# userinfo (user:pass@) before the spec is written to the on-disk tool-call log.
_DSN_CREDENTIALS_RE = re.compile(r"//[^/@\s]+@")
# DuckDB rewrites a postgresql:// DSN into libpq keyword form before connecting, so a failed
# ATTACH reports `password=<secret>` — a shape the userinfo pattern above cannot match. Both
# forms have to be masked, or the error path leaks what the arg path redacts.
_KEYWORD_PASSWORD_RE = re.compile(
    r"""(?i)\b(password\s*=\s*)('[^']*'|"[^"]*"|[^\s'";]+)"""
)


def _redact(value: object) -> object:
    """Mask credentials in DSN-like strings so they never reach the log.

    Handles both the URL form (``//user:pass@host``) and the keyword form
    (``password=secret``) that database drivers produce in connection errors.
    """
    if isinstance(value, str):
        masked = _DSN_CREDENTIALS_RE.sub("//***@", value)
        return _KEYWORD_PASSWORD_RE.sub(r"\1***", masked)
    return value


def _log_arg(key: str, value: object) -> object:
    """Make one logged argument JSON-safe: redact DSN specs, unwrap pydantic step models."""
    if key == "spec":
        return _redact(value)
    if key == "steps" and isinstance(value, list):
        return [s.model_dump() if isinstance(s, BaseModel) else s for s in value]
    return value


def _configure_tool_logging(tool_log: str | None) -> None:
    """Point the tool-call logger at a JSONL file, stderr (``"-"``), or nowhere (``None``).

    Idempotent: clears prior handlers so repeated ``build_server`` calls (e.g. in tests) don't
    stack duplicates. Never logs to stdout — that channel is the stdio MCP transport.
    """
    for handler in list(_tool_logger.handlers):
        _tool_logger.removeHandler(handler)
        handler.close()

    if tool_log is None:
        _tool_logger.addHandler(logging.NullHandler())
        return

    handler: logging.Handler
    if tool_log == "-":
        handler = logging.StreamHandler()  # stderr
    else:
        Path(tool_log).parent.mkdir(parents=True, exist_ok=True)  # create the log dir if missing
        handler = logging.FileHandler(tool_log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _tool_logger.addHandler(handler)


def _summarize_result(result: object) -> dict:
    """Compact, log-safe view of a tool result — counts and identifiers, not full row data."""
    if isinstance(result, ToolResult):
        # `visual` returns a ToolResult: a UI payload for the host plus a JSON text summary for
        # the model. Log the summary — the hydrated Vega-Lite spec carries every plotted row and
        # would bury the line in data.
        texts = [b.text for b in result.content if isinstance(b, TextContent)]
        try:
            result = json.loads(texts[0]) if texts else {}
        except ValueError:
            return {"type": "ToolResult"}
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    summary = {k: result[k] for k in _LOGGED_RESULT_FIELDS if k in result}
    for key in _COUNTED_RESULT_FIELDS:
        val = result.get(key)
        if isinstance(val, (list, dict)):
            summary["column_count" if key == "columns" else f"{key}_count"] = len(val)
    return summary


def _logged(fn):
    """Wrap a tool function so each call emits one structured JSON line to ``_tool_logger``.

    Records timestamp, tool name, the salient arguments, outcome (ok/error), a result summary,
    and wall-clock duration. ``functools.wraps`` + the original signature are preserved so
    FastMCP still derives the correct tool schema. The tool name is the function name minus its
    leading underscore (``_query`` → ``query``).
    """
    tool_name = fn.__name__.lstrip("_")
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "args": {
                k: _log_arg(k, v) for k, v in bound.arguments.items() if k in _LOGGED_ARGS
            },
        }
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            record["outcome"] = "error"
            # Redacted like the args are: a driver's connection error quotes the DSN back,
            # so an unmasked message would write to disk exactly what _log_arg withheld.
            record["error"] = _redact(f"{type(exc).__name__}: {exc}")
            record["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
            _tool_logger.info(json.dumps(record, default=str))
            raise
        record["outcome"] = "ok"
        record["result"] = _summarize_result(result)
        record["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
        _tool_logger.info(json.dumps(record, default=str))
        return result

    return wrapper


def _dispatch_query(
    session: DuckSession,
    *,
    sql: str | None,
    name: str | None,
    steps: list[QueryStep] | None,
    flow: str,
    description: str | None,
    require_descriptions: bool,
) -> dict:
    """Shared body for the `query` tool — single and batch modes — with optional enforcement of
    a mandatory ``description`` when the server runs with ``--require-descriptions``."""
    if steps is not None:
        if sql is not None or name is not None:
            raise ValueError("Pass either sql+name (single query) or steps (batch), not both.")
        if description is not None:
            # Batch descriptions are per step; a top-level one has nothing to attach to. Reject
            # rather than drop it silently — a caller who thinks it landed would see the result
            # come back undescribed in `lineage` with no clue why.
            raise ValueError(
                "description applies to a single query; in batch mode put a description on each "
                "step: steps=[{sql, name, description}, ...]."
            )
        step_dicts = [s.model_dump() for s in steps]
        if require_descriptions:
            for i, step in enumerate(step_dicts):
                if not (step.get("description") or "").strip():
                    raise ValueError(
                        f"steps[{i}] needs a one-line, plain-English description (~20 words) — "
                        "this server was started with --require-descriptions."
                    )
        return session.query_steps(step_dicts, flow)
    if sql is None or name is None:
        raise ValueError(
            "A single query needs both sql and name; a batch needs steps=[{sql, name}, ...]."
        )
    if require_descriptions and not (description or "").strip():
        raise ValueError(
            "This query needs a one-line, plain-English description (~20 words) — this server "
            "was started with --require-descriptions."
        )
    return session.query(sql, name, flow, description)


def _host_renders_ui() -> bool:
    """Best-effort: did the connected client advertise the MCP Apps extension?

    Used only to ANNOTATE the summary the model reads, never to decide what to send — `visual`
    always returns both a text summary and the UI payload, because an MCP result carries both
    and a host that can't render simply ignores `structuredContent`. That matters: not every
    UI-capable host advertises the extension, and a false negative here must cost the user
    nothing more than the model declining to say "see the chart above".
    """
    try:
        from fastmcp.apps.config import UI_EXTENSION_ID
        from fastmcp.server.dependencies import get_context

        return get_context().client_supports_extension(UI_EXTENSION_ID)
    except (RuntimeError, ImportError, AttributeError):
        # No active client session (library/test caller), no apps support, or a host/FastMCP
        # build whose context lacks `client_supports_extension`. All three are cosmetic here —
        # letting any of them escape would turn a missing annotation into a failed `visual`.
        return False


def _require_existing_flow(session: DuckSession, flow: str) -> str:
    """Reject an unknown flow BEFORE a display path can provision it.

    `session.catalog(flow)` and `session.profile(...)` both `CREATE SCHEMA IF NOT EXISTS` — right
    for the tools that BUILD, wrong for `visual`, which must leave nothing behind. Without this a
    typo'd flow name gets created and then shows up in `catalog`, which is precisely the way "a
    view is not a result" is observable. Checked here rather than in DuckSession so the
    `catalog`/`profile` TOOLS keep their existing provisioning behaviour.
    """
    known = {f["flow"] for f in session.catalog()["flows"]}  # no-arg catalog only lists
    if flow not in known:
        raise ValueError(f"Unknown flow {flow!r}. Known flows: {sorted(known)}.")
    return flow


def _provenance(session: DuckSession, name: str, flow: str) -> dict | None:
    """The lineage closure that built *name*, for the summary's provenance line — or ``None``.

    The catch is load-bearing: ``lineage()`` RAISES for a result with no recorded row, and a
    display path must not die because provenance happens to be missing. Every `query` result
    records one, so in practice this returns a graph; the fallback is what keeps an edge case
    (a result whose lineage row was never written) drawing a plain chart instead of erroring.
    """
    try:
        return session.lineage(name, flow)
    except ValueError:
        return None


def _provenance_summary(lineage: dict | None) -> dict:
    """The build order behind the plotted result, for the text half of the reply.

    Only the step ORDER goes in — the full DAG stays with the `lineage` tool rather than being
    duplicated into every view. This is the model's half only: nothing about it reaches the
    chart, so it makes no claim about what is on screen.
    """
    if not (lineage and lineage.get("nodes")):
        return {}
    return {"provenance": {"steps": lineage.get("order", [])}}


def _dispatch_visual(
    session: DuckSession,
    *,
    name: str,
    spec: Any,
    title: str | None,
    flow: str | None,
) -> ToolResult:
    """Shared body for the `visual` tool: check a spec against the data, then hydrate it.

    Returns both halves of an MCP result — `content` (JSON the model reads, mirroring `query`'s
    sample contract) and `structuredContent` (the hydrated Vega-Lite spec a UI host renders).
    Read-only throughout: no table is created and no lineage row is written, because a view is
    not a result.

    Order matters. The columns are read FIRST so `validate_spec` can check the spec's field
    references against the result's real schema — Vega-Lite renders a missing field as an empty
    or subtly wrong chart without erroring, and catching that here is what keeps a silent
    mis-plot from reaching the user.
    """
    if not name:
        raise ValueError("`visual` needs the `name` of a saved result to plot.")
    # Identifier validation first, existence second: junk like `a"b` should read as an invalid
    # name, not "unknown flow". `_require_existing_flow` then stops a typo'd flow being
    # provisioned by a display path — the way "a view is not a result" is observable.
    resolved = _validate_name(flow or session.default_flow, "flow name")
    _validate_name(name)
    _require_existing_flow(session, resolved)

    columns, rows = session.rows_for_display(name, resolved, max_rows=vega.VEGA_MAX_ROWS)
    validated = vega.validate_spec(spec, columns)
    hydrated = vega.hydrate(validated, rows, title)

    # Mirror `query`'s contract exactly: every row when the result is small on both axes,
    # otherwise a short head. The model then reads a small deliverable straight out of the text
    # half without needing the host to have rendered anything.
    complete = _full_sample_fits(len(rows), len(columns))
    summary: dict = {
        "displayed": "vega-lite",
        "rendered_by_host": _host_renders_ui(),
        "flow": resolved,
        "name": name,
        "row_count": len(rows),
        "columns": [c["name"] for c in columns],
        "fields": vega.spec_fields(validated, columns),
        "sample": rows if complete else rows[:_SAMPLE_ROWS],
        "complete": complete,
        **_provenance_summary(_provenance(session, name, resolved)),
    }
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(summary, default=str))],
        structured_content={"spec": hydrated},
    )


def build_server(
    session: DuckSession,
    tool_log: str | None = None,
    *,
    allow_add_source: bool = False,
    require_descriptions: bool = False,
) -> FastMCP:
    """Build a FastMCP instance wired to an open :class:`DuckSession`.

    Registers the ``db://`` discovery resources and the ``query`` / ``profile`` / ``export`` /
    ``catalog`` / ``drop`` / ``lineage`` / ``replay`` tools.

    ``tool_log`` controls per-call logging: a file path writes JSON lines there, ``"-"`` writes
    them to stderr, and ``None`` (the default) is silent.

    ``allow_add_source`` (off by default) additionally registers ``add_source`` / ``remove_source``
    so the agent can attach and detach files and databases at runtime. This lets the agent reach
    any file/DB the host process can — only enable it for a trusted, process-per-agent setup.

    ``require_descriptions`` (off by default) gates the plain-English description feature: when on,
    the ``query`` tool exposes a required one-line ``description`` on every single query and every
    batch step (stored in lineage, surfaced by ``lineage`` / ``catalog``); when off, the parameter
    is absent from the tool schema entirely.
    """
    _configure_tool_logging(tool_log)

    source_list = ", ".join(f"{s.name} ({s.kind})" for s in session.sources) or "(none configured)"

    mcp = FastMCP(
        "spelunk",
        version=__version__,
        instructions=(
            "Spelunk is a single DuckDB engine over all your data sources. Files (CSV/Parquet/"
            "JSON/Excel) and attached databases (SQLite/PostgreSQL/MySQL) live in one DuckDB "
            "session together with your saved results, so one query can join across all of "
            f"them. All SQL is DuckDB SQL. Configured sources: {source_list}.\n\n"
            "## Discover\n"
            "- `db://tables` — JSON array of queryable source objects. Attached-database tables "
            "are named `<source>.<table>`, or `<source>.<schema>.<table>` when in a non-default "
            "schema (paste-ready either way); file sources appear as a bare view name.\n"
            "- `db://{table}` — describe one object: columns, types, primary key, a sample, and "
            "a row count. Read this before writing SQL.\n\n"
            "## Query and build\n"
            "- `query(sql, name, flow?)` — run a read-only SELECT over sources AND saved results, "
            "and store the FULL result as table `name` in the flow (no row cap). Returns the "
            "result's columns, true row_count, and a sample — a 5-row head, or EVERY row (with "
            "`complete: true`) when the result is small — so you read a small deliverable without "
            "paging. This is the ONE tool for both "
            "looking and building: every result is named and immediately reusable — reference it "
            "by `name` in your next query. `name` is required; reuse a scratch name (e.g. `tmp`) "
            "for throwaways, or `drop` them. Reference attached DB tables as \"<source>\".\"<table>\", "
            "files and prior results by bare name.\n"
            "- `query(steps=[{sql, name}, ...], flow?)` — the SAME tool in batch mode: an ordered "
            "list of queries executed in one call. Steps may be a dependent chain (later steps "
            "reference earlier steps' names), a bundle of unrelated queries, or a mix — batch "
            "whenever you want more than one result in one round trip, not just for pipelines. "
            "ALWAYS batch instead of firing sequential single calls. Fail-fast: completed steps "
            "stay materialized (with lineage), the failing step reports its error, the rest are "
            "skipped. Every terminal step (one no later step references — the last, plus any "
            "independent query) returns a sample (full rows when small, else a head); "
            "intermediate steps consumed downstream stay compact (row_count + columns).\n"
            + (
                "- REQUIRED here: every single query and every batch step takes a one-line, "
                "plain-English `description` (~20 words) of what it does for a non-technical "
                "reader. It's stored with the result and shown in `lineage` and `catalog`, so "
                "the pipeline reads as a clear, sequential story.\n"
                if require_descriptions
                else ""
            )
            + "- `profile(sql, flow?)` — per-column stats (null_rate, min/max/mean/std, "
            "p25/p50/p75/p95 for numerics; unique/top/freq for text) over the full result. Use "
            "this instead of writing manual aggregation queries.\n"
            "- `export(target, format, path, flow?)` — write a saved result name OR a full SELECT "
            "to csv/json/parquet (no row cap).\n"
            + (
                "\n## Show it to the user\n"
                "- `visual(name, spec)` — DRAW a saved result in the chat. `spec` is a Vega-Lite "
                "spec WITHOUT any `data` (the server injects the rows), so you get the whole "
                "Vega-Lite grammar: layering, faceting, binning, tooltips, interactive "
                "selections. A view is NOT a result — `visual` creates nothing and there is "
                "nothing to drop afterwards.\n"
                "- Aggregate FIRST, then draw: capped at "
                f"{vega.VEGA_MAX_ROWS} rows, erroring rather than truncating, because a "
                "shortened chart misstates the data. `query` the GROUP BY, `visual` the result.\n"
                "- Use it when a shape, comparison or trend is the point — a chart of 12 monthly "
                "totals says more than 12 rows of JSON. Keep reading results from `query`; "
                "`visual` is for the human.\n"
            )
            + (
                "\n## Manage sources\n"
                "- `add_source(spec)` — attach a new data source at runtime. `spec` is a file path "
                "(.csv/.parquet/.json/.xlsx), a SQLite file, or a sqlite:// / postgresql:// / "
                "mysql:// DSN. To name the source, put YOUR chosen name before an `=` at the "
                "very front — `sales=./sales.parquet` names it `sales`. (The word before the "
                "`=` IS the name: writing `name=sales ./sales.parquet` literally asks for a "
                "source called `name` and fails to parse.) The source becomes queryable in "
                "every flow.\n"
                "- `remove_source(name)` — detach a source by its name. Affects this session only.\n"
                "\n## Work with APIs — one API is ONE source\n"
                "- `add_source('tmdb=openapi:<spec-url-or-path> auth_env=<ENV>')` attaches a whole "
                "API: a queryable endpoint catalog AND a live connection. Do this ONCE. Never "
                "attach a source per endpoint.\n"
                "- Find the endpoint with SQL over the catalog — `path`, `response_fields` (the "
                "fields it RETURNS, so you can search by the data you need), `records_hint`, "
                "`pagination_hint`, `params`. `method` is lowercase ('get').\n"
                "- `fetch(source, path, name, params?)` calls one endpoint and stores the response "
                "as result `name`, exactly like `query` does. Responses are RESULTS, not sources: "
                "droppable, in `lineage`, flow-scoped. Batch with `fetch(steps=[...])`.\n"
                "- `fetch(source, path='/x/{id}', rows_from=<result>, name=...)` fetches ONE URL "
                "PER ROW of that result — the list->detail fan-out (a list endpoint rarely carries "
                "the detail fields you need). Rows are stamped `_key_<placeholder>` to join back.\n"
                "- Credentials live on the connection. NEVER pass one in `params` — params are "
                "logged verbatim, and the attempt is refused.\n"
                if allow_add_source
                else ""
            )
            + "\n## Organize with flows\n"
            "A *flow* is an isolated result namespace (a DuckDB schema; default `\"default\"`). "
            "Give each concurrent line of analysis its own `flow` so results never collide. "
            "Reference a result in another flow as \"<flow>\".\"<name>\".\n"
            "- `catalog()` — list flows and their result counts; `catalog(flow)` — list the "
            "results in a flow with columns and row counts.\n"
            "- `drop(name, flow?)` — drop one result; `drop(flow=...)` with no name — drop a whole "
            "flow.\n\n"
            "## Provenance & replay\n"
            "Every result records the SQL and dependencies that built it, so a flow is a "
            "reproducible pipeline, not a pile of tables.\n"
            "- `lineage(name?, flow?)` — see how results were built: `lineage(name)` gives the "
            "upstream closure that produced one result; `lineage()` gives the whole flow's DAG "
            "(nodes, edges, and a dependency-first order).\n"
            "- `replay(flow?, into?, dry_run?)` — rebuild a flow from its recorded SQL in "
            "dependency order. `replay(flow, into='v2')` rebuilds into a fresh flow "
            "(non-destructive — e.g. after source files change, then diff old vs new); "
            "`replay(flow)` refreshes in place; `dry_run=true` shows the plan first.\n\n"
            "## Notes\n"
            "- All queries are read-only SELECTs (CTEs fine); writes/DDL are rejected at the AST "
            "level. The server materializes your SELECT as a table for you — don't write CREATE/"
            "INSERT yourself.\n"
            "- The sample is the whole result when `complete` is true; otherwise it's a 5-row "
            "preview and the full result is the saved table — `profile` or query it for the whole "
            "set, or `export` it to a file.\n"
            "- Sources are read on demand (DuckDB pushes filters/projections down); filter or "
            "aggregate before materializing rather than copying a whole large table."
        ),
    )

    # --- Resources: source discovery ------------------------------------------------ #
    @mcp.resource("db://tables", name="list_tables", description="List queryable source objects (attached-DB tables + file views).")
    def _list_tables() -> str:
        return json.dumps([obj.model_dump() for obj in session.list_objects()])

    @mcp.resource("db://{table}", name="describe_table", description="Describe one source object: columns, primary key, sample rows, row count.")
    def _describe_table(table: str) -> str:
        return session.describe(table).model_dump_json()

    # --- Tools ----------------------------------------------------------------------- #
    query_tool_description = (
        "Run read-only DuckDB SELECTs over sources and saved results, storing each full "
        "result (no row cap) as a named table in the flow for immediate reuse. Two modes: "
        "single (`sql` + `name`) or batch (`steps=[{sql, name}, ...]`). ALWAYS prefer one "
        "batch call over sequential single calls: steps run in list order and a later step "
        "may reference an earlier step's `name` like any saved result. Batch is not only for "
        "pipelines — the steps can be a dependent chain, a bundle of unrelated queries, or a "
        "mix; batch whenever you want more than one result in one round trip. Fail-fast — "
        "completed steps stay materialized, the failing step reports its error, the rest are "
        "skipped. Every terminal step (one no later step references — the last, plus any "
        "independent query) returns a sample — every row with `complete: true` when small, "
        "else a 5-row head; downstream-consumed intermediates return row_count + columns. "
        "Reference attached DB tables as \"<source>\".\"<table>\"; files and prior results by "
        "bare name. Writes/DDL are rejected."
    )
    if require_descriptions:
        query_tool_description += (
            " This server REQUIRES a one-line, plain-English `description` (~20 words, for a "
            "non-technical reader) on every single query and every batch step; it is stored "
            "with the result and shown in `lineage` and `catalog`."
        )

    if require_descriptions:
        @mcp.tool(name="query", description=query_tool_description)
        @_logged
        def _query(
            sql: str | None = None,
            name: str | None = None,
            description: Annotated[str | None, Field(description=_DESCRIPTION_HELP)] = None,
            steps: list[DescribedQueryStep] | None = None,
            flow: str = "default",
        ) -> dict:
            return _dispatch_query(
                session, sql=sql, name=name, steps=steps, flow=flow,
                description=description, require_descriptions=True,
            )
    else:
        @mcp.tool(name="query", description=query_tool_description)
        @_logged
        def _query(
            sql: str | None = None,
            name: str | None = None,
            steps: list[QueryStep] | None = None,
            flow: str = "default",
        ) -> dict:
            return _dispatch_query(
                session, sql=sql, name=name, steps=steps, flow=flow,
                description=None, require_descriptions=False,
            )

    @mcp.tool(
        name="profile",
        description=(
            "Run a SELECT and return per-column statistics over the full result, computed in "
            "DuckDB. Numerics: non_null_count, null_rate, min, max, mean, std, p25/p50/p75/p95. "
            "Text: non_null_count, null_rate, unique, top, freq. No row cap."
        ),
    )
    @_logged
    def _profile(sql: str, flow: str = "default") -> dict:
        return session.profile(sql, flow)

    @mcp.tool(
        name="export",
        description=(
            "Write a saved result (by name, e.g. `joined` or \"src\".\"orders\") OR a full SELECT "
            "to a file. Formats: csv, json, parquet. Parent directories are created. No row cap."
        ),
    )
    @_logged
    def _export(target: str, format: str, path: str, flow: str = "default") -> dict:
        return session.export(target, format, path, flow)

    @mcp.tool(
        name="catalog",
        description=(
            "With no argument: list active flows and how many results each holds. With a flow: "
            "list that flow's saved results with their columns, types, row counts, and the "
            "one-line plain-English description recorded for each (null if none)."
        ),
    )
    @_logged
    def _catalog(flow: str | None = None) -> dict:
        return session.catalog(flow)

    @mcp.tool(
        name="drop",
        description=(
            "Delete a saved result (give `name`) or an entire flow and all its results (give "
            "only `flow`). Idempotent; cannot drop reserved schemas."
        ),
    )
    @_logged
    def _drop(name: str | None = None, flow: str = "default") -> dict:
        return session.drop(name, flow)

    @mcp.tool(
        name="lineage",
        description=(
            "Show how results were built: the SQL and dependency edges behind them. With `name`, "
            "return the upstream closure that produced that result (the node plus every result it "
            "transitively depends on, across flows); with no `name`, the whole flow. Returns nodes "
            "(name, kind, description, sql, deps, sources, created_at), edges, a dependency-first "
            "`order`, and `missing` (deps whose lineage is gone). `description` is a one-line, "
            "plain-English label (null if none was given). Pass `render='mermaid'` (or `'dot'`) to "
            "also get a ready-to-display diagram string (under that key) built deterministically "
            "from the same graph — no parsing needed; paste Mermaid into markdown/an artifact, or "
            "run DOT through `dot -Tsvg`. `path` writes the diagram to a file (implies "
            "`render='mermaid'`) and returns its absolute path under `rendered_to`. Read-only."
        ),
    )
    @_logged
    def _lineage(
        name: str | None = None,
        flow: str = "default",
        render: str | None = None,
        path: str | None = None,
    ) -> dict:
        return session.lineage(name, flow, render, path)

    # Spelunk serves the app page for `visual` as its own ui:// resource. The CSP belongs on the
    # RESOURCE, not the tool: that is where the host reads it from (the tool carries only
    # resourceUri). Declaring it on the tool instead silently yields a resource with NO policy,
    # the host blocks every bundle, and the app frame stays completely blank — no error, no
    # spinner, nothing to debug.
    @mcp.resource(
        vega.VEGA_URI,
        name="spelunk_vega",
        description="Vega-Lite renderer for `visual`, wired to the host through the "
                    "ext-apps SDK, with the ext-apps#696 structuredContent recovery path.",
        app=AppConfig(csp=ResourceCSP(**vega.app_csp())),
    )
    def _vega_app() -> str:
        return vega.app_html()

    @mcp.tool(
        name="visual",
        app=AppConfig(resource_uri=vega.VEGA_URI),
        output_schema=vega.VISUAL_OUTPUT_SCHEMA,
        description=(
            "DRAW a saved result in the chat as an interactive Vega-Lite chart. Read-only and "
            "non-destructive: `visual` creates NO result and NO lineage node — it is a VIEW of "
            "what already exists, so there is nothing to `drop` afterwards.\n"
            "`name` is the saved result to plot. `spec` is a Vega-Lite spec (object or JSON "
            "string) with **no `data` key** — the server injects the result's rows for you, so "
            "write the spec as though `data` were already there. That gives you the whole "
            "Vega-Lite grammar: `mark`, `encoding`, `transform`, `layer`, `facet`, `hconcat`/"
            "`vconcat`, `params` for interactive selections, tooltips, and binning.\n"
            'Example — `{"mark": "bar", "encoding": {"x": {"field": "region", '
            '"type": "nominal"}, "y": {"field": "revenue", "type": '
            '"quantitative"}}}`.\n'
            "Every `field` you name must be a column of the result (or one your own `transform` "
            "creates) — a field that exists in neither is an ERROR, never a silently blank "
            "chart. `data.url` is refused: every row comes from a saved result.\n"
            "AGGREGATE FIRST: capped at "
            f"{vega.VEGA_MAX_ROWS} rows, and past that `visual` ERRORS rather than truncating, "
            "because a silently shortened chart is a picture that misstates the data. Group or "
            "top-N with `query`, then draw that result. "
            "`title` sets the chart title when the spec has none. "
            "The reply also carries a text summary (with the rows themselves when the result is "
            "small), so you can keep reasoning about what you displayed."
        ),
    )
    @_logged
    def _visual(
        name: str,
        spec: dict | str,
        title: str | None = None,
        flow: str | None = None,
    ) -> ToolResult:
        return _dispatch_visual(session, name=name, spec=spec, title=title, flow=flow)


    @mcp.tool(
        name="replay",
        description=(
            "Rebuild a flow's results from their recorded SQL, in dependency order — re-running "
            "each `query`. External inputs (sources, cross-flow "
            "results) must already exist; they are read, not rebuilt. With `into`, rebuild into a "
            "fresh flow (non-destructive — e.g. re-run the pipeline against updated source files, "
            "then compare); without it, refresh in place. `dry_run=true` returns the ordered plan "
            "without executing. Errors on a dependency cycle."
        ),
    )
    @_logged
    def _replay(flow: str = "default", into: str | None = None, dry_run: bool = False) -> dict:
        return session.replay(flow, into, dry_run)

    if allow_add_source:
        @mcp.tool(
            name="add_source",
            description=(
                "Attach a new data source at runtime, then query it like any configured source. "
                "`spec` is a file path (.csv/.parquet/.json/.xlsx), a SQLite file, a sqlite:// / "
                "postgresql:// / mysql:// DSN, or a REST/JSON API — `api:<url> [key=value ...]` "
                "fetches the endpoint ONCE into a local snapshot (options: records=<dot.path>, "
                "paginate=page|offset|cursor|keyset|link|odata, max_pages/max_rows. "
                "EVERY PAGING PARAM NAME IS CONFIGURABLE — page_param=/offset_param=/size_param="
                "/page_size=/start=/cursor_param=/cursor_path=/keyset_field= — so an API that "
                "pages by its own vocabulary needs no special support: e.g. "
                "`paginate=offset offset_param=startIndex size_param=resultsPerPage "
                "page_size=2000`. The page/offset/limit defaults are only conventions, and some "
                "APIs REJECT a paging param they don't recognize, so set these to the endpoint's "
                "real names rather than letting the defaults ride — one paginating source beats "
                "N hand-paged ones. json=true types the snapshot as one raw JSON column for "
                "genuinely polymorphic payloads; OData sources also "
                "take filter=\"<SQL predicate>\" and select=<cols>, translated to $filter/"
                "$select and applied SERVER-side; auth via auth_env=<ENV> "
                "Bearer, header=<Name>:<ENV>, or param=<name>:<ENV> — env var names, never "
                "values; re-add to refresh). `openapi:<url-or-path>` attaches an OpenAPI 3.x "
                "spec as a queryable endpoint CATALOG (one row per path+method — `method` is "
                "LOWERCASE, e.g. 'get' — carrying `response_fields`, the fields each endpoint "
                "returns, so you can find an endpoint by the data it exposes, plus a ready-made "
                "`suggested_spec` column for GETs — query it, fill <SET_ME> with an env var "
                "name, pass to add_source; a `pagination_hint` of `unknown; endpoint declares "
                "...` lists the paging params found — set offset_param=/size_param= from them). "
                "To name the source, put YOUR chosen name before an `=` at the very front — "
                "`sales=./sales.parquet`, `nvd=api:https://...` (the word before `=` IS the "
                "name; `name=nvd api:...` is wrong and fails to parse). "
                "Returns the source name, kind, and the objects it made queryable. The source is "
                "visible in every flow of this session."
            ),
        )
        @_logged
        def _add_source(spec: str) -> dict:
            return session.add_source(spec)

        @mcp.tool(
            name="remove_source",
            description=(
                "Detach a data source by its name (as shown by `add_source` or in `db://tables`), "
                "removing it from this session. Idempotent on the underlying detach; errors only if "
                "no source has that name."
            ),
        )
        @_logged
        def _remove_source(name: str) -> dict:
            return session.remove_source(name)

        @mcp.tool(
            name="fetch",
            description=(
                "Call ONE endpoint of an attached API connection (an `openapi:` source) and "
                "materialize the response as result `name` — the same return shape as `query`. "
                "One API is ONE source: attach it once with add_source, then fetch as many "
                "endpoints as you like. Each response is a flow-scoped RESULT (droppable, in "
                "`lineage`), not a new source. Find the endpoint first by SQL-querying the "
                "catalog — `path`, `response_fields` (what it returns), `records_hint`, "
                "`pagination_hint` — then pass that same `path` here. "
                "`params` is a JSON object of query params ({\"sort_by\": \"revenue.desc\"}); a "
                "`{placeholder}` in the path consumes the param of that name as a path segment. "
                "NEVER put a credential in `params` (they are logged verbatim) — the connection "
                "already carries it. Pagination params are managed for you; passing one errors. "
                "`rows_from=<result>` binds the remaining {placeholders} to that result's "
                "columns and fetches ONE URL PER ROW — the list->detail fan-out (movies -> "
                "/movie/{movie_id}); rows are stamped with _key_<placeholder> so they join back. "
                "`steps=[{source,path,name,...},...]` runs several fetches in one call. "
                "Snapshot semantics: fetched once, queries never re-hit the API; fetch again to "
                "refresh."
            ),
        )
        @_logged
        def _fetch(
            source: str | None = None,
            path: str | None = None,
            name: str | None = None,
            params: dict | None = None,
            rows_from: str | None = None,
            records: str | None = None,
            paginate: str | None = None,
            max_pages: int | None = None,
            max_rows: int | None = None,
            max_urls: int | None = None,
            concurrency: int | None = None,
            options: dict | None = None,
            steps: list[dict] | None = None,
            description: str | None = None,
            flow: str = "default",
        ) -> dict:
            if steps is not None:
                if source is not None or path is not None or name is not None:
                    raise ValueError(
                        "Pass either source+path+name (one fetch) or steps (batch), not both."
                    )
                return session.fetch_steps(steps, flow=flow)
            if not source or not path or not name:
                raise ValueError("fetch needs source, path and name (or steps=[...]).")
            return session.fetch(
                source=source, path=path, name=name, params=params, flow=flow,
                description=description, rows_from=rows_from, records=records,
                paginate=paginate, max_pages=max_pages, max_rows=max_rows,
                max_urls=max_urls, concurrency=concurrency, options=options,
            )

    return mcp


def _load_env_file(path: str) -> None:
    """Export KEY=VALUE lines from *path* into ``os.environ`` (existing variables win).

    Quotes around values are stripped; blank lines and ``#`` comments are ignored. A missing
    or unreadable file warns on stderr rather than failing startup — the server is still
    useful without the credentials, and api: sources name the missing variable on use.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"[spelunk] --env-file {path!r} not loaded: {exc}", file=sys.stderr)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

# How many DISTINCT rejected Origin/Host values one process will explain before going quiet.
# The dedupe set behind the 403 diagnostic is keyed on a header an unauthenticated caller
# controls, so it needs a ceiling; a real deployment sees a handful (one tunnel hostname, one
# client origin, a rotation or two), so this is far above legitimate use and far below a leak.
_ANNOUNCE_LIMIT = 32


def _is_loopback(host: str) -> bool:
    """Does `host` name this machine only? Decides whether we can enumerate valid Host values."""
    bare = host.strip("[]").lower()
    if bare in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def _bracketed(host: str) -> str:
    """`::1` -> `[::1]`; anything else unchanged. An IPv6 literal is only a valid URL authority
    in brackets, so the announced URL and the Host/Origin comparisons must both use this form."""
    bare = host.strip("[]")
    try:
        if ipaddress.ip_address(bare).version == 6:
            return f"[{bare}]"
    except ValueError:
        pass
    return bare


def _endpoint_authorities(host: str, port: int, extra: list[str] | None = None) -> frozenset[str]:
    """Every `Host:`/`Origin:` authority that legitimately addresses this endpoint."""
    # The bound host ALWAYS names itself. Listing only _LOOPBACK_NAMES would make the guard
    # refuse the endpoint's own clients on any loopback address other than 127.0.0.1 — the whole
    # of 127.0.0.0/8 is loopback, so `--host 127.0.0.2` would 403 a client whose Host is exactly
    # what it dialled. The aliases are added on top, not instead.
    bases = [_bracketed(host)]
    if _is_loopback(host):
        bases += list(_LOOPBACK_NAMES)
    names = set()
    for base in bases:
        names.add(f"{base}:{port}".lower())
        if port in (80, 443):
            names.add(base.lower())  # browsers omit the default port from Origin
    for value in extra or ():
        # Accept a full origin (`https://app.example`) or a bare authority (`app.example:443`).
        authority = (urlsplit(value).netloc or value).strip().lower()
        if _host_only(authority) == "null":
            # `Origin: null` is what a sandboxed iframe (and a few opaque origins) send — it
            # names no host, so allowing it would admit ANY such context, not one trusted peer.
            # It parses out of `null`, `https://null`, `null:443` and `https://null:443` alike,
            # so the check is on the parsed HOST rather than the raw spelling.
            raise ValueError(
                f"--allowed-origin {value!r} resolves to the opaque origin 'null', which names "
                "no host: allowing it would admit any sandboxed iframe. Pass the real origin."
            )
        names.add(authority)
    return frozenset(names)


def _host_only(authority: str) -> str:
    """An authority minus a trailing `:port`.

    Needed because the `null` checks below compare a HOST, and `urlsplit` does not give one for
    a bare authority: `null:443` splits to no netloc at all, so the fallback keeps the port and
    `== "null"` misses it. Bracketed IPv6 keeps its brackets and inner colons — `[::1]:443` is a
    host of `[::1]`, and rpartitioning on `:` would otherwise shred it.
    """
    if authority.startswith("["):
        end = authority.find("]")
        return authority[: end + 1] if end != -1 else authority
    host, sep, port = authority.rpartition(":")
    return host if sep and port.isdigit() else authority


def _authority_of(origin: str) -> str:
    """Authority part of an Origin header. `null` (sandboxed iframe) has none and stays `null`,
    which never matches an allowed name — that is the intended outcome, not an oversight."""
    return (urlsplit(origin).netloc or origin).strip().lower()


class _OriginGuard:
    """ASGI middleware: reject cross-origin and DNS-rebound requests to the HTTP transport.

    A loopback bind is not a security boundary. Any page the user happens to visit can POST to
    127.0.0.1, and DNS rebinding lets it do so under a hostname it controls — which is why the
    MCP HTTP guidance is that a local server MUST validate `Origin`. FastMCP 3.4 ships no such
    guard (`host_origin_protection` appears nowhere in the package), so Spelunk supplies one
    rather than documenting a protection it does not have.

    Two checks:
      * `Origin`, when present, must name this endpoint. Browsers set it on exactly the
        cross-origin requests an attack would use; a normal MCP client sends none at all, so
        this costs a CLI client nothing.
      * `Host` must name this endpoint too — but only when we bound loopback and therefore KNOW
        every name that can legitimately reach us. A rebound request carries the attacker's
        hostname, so this is the rebinding defence proper rather than a CORS nicety. On a
        non-loopback bind the set of valid names is whatever DNS says, which we cannot
        enumerate, so the Host check is skipped and `Origin` carries the weight.

    A rejection is announced on stderr, not just in the 403 body. The body is the only place the
    reason used to appear, and no MCP client shows it — a tunnelled client (ngrok, a reverse
    proxy) forwards its own `Host`/`Origin`, trips the guard, and the user sees nothing but
    "server disconnected". The log names the header, the value that arrived, and the
    `--allowed-origin` flag that would admit it, so the fix is readable off the terminal rather
    than deduced from this source file. Each distinct (header, value) pair is logged once — a
    scanner hammering the port must not drown the one line that matters.
    """

    def __init__(self, app, *, allowed: frozenset[str], check_host: bool) -> None:
        self.app = app
        self.allowed = allowed
        self.check_host = check_host
        self._announced: set[tuple[str, str]] = set()
        self._suppressed = False

    def _rejections(self, headers: dict[str, str]) -> list[tuple[str, str]]:
        """EVERY header that failed, with the value it carried; empty to allow.

        All of them, not the first: a tunnelled browser client gets both wrong at once (ngrok
        forwards its own `Host`, the client sends its own `Origin`), and reporting only the first
        costs the user a restart to discover the second.
        """
        failed = []
        origin = headers.get("origin")
        if origin is not None and _authority_of(origin) not in self.allowed:
            failed.append(("Origin", origin.strip()))
        if self.check_host:
            host = headers.get("host")
            if host is not None and host.strip().lower() not in self.allowed:
                failed.append(("Host", host.strip()))
        return failed

    def _announce(self, header: str, value: str) -> None:
        if (header, value) in self._announced:
            return
        if len(self._announced) >= _ANNOUNCE_LIMIT:
            # The dedupe set is fed by an attacker-controllable header on a pre-auth path, so it
            # cannot grow without bound. Say so once, then go quiet: the diagnostic exists for
            # the first few distinct values, and a flood is itself the thing worth reporting.
            if not self._suppressed:
                self._suppressed = True
                print(
                    f"[spelunk] {_ANNOUNCE_LIMIT} distinct rejected Origin/Host values seen; "
                    "further 403 diagnostics suppressed for this process.",
                    file=sys.stderr,
                )
            return
        self._announced.add((header, value))
        print(
            f"[spelunk] 403: {header}: {value} does not name this endpoint (DNS-rebinding "
            f"guard). Allowed: {', '.join(sorted(self.allowed))}."
            + self._advice(header, value),
            file=sys.stderr,
        )

    def _advice(self, header: str, value: str) -> str:
        """The paste-ready fix — omitted when there isn't one.

        `Origin: null` (a sandboxed iframe) names no host, so no --allowed-origin value can
        admit that client without admitting every other opaque origin too. Suggesting one would
        coach the user into `https://null`, which parses to the authority `null` and matches the
        very header this guard is documented to refuse. Say why instead.
        """
        if header == "Origin" and _host_only(_authority_of(value)) == "null":
            return (
                " This client sent the opaque origin 'null' (a sandboxed iframe); it names no "
                "host, so it cannot be allowlisted. Give it a real origin."
            )
        # An Origin arrives as a full origin and is echoed as-is; a Host is a bare authority, so
        # give it a scheme to match how --allowed-origin is normally written (either form parses).
        suggestion = value if "://" in value else f"https://{value}"
        return (
            f" If this client is legitimate (a tunnel or reverse proxy forwards its own "
            f"{header}), restart with --allowed-origin {suggestion}"
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        rejections = self._rejections(headers)
        if not rejections:
            await self.app(scope, receive, send)
            return
        for header, value in rejections:
            self._announce(header, value)
        # The body names every failing header for the same reason the log does, but stays a
        # single `error` string — no client renders it, and the shape is what tests pin.
        reason = " and ".join(header for header, _ in rejections)
        noun = "header does" if len(rejections) == 1 else "headers do"
        body = json.dumps(
            {"error": f"{reason} {noun} not name this endpoint (DNS-rebinding guard)"}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def main() -> None:
    """CLI entry point: build a DuckSession from --source specs, serve over stdio."""
    parser = argparse.ArgumentParser(
        description="Spelunk MCP server — one DuckDB engine over files and databases."
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "A data source, repeatable. A file path (.csv/.parquet/.json/.xlsx/.avro/.yaml/.yml) "
            "— local or a remote https:// / s3:// / gs:// / az:// URL — a SQLite file, a delta:<path> / "
            "iceberg:<path> lakehouse table, a sqlite:// / postgresql:// / mysql:// / ducklake: "
            "DSN, or a REST/JSON API: 'api:<url> [key=value ...]' is fetched once at startup into "
            "a local snapshot (options incl. records=<dot.path>, "
            "paginate=page|offset|cursor|keyset|link|odata plus the paging param names the API "
            "actually uses — offset_param=/size_param=/page_param=/cursor_param=/page_size=; "
            "auth via auth_env=<ENV>, "
            "header=<Name>:<ENV>, param=<name>:<ENV>); or 'openapi:<url-or-path>' for an "
            "OpenAPI 3.x spec as a queryable endpoint catalog. Prefix with your chosen name "
            "and '=' to set the source name, e.g. sales=./sales.parquet (the word before '=' "
            "is the name itself, not the literal token 'name'). "
            "For a file with an odd/absent extension, force the reader with a format prefix "
            "(csv:/tsv:/json:/parquet:/excel:/avro:/yaml: or yml:), e.g. "
            "routes=csv:https://host/routes.dat."
        ),
    )
    parser.add_argument("--dsn", default=None, help="Alias for a single --source (back-compat).")
    parser.add_argument(
        "--session-dir",
        default=".spelunk_session",
        help=(
            "Root directory for durable workspaces (created if missing). Each process gets its "
            "own workspace at <session-dir>/<pid>-<rand>/ so concurrent servers never collide. "
            "Default: ./.spelunk_session."
        ),
    )
    parser.add_argument(
        "--shared-workspace",
        action="store_true",
        help=(
            "Use one shared <session-dir>/workspace.duckdb instead of a per-process subdir. "
            "Results persist across restarts and are visible to a later server on the same dir, "
            "but concurrent servers contend for the single-writer lock (only the first is "
            "durable; the rest fall back to ephemeral)."
        ),
    )
    parser.add_argument(
        "--keep-workspaces",
        type=int,
        default=3,
        metavar="N",
        help=(
            "On startup, keep the N most recent per-process workspaces under --session-dir and "
            "reclaim older ones with no live owner. Empty workspaces (no results, no logged "
            "calls) are reclaimed regardless of N. Default: 3. 0 or less keeps everything. "
            "No-op with --shared-workspace."
        ),
    )
    parser.add_argument(
        "--allow-add-source",
        action="store_true",
        help=(
            "Register the add_source / remove_source / fetch tools so the agent can attach and "
            "detach data sources at runtime and call endpoints of an attached API connection. "
            "This lets the agent read any file/database the server process can reach — only "
            "enable it for a trusted, process-per-agent setup. Off by default."
        ),
    )
    parser.add_argument(
        "--require-descriptions",
        action="store_true",
        help=(
            "Require a one-line, plain-English description (~20 words, for a non-technical "
            "reader) on every query and every batch step. Descriptions are stored in lineage and "
            "surfaced by the lineage / catalog tools, so a flow reads as a clear, sequential "
            "story. Off by default (the description parameter is then absent entirely)."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help=(
            "How the MCP protocol is carried. 'stdio' (default) speaks JSON-RPC over the "
            "process's stdin/stdout — the shape a command-launched client (Claude Code's "
            ".mcp.json) expects. 'http' serves streamable HTTP on --host/--port instead, for a "
            "client that connects to a URL. See --host/--port/--http-path."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Interface to bind with --transport http. Default 127.0.0.1 (loopback only). "
            "'0.0.0.0' exposes the server to the network — the tools are read-only SELECTs, but "
            "they read every configured source, so only do that on a trusted network."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Port to bind with --transport http. Default: 8080.",
    )
    parser.add_argument(
        "--http-path",
        default="/mcp",
        help="URL path the MCP endpoint is mounted at with --transport http. Default: /mcp.",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "Extra Origin/Host authority to accept with --transport http, repeatable. The "
            "endpoint's own names are always allowed; this is the escape hatch for a browser "
            "client reaching a non-loopback bind, which the DNS-rebinding guard would otherwise "
            "refuse. Accepts a full origin (https://app.example) or a bare authority."
        ),
    )
    parser.add_argument("--memory-limit", default=None, help="DuckDB memory_limit, e.g. 4GB.")
    parser.add_argument("--temp-dir", default=None, help="Directory for DuckDB spill files.")
    parser.add_argument("--max-temp-size", default=None, help="Cap on spill size, e.g. 50GB.")
    parser.add_argument(
        "--tool-log",
        default=None,
        metavar="PATH",
        help=(
            "Where to write one JSON line per tool call for usage analysis. A file path appends "
            "there; '-' writes to stderr; 'off' disables. Default: <session-dir>/tool-calls.jsonl "
            "when --session-dir is set, otherwise stderr."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help=(
            "Load KEY=VALUE lines from this file into the server's environment before opening "
            "sources (already-set variables win; missing file is a warning, not an error). This "
            "is how api: source credentials (auth_env=/header=/param=) reach the server without "
            "putting secrets in a checked-in MCP config — point it at a gitignored .env."
        ),
    )
    args = parser.parse_args()

    if not args.http_path.startswith("/"):
        # Fail fast rather than at bind time: Starlette routes must be rooted, and the announced
        # URL would silently read `...:8080mcp` — a broken address the user would copy.
        parser.error(f"--http-path must start with '/': got {args.http_path!r}")

    allowed_authorities: frozenset[str] = frozenset()
    if args.transport == "http":
        # Resolve here, before any session is opened: a rejected --allowed-origin should be an
        # argparse error, not a traceback out of the middleware with a workspace already created.
        try:
            allowed_authorities = _endpoint_authorities(
                args.host, args.port, args.allowed_origin
            )
        except ValueError as exc:
            parser.error(str(exc))

    if args.transport == "http" and args.allow_add_source:
        # Worth saying out loud: --allow-add-source is documented as sound *because* of the
        # process-per-agent model — each agent's stdio server is its own process, so an
        # add/remove touches only that connection. One HTTP server serves every client that
        # connects, from one DuckSession, so that isolation no longer holds.
        print(
            "[spelunk] warning: --allow-add-source over HTTP — every client sharing this "
            f"endpoint shares one session, and can attach any file/DSN reachable from this "
            f"process (bound to {args.host}).",
            file=sys.stderr,
        )

    if args.env_file:
        _load_env_file(args.env_file)

    specs = list(args.source)
    if args.dsn:
        specs.append(args.dsn)

    session = DuckSession.open(
        specs,
        session_dir=args.session_dir,
        per_process=not args.shared_workspace,
        keep_workspaces=args.keep_workspaces,
        memory_limit=args.memory_limit,
        temp_dir=args.temp_dir,
        max_temp_size=args.max_temp_size,
    )

    # Resolve the tool-log AFTER open(): by default the durable dir is a per-process subdir
    # chosen inside open(), so the default log belongs there, not under the --session-dir root.
    if args.tool_log == "off":
        tool_log: str | None = None
    elif args.tool_log:
        tool_log = args.tool_log
    elif args.session_dir:
        tool_log = str(Path(session.workspace_dir) / "tool-calls.jsonl")
    else:
        tool_log = "-"  # stderr

    server = build_server(
        session,
        tool_log=tool_log,
        allow_add_source=args.allow_add_source,
        require_descriptions=args.require_descriptions,
    )
    try:
        if args.transport == "http":
            from starlette.middleware import Middleware

            # FastMCP's own banner can be suppressed (FASTMCP_SHOW_SERVER_BANNER=0), so print the
            # URL ourselves — on stderr, which is safe in either transport. An IPv6 literal is
            # only a valid authority in brackets, hence _bracketed.
            print(
                f"[spelunk] MCP over HTTP: "
                f"http://{_bracketed(args.host)}:{args.port}{args.http_path}",
                file=sys.stderr,
            )
            guard = Middleware(
                _OriginGuard,
                allowed=allowed_authorities,  # validated at parse time, above
                check_host=_is_loopback(args.host),
            )
            server.run(
                transport="http",
                host=args.host,
                port=args.port,
                path=args.http_path,
                middleware=[guard],
            )
        else:
            server.run(transport="stdio")
    finally:
        # Clean shutdown — stdio: the client closed stdin; http: uvicorn caught SIGINT/SIGTERM and
        # returned. Release the tool-log file handle so the dir is deletable on Windows, then let
        # the session reclaim its own workspace if this run never did any work — reconnect churn
        # then leaves no empty <pid>-<rand> dirs behind. A hard kill skips this; the next
        # server's startup sweep reclaims the dir instead.
        _configure_tool_logging(None)
        session.close(reclaim_if_empty=True)


if __name__ == "__main__":
    main()
