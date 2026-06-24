"""Tests for agent.graph — the ReAct loop, offline (a scripted fake chat model)."""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from spelunk.agent.graph import run_agent
from spelunk.agent.rungs import RungConfig, get_rung
from spelunk.core.connection import connect


# --------------------------------------------------------------------------- #
# A scripted fake chat model: no network, returns pre-baked AIMessages.
# --------------------------------------------------------------------------- #
def _ai(name, args, *, id="call-1", usage=(10, 5)):
    """An AIMessage carrying a single tool call + token usage."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": id}],
        usage_metadata={
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    )


class FakeChat:
    def __init__(self, script, *, repeat_last=False, raise_on_invoke=False):
        self.script = list(script)
        self.repeat_last = repeat_last
        self.raise_on_invoke = raise_on_invoke
        self.i = 0
        self.seen = []  # the message list handed to each invoke()
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.seen.append(list(messages))
        if self.raise_on_invoke:
            raise RuntimeError("simulated transport failure")
        if self.i < len(self.script):
            msg = self.script[self.i]
            self.i += 1
            return msg
        if self.repeat_last and self.script:
            return self.script[-1]
        return AIMessage(content="done")  # no tool calls -> loop stops


R0 = None  # lazily fetched in tests
R1 = None


def test_submit_sql_terminates_and_captures_sql(sample_db):
    engine = connect(sample_db)
    model = FakeChat([_ai("submit_sql", {"sql": "SELECT 1"})])
    res = run_agent(engine, "anything", model=model, rung=get_rung("R0_baseline"))
    assert res.final_sql == "SELECT 1"
    assert res.n_llm_calls == 1
    assert res.n_tool_calls == 0
    assert res.error is None


def test_explore_then_submit_counts_tool_calls_and_tokens(sample_db):
    engine = connect(sample_db)
    model = FakeChat(
        [
            _ai("list_tables", {}, id="a"),
            _ai("describe_table", {"name": "orders"}, id="b"),
            _ai("submit_sql", {"sql": "SELECT * FROM orders"}, id="c"),
        ]
    )
    res = run_agent(engine, "list orders", model=model, rung=get_rung("R1_discovery_fs"))
    assert res.final_sql == "SELECT * FROM orders"
    assert res.n_tool_calls == 2  # list_tables + describe_table (submit is not a tool-call)
    assert res.n_llm_calls == 3
    assert res.prompt_tokens == 30  # 3 calls x 10
    assert res.completion_tokens == 15  # 3 calls x 5


def test_run_query_probe_is_row_capped(sample_db):
    engine = connect(sample_db)
    model = FakeChat(
        [
            _ai("run_query", {"sql": "SELECT * FROM customers"}, id="q"),
            _ai("submit_sql", {"sql": "SELECT * FROM customers"}, id="s"),
        ]
    )
    res = run_agent(
        engine, "all customers", model=model,
        rung=get_rung("R1_discovery_fs"), max_probe_rows=2,
    )
    # Find the ToolMessage that fed the run_query result back to the model.
    last_msgs = model.seen[-1]
    tool_payloads = [
        json.loads(m.content)
        for m in last_msgs
        if m.__class__.__name__ == "ToolMessage" and "row_count" in str(m.content)
    ]
    assert tool_payloads, "expected a run_query result to be fed back"
    qr = tool_payloads[0]
    assert qr["row_count"] <= 2
    assert qr["truncated"] is True


def test_max_steps_cap_stops_unsubmitted_loop(sample_db):
    engine = connect(sample_db)
    # Always asks to list_tables, never submits.
    model = FakeChat([_ai("list_tables", {}, id="x")], repeat_last=True)
    res = run_agent(engine, "loop forever", model=model,
                    rung=get_rung("R1_discovery_fs"), max_steps=4)
    assert res.final_sql is None
    assert res.n_llm_calls == 4
    assert res.steps == 4


def test_unsafe_probe_is_returned_as_error_not_raised(sample_db):
    engine = connect(sample_db)
    model = FakeChat(
        [
            _ai("run_query", {"sql": "DELETE FROM orders"}, id="q"),
            _ai("submit_sql", {"sql": "SELECT 1"}, id="s"),
        ]
    )
    res = run_agent(engine, "be naughty", model=model, rung=get_rung("R1_discovery_fs"))
    assert res.error is None  # the loop did not crash
    assert res.final_sql == "SELECT 1"
    last_msgs = model.seen[-1]
    errs = [
        json.loads(m.content)
        for m in last_msgs
        if m.__class__.__name__ == "ToolMessage" and "error" in str(m.content)
    ]
    assert errs and "UnsafeSQLError" in errs[0]["error"]


def test_dump_rung_injects_full_schema(sample_db):
    engine = connect(sample_db)
    model = FakeChat([_ai("submit_sql", {"sql": "SELECT 1"})])
    run_agent(engine, "q", model=model, rung=get_rung("R0_baseline"))
    system = model.seen[0][0].content  # first message of the first invoke
    assert "customers" in system
    assert "orders" in system
    assert "FK" in system  # the orders->customers foreign key is rendered


def test_explore_rung_omits_schema_dump(sample_db):
    engine = connect(sample_db)
    model = FakeChat([_ai("submit_sql", {"sql": "SELECT 1"})])
    run_agent(engine, "q", model=model, rung=get_rung("R1_discovery_fs"))
    system = model.seen[0][0].content
    assert "list_tables" in system
    assert "TABLE customers(" not in system  # no dump in explore mode


def test_rag_rung_surfaces_retrieved_tables(sample_db):
    engine = connect(sample_db)

    class FakeIndex:
        def retrieve(self, question, k=5):
            return ["orders", "customers"][:k]

    rung = RungConfig(name="R2", schema_mode="explore", profile=True, rag=True, rag_top_k=2)
    model = FakeChat([_ai("submit_sql", {"sql": "SELECT 1"})])
    run_agent(engine, "orders by customer", model=model, rung=rung, schema_index=FakeIndex())
    system = model.seen[0][0].content
    assert "most likely relevant" in system
    assert "orders" in system


def test_model_failure_is_captured_not_raised(sample_db):
    engine = connect(sample_db)
    model = FakeChat([], raise_on_invoke=True)
    res = run_agent(engine, "q", model=model, rung=get_rung("R0_baseline"))
    assert res.final_sql is None
    assert res.error is not None
    assert "simulated transport failure" in res.error
