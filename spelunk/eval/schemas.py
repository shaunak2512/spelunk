"""Frozen data schemas for the eval layer.

These define ``questions.jsonl`` and ``results.csv`` (see ``data/README.md``). The whole
eval pipeline — dataset, score, runner, report — is built against these, so they can be
implemented in parallel BEFORE the agent exists. Changing them is a barrier-level event.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Difficulty = Literal["simple", "moderate", "challenging"]


class BirdQuestion(BaseModel):
    """One row of the frozen eval dataset (``questions.jsonl``)."""

    question_id: int
    db_id: str
    question: str
    evidence: str | None = None  # BIRD 'evidence' / external-knowledge hint
    gold_sql: str
    difficulty: Difficulty


class RunResult(BaseModel):
    """One row of ``results.csv`` — a single ``(model, rung, question)`` run."""

    run_id: str  # convention: f"{model}|{rung}|{question_id}"
    question_id: int
    db_id: str
    difficulty: Difficulty
    model: str  # name from configs/models.yaml
    rung: str  # name from configs/rungs.yaml
    predicted_sql: str | None
    ex_correct: bool  # execution-accuracy verdict
    n_llm_calls: int = 0
    n_tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd_cost: float = 0.0
    latency_s: float = 0.0
    error: str | None = None


# Stable column order for results.csv.
RESULTS_CSV_COLUMNS = [
    "run_id",
    "question_id",
    "db_id",
    "difficulty",
    "model",
    "rung",
    "predicted_sql",
    "ex_correct",
    "n_llm_calls",
    "n_tool_calls",
    "prompt_tokens",
    "completion_tokens",
    "usd_cost",
    "latency_s",
    "error",
]
