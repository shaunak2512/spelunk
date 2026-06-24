"""Tests for eval.runner — the (model x rung x question) matrix, offline with fakes."""
from __future__ import annotations

from spelunk.agent.graph import AgentResult
from spelunk.agent.models import ModelSpec
from spelunk.agent.rungs import load_rungs
from spelunk.core.connection import connect
from spelunk.eval.runner import (
    ResultCache,
    bird_db_path,
    bird_dsn,
    run_matrix,
    write_results_csv,
)
from spelunk.eval.schemas import RESULTS_CSV_COLUMNS, BirdQuestion


CHEAP = ModelSpec(name="claude-haiku", provider="anthropic", model_id="x",
                  tier="cheap", price_in=1.0, price_out=5.0)
FRONTIER = ModelSpec(name="claude-frontier", provider="anthropic", model_id="y",
                     tier="frontier", price_in=15.0, price_out=75.0)


def _q(qid, sql):
    return BirdQuestion(question_id=qid, db_id="shop", question="q?", gold_sql=sql,
                        difficulty="simple")


def _seams(sample_db):
    """engine_for / db_path_for bound to the conftest sample DB."""
    db_file = sample_db.replace("sqlite:///", "")
    return (lambda db_id: connect(sample_db), lambda db_id: db_file)


def _agent_returning(sql, **tele):
    def fn(engine, question, **kwargs):
        return AgentResult(final_sql=sql, **tele)
    return fn


# --------------------------------------------------------------------------- #
def test_frontier_restricted_to_r0_cheap_runs_all_rungs(sample_db):
    engine_for, db_path_for = _seams(sample_db)
    rungs = load_rungs()
    results = run_matrix(
        [_q(1, "SELECT 1")], [CHEAP, FRONTIER], rungs,
        engine_for=engine_for, db_path_for=db_path_for,
        agent_fn=_agent_returning("SELECT 1"),
        model_loader=lambda name: object(),
    )
    cheap_rungs = sorted(r.rung for r in results if r.model == "claude-haiku")
    frontier_rungs = sorted(r.rung for r in results if r.model == "claude-frontier")
    assert cheap_rungs == ["R0_baseline", "R1_discovery_fs", "R2_schema_rag"]
    assert frontier_rungs == ["R0_baseline"]  # bare reference only
    assert len(results) == 4


def test_scoring_correct_and_incorrect(sample_db):
    engine_for, db_path_for = _seams(sample_db)
    gold = "SELECT name FROM customers ORDER BY id"
    rungs = {k: v for k, v in load_rungs().items() if k == "R0_baseline"}

    good = run_matrix([_q(1, gold)], [CHEAP], rungs,
                      engine_for=engine_for, db_path_for=db_path_for,
                      agent_fn=_agent_returning(gold), model_loader=lambda n: object())
    assert good[0].ex_correct is True

    bad = run_matrix([_q(1, gold)], [CHEAP], rungs,
                     engine_for=engine_for, db_path_for=db_path_for,
                     agent_fn=_agent_returning("SELECT name FROM customers WHERE id < 0"),
                     model_loader=lambda n: object())
    assert bad[0].ex_correct is False


def test_telemetry_and_cost_recorded(sample_db):
    engine_for, db_path_for = _seams(sample_db)
    rungs = {k: v for k, v in load_rungs().items() if k == "R0_baseline"}
    results = run_matrix(
        [_q(1, "SELECT 1")], [CHEAP], rungs,
        engine_for=engine_for, db_path_for=db_path_for,
        agent_fn=_agent_returning("SELECT 1", n_llm_calls=3, n_tool_calls=4,
                                  prompt_tokens=1000, completion_tokens=200),
        model_loader=lambda n: object(),
    )
    rr = results[0]
    assert rr.n_llm_calls == 3 and rr.n_tool_calls == 4
    assert rr.prompt_tokens == 1000 and rr.completion_tokens == 200
    assert rr.usd_cost > 0  # cost math ran via models.yaml prices for claude-haiku
    assert rr.run_id == "claude-haiku|R0_baseline|1"


