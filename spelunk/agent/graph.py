"""The exploration agent (Wave 3).

A tool-calling ("ReAct") loop that lets a model explore a database and emit a single
SQL answer. The build spec (§3.1) sanctions starting from LangGraph's
``create_react_agent`` and "dropping to a hand-wired loop if you need tighter control":
we need that tighter control here — exact telemetry (LLM/tool-call counts, token
usage), a hard ``max_steps`` cap, a hard probe-row cap, and a ``submit_sql`` terminator
— so this is a small, explicit loop over ``langchain_core`` message primitives. It is
fully offline-testable: pass any object exposing ``bind_tools`` + ``invoke`` as ``model``.

The four tools come from :func:`spelunk.agent.tools.make_tools`. ``run_query`` is
dispatched here (not via the tool's default) so the probe-row cap is enforced at the
agent boundary — exploration can read at most ``max_probe_rows`` rows, which keeps a
runaway scan from blowing the context window.

Rung wiring (see :mod:`spelunk.agent.rungs`):
  * ``dump``    — the full schema is rendered into the system prompt (R0 baseline).
  * ``explore`` — no dump; the prompt tells the model to use list_tables/describe_table.
  * ``rag``     — when on and a ``SchemaIndex`` is supplied, the top-k relevant tables
                  are retrieved and surfaced as a focus hint (R2).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from spelunk.agent.tools import make_tools
from spelunk.core.introspect import describe, list_objects
from spelunk.core.query import run_sql
from spelunk.core.types import SpelunkError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from spelunk.agent.rungs import RungConfig
    from spelunk.rag.schema_index import SchemaIndex

DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_PROBE_ROWS = 50


class AgentResult(BaseModel):
    """Outcome of one agent run over a single question.

    ``final_sql`` is ``None`` if the agent never called ``submit_sql`` (it ran out of
    steps or answered without submitting). Telemetry fields feed ``RunResult`` rows.
    """

    final_sql: str | None = None
    n_llm_calls: int = 0
    n_tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: int = 0
    error: str | None = None


_BASE_INSTRUCTIONS = (
    "You are a meticulous data analyst answering a question by writing ONE SQLite "
    "SQL query. You have tools to explore the database. Inspect the schema and, when "
    "useful, run small read-only probe queries to ground your answer in the real data "
    "(values, formats, edge cases). Probe results are capped at {max_probe_rows} rows. "
    "When you are confident, call submit_sql exactly once with the final query that "
    "answers the question. Do not call submit_sql until you have the final SQL."
)


def _render_schema_dump(engine: "Engine") -> str:
    """Render the full schema as compact text for the R0 (dump) baseline.

    One line per table: ``TABLE name(col type [PK], ...)`` followed by any foreign keys.
    Profiling is intentionally off here — R0 is the bare text-to-SQL baseline.
    """
    lines: list[str] = []
    for obj in list_objects(engine):
        td = describe(engine, obj.name, profile=False)
        cols = ", ".join(
            f"{c.name} {c.type}" + (" PK" if c.primary_key else "")
            for c in td.columns
        )
        lines.append(f"{obj.kind.upper()} {obj.name}({cols})")
        for fk in td.foreign_keys:
            lines.append(f"  FK {obj.name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}")
    return "\n".join(lines)


def _build_system_prompt(
    engine: "Engine",
    rung: "RungConfig",
    question: str,
    *,
    schema_index: "SchemaIndex | None",
    max_probe_rows: int,
) -> str:
    parts = [_BASE_INSTRUCTIONS.format(max_probe_rows=max_probe_rows)]

    if rung.schema_mode == "dump":
        parts.append(
            "Here is the complete database schema:\n" + _render_schema_dump(engine)
        )
    else:  # explore
        parts.append(
            "The schema is NOT given. Call list_tables to see the tables, then "
            "describe_table on the ones you need before writing SQL."
        )

    if rung.rag and schema_index is not None:
        try:
            top = schema_index.retrieve(question, k=rung.rag_top_k)
        except Exception:
            top = []
        if top:
            parts.append(
                "These tables are most likely relevant to the question; start there: "
                + ", ".join(top)
            )

    return "\n\n".join(parts)


def _user_prompt(question: str, evidence: str | None) -> str:
    if evidence:
        return f"Question: {question}\n\nExternal knowledge / hint: {evidence}"
    return f"Question: {question}"


def _capped_run_query(engine: "Engine", sql: str, max_probe_rows: int) -> str:
    """Execute a probe query with the hard row cap, returning JSON the model can read.

    Guard violations (writes/DDL) and any execution error are returned as a JSON
    ``{"error": ...}`` payload rather than raised, so the agent can read the failure and
    recover within the loop instead of the run crashing.
    """
    try:
        qr = run_sql(engine, sql, max_rows=max_probe_rows)
        return qr.model_dump_json()
    except SpelunkError as err:
        return json.dumps({"error": f"{type(err).__name__}: {err}"})
    except Exception as err:  # pragma: no cover - defensive; DB-driver errors vary
        return json.dumps({"error": f"{type(err).__name__}: {err}"})


def run_agent(
    engine: "Engine",
    question: str,
    *,
    model: Any,
    rung: "RungConfig",
    evidence: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_probe_rows: int = DEFAULT_MAX_PROBE_ROWS,
    schema_index: "SchemaIndex | None" = None,
) -> AgentResult:
    """Run the exploration loop for one question and return the answer + telemetry.

    ``model`` is any object exposing ``bind_tools(tools)`` and ``invoke(messages)``
    (a LangChain ``BaseChatModel``, or a fake in tests). The loop:

      1. binds the four core tools to the model,
      2. seeds messages with a rung-specific system prompt + the question,
      3. invokes the model, executes any tool calls (``run_query`` is row-capped),
      4. terminates when ``submit_sql`` is called or ``max_steps`` is hit.

    Never raises: tool failures are fed back to the model; a model/transport failure is
    captured into ``AgentResult.error`` so a single bad cell can't crash a matrix run.
    """
    tools = make_tools(engine, profile=rung.profile)
    tools_by_name = {t.name: t for t in tools}
    bound = model.bind_tools(tools)

    system = _build_system_prompt(
        engine, rung, question, schema_index=schema_index, max_probe_rows=max_probe_rows
    )
    messages: list[Any] = [
        SystemMessage(content=system),
        HumanMessage(content=_user_prompt(question, evidence)),
    ]

    result = AgentResult()
    try:
        for step in range(max_steps):
            result.steps = step + 1
            ai: AIMessage = bound.invoke(messages)
            result.n_llm_calls += 1

            usage = getattr(ai, "usage_metadata", None) or {}
            result.prompt_tokens += int(usage.get("input_tokens", 0) or 0)
            result.completion_tokens += int(usage.get("output_tokens", 0) or 0)

            messages.append(ai)
            tool_calls = getattr(ai, "tool_calls", None) or []
            if not tool_calls:
                # Model answered without calling a tool — nothing more to do.
                break

            terminated = False
            for call in tool_calls:
                name = call.get("name")
                args = call.get("args", {}) or {}
                call_id = call.get("id")

                if name == "submit_sql":
                    result.final_sql = args.get("sql")
                    messages.append(
                        ToolMessage(
                            content=json.dumps({"sql": result.final_sql}),
                            tool_call_id=call_id,
                        )
                    )
                    terminated = True
                    break

                result.n_tool_calls += 1
                if name == "run_query":
                    out = _capped_run_query(engine, args.get("sql", ""), max_probe_rows)
                elif name in tools_by_name:
                    try:
                        out = tools_by_name[name].invoke(args)
                    except Exception as err:
                        out = json.dumps({"error": f"{type(err).__name__}: {err}"})
                else:
                    out = json.dumps({"error": f"unknown tool: {name}"})
                messages.append(ToolMessage(content=out, tool_call_id=call_id))

            if terminated:
                break
    except Exception as err:  # model/transport failure — record, don't propagate.
        result.error = f"{type(err).__name__}: {err}"

    return result
