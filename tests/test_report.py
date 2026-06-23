"""Acceptance tests for spelunk.eval.report.

Builds a synthetic results.csv (rows = RunResult; columns = RESULTS_CSV_COLUMNS):
two cheap models across three rungs, plus two frontier-tier models at R0 only.
Asserts the aggregation math and that the plot functions write PNG files.
"""
from __future__ import annotations

import pandas as pd

from spelunk.eval import report
from spelunk.eval.schemas import RESULTS_CSV_COLUMNS, RunResult

RUNGS = ["R0_baseline", "R1_schema", "R2_tools"]
CHEAP_MODELS = ["claude-haiku", "gpt-4o-mini"]
FRONTIER_MODELS = ["claude-opus", "gpt-4o"]


def _row(model: str, rung: str, qid: int, *, ex_correct: bool, usd_cost: float) -> dict:
    """One fully-populated RunResult row (validated, then dumped to a dict)."""
    return RunResult(
        run_id=f"{model}|{rung}|{qid}",
        question_id=qid,
        db_id="shop",
        difficulty="simple",
        model=model,
        rung=rung,
        predicted_sql="SELECT 1",
        ex_correct=ex_correct,
        n_llm_calls=1,
        n_tool_calls=0,
        prompt_tokens=100,
        completion_tokens=20,
        usd_cost=usd_cost,
        latency_s=0.5,
        error=None,
    ).model_dump()


def _synthetic_rows() -> list[dict]:
    """8 rows: 2 cheap models x 3 rungs (6) + 2 frontier @ R0 (2).

    ex_correct pattern is deterministic so the tests can assert exact means:
      - claude-haiku:  R0 -> False, R1 -> True,  R2 -> True   (mean 0, 1, 1)
      - gpt-4o-mini:   R0 -> True,  R1 -> False, R2 -> True   (mean 1, 0, 1)
      - frontier @ R0: both True
    """
    rows: list[dict] = []
    correctness = {
        ("claude-haiku", "R0_baseline"): False,
        ("claude-haiku", "R1_schema"): True,
        ("claude-haiku", "R2_tools"): True,
        ("gpt-4o-mini", "R0_baseline"): True,
        ("gpt-4o-mini", "R1_schema"): False,
        ("gpt-4o-mini", "R2_tools"): True,
    }
    qid = 1
    for model in CHEAP_MODELS:
        for rung in RUNGS:
            rows.append(
                _row(
                    model,
                    rung,
                    qid,
                    ex_correct=correctness[(model, rung)],
                    usd_cost=0.01,
                )
            )
            qid += 1
    # Frontier-tier models, R0 only, both correct, pricier.
    for model in FRONTIER_MODELS:
        rows.append(_row(model, "R0_baseline", qid, ex_correct=True, usd_cost=0.10))
        qid += 1
    return rows


def _write_csv(tmp_path) -> str:
    csv_path = tmp_path / "results.csv"
    df = pd.DataFrame(_synthetic_rows(), columns=RESULTS_CSV_COLUMNS)
    df.to_csv(csv_path, index=False)
    return str(csv_path)


def test_load_results_roundtrip(tmp_path):
    csv_path = _write_csv(tmp_path)
    df = report.load_results(csv_path)
    assert list(df.columns) == RESULTS_CSV_COLUMNS
    assert len(df) == 8
    # ex_correct must be a real boolean dtype after the CSV round-trip.
    assert df["ex_correct"].dtype == bool


def test_accuracy_by_rung_shape_and_values(tmp_path):
    df = report.load_results(_write_csv(tmp_path))
    acc = report.accuracy_by_rung(df)

    # index = model, columns = rung
    assert acc.index.name == "model"
    assert set(acc.index) == set(CHEAP_MODELS + FRONTIER_MODELS)
    assert set(acc.columns) == set(RUNGS)

    # Deterministic means from the synthetic pattern.
    assert acc.loc["claude-haiku", "R0_baseline"] == 0.0
    assert acc.loc["claude-haiku", "R1_schema"] == 1.0
    assert acc.loc["claude-haiku", "R2_tools"] == 1.0
    assert acc.loc["gpt-4o-mini", "R0_baseline"] == 1.0
    assert acc.loc["gpt-4o-mini", "R1_schema"] == 0.0
    assert acc.loc["gpt-4o-mini", "R2_tools"] == 1.0
    assert acc.loc["claude-opus", "R0_baseline"] == 1.0

    # Frontier models only ran R0; other rungs are NaN (not 0).
    assert pd.isna(acc.loc["claude-opus", "R1_schema"])
    assert pd.isna(acc.loc["gpt-4o", "R2_tools"])


def test_cost_per_correct_math(tmp_path):
    df = report.load_results(_write_csv(tmp_path))
    cpc = report.cost_per_correct(df)

    assert cpc.index.name == "model"
    # claude-haiku: 3 runs * 0.01 = 0.03 total, 2 correct -> 0.015
    assert cpc.loc["claude-haiku", "cost_per_correct"] == 0.015
    # gpt-4o-mini: 0.03 total, 2 correct -> 0.015
    assert cpc.loc["gpt-4o-mini", "cost_per_correct"] == 0.015
    # claude-opus: 0.10 total, 1 correct -> 0.10
    assert cpc.loc["claude-opus", "cost_per_correct"] == 0.10
    # sanity on the intermediate aggregates
    assert cpc.loc["claude-haiku", "total_cost"] == 0.03
    assert cpc.loc["claude-haiku", "n_correct"] == 2


def test_cost_per_correct_handles_zero_correct(tmp_path):
    # A model that is never correct must not blow up (no ZeroDivisionError / inf).
    rows = _synthetic_rows()
    rows.append(_row("always-wrong", "R0_baseline", 99, ex_correct=False, usd_cost=0.05))
    df = pd.DataFrame(rows, columns=RESULTS_CSV_COLUMNS)
    cpc = report.cost_per_correct(df)
    assert cpc.loc["always-wrong", "n_correct"] == 0
    # cost_per_correct for zero correct should be NaN, never inf.
    val = cpc.loc["always-wrong", "cost_per_correct"]
    assert pd.isna(val)


def test_plot_headline_writes_png(tmp_path):
    df = report.load_results(_write_csv(tmp_path))
    out = tmp_path / "headline.png"
    ret = report.plot_headline(df, out=str(out))
    assert out.exists()
    assert out.stat().st_size > 0
    # Returned path should match what was written.
    assert str(ret) == str(out)


def test_plot_cost_writes_png(tmp_path):
    df = report.load_results(_write_csv(tmp_path))
    out = tmp_path / "cost.png"
    report.plot_cost(df, out=str(out))
    assert out.exists()
    assert out.stat().st_size > 0


def test_plot_headline_creates_missing_dirs(tmp_path):
    df = report.load_results(_write_csv(tmp_path))
    out = tmp_path / "nested" / "deeper" / "headline.png"
    report.plot_headline(df, out=str(out))
    assert out.exists()