def test_cache_short_circuits_recompute(sample_db, tmp_path):
    engine_for, db_path_for = _seams(sample_db)
    rungs = {k: v for k, v in load_rungs().items() if k == "R0_baseline"}
    cache = ResultCache(tmp_path / "cache")

    calls = {"n": 0}
    def counting_agent(engine, question, **kwargs):
        calls["n"] += 1
        return AgentResult(final_sql="SELECT 1")

    args = dict(engine_for=engine_for, db_path_for=db_path_for,
                agent_fn=counting_agent, model_loader=lambda n: object(), cache=cache)
    first = run_matrix([_q(1, "SELECT 1")], [CHEAP], rungs, **args)
    second = run_matrix([_q(1, "SELECT 1")], [CHEAP], rungs, **args)

    assert calls["n"] == 1  # second run served entirely from cache
    assert first[0].run_id == second[0].run_id
    assert second[0].predicted_sql == "SELECT 1"


def test_agent_exception_is_captured(sample_db):
    engine_for, db_path_for = _seams(sample_db)
    rungs = {k: v for k, v in load_rungs().items() if k == "R0_baseline"}

    def boom(engine, question, **kwargs):
        raise RuntimeError("kaboom")

    results = run_matrix([_q(1, "SELECT 1")], [CHEAP], rungs,
                         engine_for=engine_for, db_path_for=db_path_for,
                         agent_fn=boom, model_loader=lambda n: object())
    assert results[0].error is not None and "kaboom" in results[0].error
    assert results[0].ex_correct is False
    assert results[0].predicted_sql is None


def test_model_loader_called_once_per_model(sample_db):
    engine_for, db_path_for = _seams(sample_db)
    rungs = load_rungs()  # 3 rungs
    loads = {"n": 0}
    def loader(name):
        loads["n"] += 1
        return object()
    run_matrix([_q(1, "SELECT 1")], [CHEAP], rungs,
               engine_for=engine_for, db_path_for=db_path_for,
               agent_fn=_agent_returning("SELECT 1"), model_loader=loader)
    assert loads["n"] == 1  # one cheap model -> loaded once despite 3 rungs


def test_schema_index_supplied_only_for_rag_rung(sample_db):
    engine_for, db_path_for = _seams(sample_db)
    rungs = load_rungs()
    seen = {}
    def recording_agent(engine, question, **kwargs):
        seen[kwargs["rung"].name] = kwargs.get("schema_index")
        return AgentResult(final_sql="SELECT 1")

    sentinel = object()
    run_matrix([_q(1, "SELECT 1")], [CHEAP], rungs,
               engine_for=engine_for, db_path_for=db_path_for,
               agent_fn=recording_agent, model_loader=lambda n: object(),
               schema_index_for=lambda db_id: sentinel)
    assert seen["R2_schema_rag"] is sentinel
    assert seen["R1_discovery_fs"] is None
    assert seen["R0_baseline"] is None


def test_write_results_csv_roundtrip(sample_db, tmp_path):
    engine_for, db_path_for = _seams(sample_db)
    rungs = load_rungs()
    results = run_matrix([_q(1, "SELECT 1"), _q(2, "SELECT 1")], [CHEAP], rungs,
                         engine_for=engine_for, db_path_for=db_path_for,
                         agent_fn=_agent_returning("SELECT 1"),
                         model_loader=lambda n: object())
    out = tmp_path / "results.csv"
    write_results_csv(results, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == RESULTS_CSV_COLUMNS
    assert len(lines) == 1 + len(results)  # header + one row per cell


def test_bird_path_helpers():
    assert bird_db_path("/data/bird", "financial").replace("\\", "/") == \
        "/data/bird/dev_databases/financial/financial.sqlite"
    assert bird_dsn("/data/bird", "financial") == \
        "sqlite:////data/bird/dev_databases/financial/financial.sqlite"
