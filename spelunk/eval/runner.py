"""The eval matrix runner (Wave 3).

Drives the ``(model × rung × question)`` grid, turning each cell into a
:class:`spelunk.eval.schemas.RunResult` row of ``results.csv``. It glues together the
pieces built in earlier waves:

  * the agent loop (:func:`spelunk.agent.graph.run_agent`) produces a predicted SQL +
    telemetry,
  * the scorer (:func:`spelunk.eval.score.execution_accuracy`) decides ``ex_correct``,
  * cost math (:func:`spelunk.agent.models.usd_cost`) turns tokens into USD.

Design notes that matter for a *benchmark* (not a product):

  * **Matrix shape (spec §3.3).** Cheap-tier models run every rung; frontier-tier models
    run only the bare baseline (``schema_mode == "dump"``, i.e. R0) — they are the
    "expensive, no harness" reference bar, so spending frontier tokens on R1/R2 is waste.
  * **Response cache (spec §3.5).** Each cell's result is cached by ``run_id``
    (``f"{model}|{rung}|{question_id}"``) so re-runs are free — a *dev-cost* measure, not
    a product feature. A finished cell is never recomputed.
  * **Never crash the run.** An agent failure on one cell is captured into that row's
    ``error`` and scored ``ex_correct=False``; the matrix keeps going.

Every external dependency (the agent fn, the model loader, the cost fn, per-db engine and
db-path resolvers) is injectable, so the whole runner is exercised offline with fakes.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from spelunk.agent.graph import run_agent
from spelunk.agent.models import load_model, usd_cost
from spelunk.eval.schemas import RESULTS_CSV_COLUMNS, BirdQuestion, RunResult
from spelunk.eval.score import execution_accuracy

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from spelunk.agent.models import ModelSpec
    from spelunk.agent.rungs import RungConfig
    from spelunk.rag.schema_index import SchemaIndex


# --------------------------------------------------------------------------- #
# Response cache — one JSON file per cell, keyed by run_id.
# --------------------------------------------------------------------------- #
class ResultCache:
    """A filesystem cache of completed ``RunResult`` rows, keyed by ``run_id``.

    Makes re-runs free: a cell already on disk is loaded instead of recomputed. The
    cache is content-addressed by the deterministic ``run_id``
    (``model|rung|question_id``); change the model/rung/question set and you simply get
    new keys. Disable caching by passing ``cache=None`` to :func:`run_matrix`.
    """

    def __init__(self, cache_dir: str | Path):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        # run_id contains '|' and arbitrary ints; make a filesystem-safe name.
        safe = run_id.replace("|", "__").replace("/", "_").replace("\\", "_")
        return self.dir / f"{safe}.json"

    def has(self, run_id: str) -> bool:
        return self._path(run_id).exists()

    def get(self, run_id: str) -> RunResult:
        return RunResult.model_validate_json(self._path(run_id).read_text(encoding="utf-8"))

    def put(self, result: RunResult) -> None:
        self._path(result.run_id).write_text(result.model_dump_json(), encoding="utf-8")


# --------------------------------------------------------------------------- #
# BIRD path helpers (the real-run defaults; not used by the offline tests).
# --------------------------------------------------------------------------- #
def bird_db_path(bird_root: str | Path, db_id: str) -> str:
    """Path to a BIRD dev SQLite file: ``<root>/dev_databases/<db_id>/<db_id>.sqlite``."""
    return str(Path(bird_root) / "dev_databases" / db_id / f"{db_id}.sqlite")


def bird_dsn(bird_root: str | Path, db_id: str) -> str:
    """SQLAlchemy DSN for a BIRD dev database."""
    return "sqlite:///" + bird_db_path(bird_root, db_id).replace("\\", "/")


def _is_bare_baseline(rung: "RungConfig") -> bool:
    """R0: full schema dumped, no profiling/RAG — the 'no harness' reference."""
    return rung.schema_mode == "dump"


def _as_list(specs: "Iterable[ModelSpec] | dict[str, ModelSpec]") -> list:
    return list(specs.values()) if isinstance(specs, dict) else list(specs)


def run_matrix(
    questions: list[BirdQuestion],
    model_specs: "Iterable[ModelSpec] | dict[str, ModelSpec]",
    rungs: "Iterable[RungConfig] | dict[str, RungConfig]",
    *,
    engine_for: Callable[[str], "Engine"],
    db_path_for: Callable[[str], str],
    agent_fn: Callable[..., Any] = run_agent,
    model_loader: Callable[[str], Any] = load_model,
    cost_fn: Callable[[str, int, int], float] = usd_cost,
    schema_index_for: Callable[[str], "SchemaIndex"] | None = None,
    cache: ResultCache | None = None,
    max_steps: int = 12,
    max_probe_rows: int = 50,
    progress: Callable[[RunResult], None] | None = None,
) -> list[RunResult]:
    """Run the full ablation matrix and return one ``RunResult`` per executed cell.

    Parameters
    ----------
    engine_for / db_path_for:
        Map a ``db_id`` to a live ``Engine`` (for the agent) and to a SQLite file path
        (for scoring). For a real BIRD run, build these from :func:`bird_dsn` /
        :func:`bird_db_path`.
    agent_fn / model_loader / cost_fn / schema_index_for:
        Injectable seams (default to the real implementations) so the runner is fully
        testable offline.
    cache:
        A :class:`ResultCache` to skip already-computed cells, or ``None`` to always run.

    Frontier-tier models are restricted to the bare baseline rung (R0).
    """
    specs = _as_list(model_specs)
    rung_list = list(rungs.values()) if isinstance(rungs, dict) else list(rungs)

    results: list[RunResult] = []
    for spec in specs:
        model = None  # lazily loaded only when a non-cached cell actually needs it
        for rung in rung_list:
            if spec.tier == "frontier" and not _is_bare_baseline(rung):
                continue  # frontier = "expensive, no harness" reference: R0 only
            for q in questions:
                run_id = f"{spec.name}|{rung.name}|{q.question_id}"

                if cache is not None and cache.has(run_id):
                    rr = cache.get(run_id)
                    results.append(rr)
                    if progress is not None:
                        progress(rr)
                    continue

                if model is None:
                    model = model_loader(spec.name)

                idx = (
                    schema_index_for(q.db_id)
                    if (rung.rag and schema_index_for is not None)
                    else None
                )

                t0 = time.perf_counter()
                try:
                    ar = agent_fn(
                        engine_for(q.db_id),
                        q.question,
                        model=model,
                        rung=rung,
                        evidence=q.evidence,
                        max_steps=max_steps,
                        max_probe_rows=max_probe_rows,
                        schema_index=idx,
                    )
                    error = ar.error
                    final_sql = ar.final_sql
                    n_llm, n_tool = ar.n_llm_calls, ar.n_tool_calls
                    ptok, ctok = ar.prompt_tokens, ar.completion_tokens
                except Exception as err:  # belt-and-braces; agent_fn shouldn't raise
                    error = f"{type(err).__name__}: {err}"
                    final_sql, n_llm, n_tool, ptok, ctok = None, 0, 0, 0, 0
                latency = time.perf_counter() - t0

                ex_correct = False
                if final_sql:
                    ex_correct = execution_accuracy(final_sql, q.gold_sql, db_path_for(q.db_id))

                rr = RunResult(
                    run_id=run_id,
                    question_id=q.question_id,
                    db_id=q.db_id,
                    difficulty=q.difficulty,
                    model=spec.name,
                    rung=rung.name,
                    predicted_sql=final_sql,
                    ex_correct=ex_correct,
                    n_llm_calls=n_llm,
                    n_tool_calls=n_tool,
                    prompt_tokens=ptok,
                    completion_tokens=ctok,
                    usd_cost=cost_fn(spec.name, ptok, ctok),
                    latency_s=latency,
                    error=error,
                )

                if cache is not None:
                    cache.put(rr)
                results.append(rr)
                if progress is not None:
                    progress(rr)

    return results


def write_results_csv(results: list[RunResult], path: str | Path) -> None:
    """Write ``results`` to ``path`` as ``results.csv`` in the frozen column order."""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_CSV_COLUMNS)
        writer.writeheader()
        for rr in results:
            row = rr.model_dump()
            # bool/None render predictably for pandas re-read in report.py.
            writer.writerow({col: row[col] for col in RESULTS_CSV_COLUMNS})
