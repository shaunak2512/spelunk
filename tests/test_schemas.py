"""The frozen eval schemas are implemented now, so these should pass immediately —
the GREEN baseline that proves the data contracts work."""
from __future__ import annotations

from spelunk.eval.schemas import RESULTS_CSV_COLUMNS, BirdQuestion, RunResult


def test_bird_question_roundtrip():
    q = BirdQuestion(
        question_id=1,
        db_id="shop",
        question="How many customers are there?",
        gold_sql="SELECT COUNT(*) FROM customers",
        difficulty="simple",
    )
    assert BirdQuestion(**q.model_dump()) == q


def test_run_result_defaults_and_fields():
    r = RunResult(
        run_id="claude-haiku|R0_baseline|1",
        question_id=1,
        db_id="shop",
        difficulty="simple",
        model="claude-haiku",
        rung="R0_baseline",
        predicted_sql="SELECT COUNT(*) FROM customers",
        ex_correct=True,
    )
    assert r.usd_cost == 0.0
    assert r.n_llm_calls == 0


def test_results_csv_columns_match_model():
    model_fields = set(RunResult.model_fields)
    assert set(RESULTS_CSV_COLUMNS) == model_fields
