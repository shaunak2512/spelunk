"""Drive the Spelunk eval matrix against the frozen BIRD slice (Wave 3).

This is the CLI that actually *runs* the benchmark: it wires the tested
:func:`spelunk.eval.runner.run_matrix` to the real model loader, the BIRD databases,
and an OpenAI-backed schema-RAG embedder, then writes ``results.csv`` and prints an
accuracy/cost summary.

It MAKES PAID LLM CALLS. Use ``--limit`` for a cheap smoke before the full matrix.

Examples::

    # Smoke: 5 questions, cheap models, all rungs
    python scripts/run_eval.py --limit 5 --tier cheap

    # Full lean matrix (cheap models all rungs + frontier on R0)
    python scripts/run_eval.py

Credentials come from ``.env`` (ANTHROPIC_API_KEY, OPENAI_API_KEY), loaded here with a
tiny stdlib parser so no python-dotenv dependency is needed.
"""
from __future__ import annotations

import argparse
import functools
import os
from pathlib import Path

from spelunk.agent.graph import run_agent
from spelunk.agent.models import load_model, load_models_config, usd_cost
from spelunk.agent.rungs import load_rungs
from spelunk.core.connection import connect
from spelunk.eval.dataset import read_jsonl
from spelunk.eval.runner import (
    ResultCache,
    bird_db_path,
    bird_dsn,
    run_matrix,
    write_results_csv,
)
from spelunk.rag.schema_index import SchemaIndex, openai_embed_fn


def load_dotenv(path: str = ".env") -> None:
    """Minimal ``.env`` loader: KEY=VALUE lines, ignoring comments/blanks.

    Existing environment variables win (so a real shell export is not clobbered).
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bird-root", default="data/bird/dev_20240627")
    parser.add_argument("--questions", default="data/questions.jsonl")
    parser.add_argument("--out", default="results/results.csv")
    parser.add_argument("--cache", default="results/cache",
                        help="Response-cache dir; re-runs reuse completed cells. Empty to disable.")
    parser.add_argument("--models", default=None,
                        help="Comma-separated model names from models.yaml (default: by --tier).")
    parser.add_argument("--tier", default=None, choices=["cheap", "frontier"],
                        help="Restrict to a tier (default: all models in models.yaml).")
    parser.add_argument("--rungs", default=None,
                        help="Comma-separated rung names (default: all in rungs.yaml).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Use only the first N frozen questions (smoke runs).")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-probe-rows", type=int, default=50)
    parser.add_argument("--embed-model", default="text-embedding-3-small")
    args = parser.parse_args()

    load_dotenv()
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if not os.environ.get(key):
            print(f"WARNING: {key} not set — calls to that vendor will fail.")

    # --- select models ---
    all_models = load_models_config()
    if args.models:
        wanted = [m.strip() for m in args.models.split(",")]
        specs = [all_models[name] for name in wanted]
    elif args.tier:
        specs = [m for m in all_models.values() if m.tier == args.tier]
    else:
        specs = list(all_models.values())

    # --- select rungs ---
    all_rungs = load_rungs()
    if args.rungs:
        wanted_r = [r.strip() for r in args.rungs.split(",")]
        rungs = [all_rungs[name] for name in wanted_r]
    else:
        rungs = list(all_rungs.values())

    # --- questions ---
    questions = read_jsonl(args.questions)
    if args.limit is not None:
        questions = questions[: args.limit]

    root = args.bird_root

    # --- seams (memoised so each db builds its engine / index once) ---
    @functools.lru_cache(maxsize=None)
    def engine_for(db_id: str):
        return connect(bird_dsn(root, db_id))

    def db_path_for(db_id: str) -> str:
        return bird_db_path(root, db_id)

    embed_fn = openai_embed_fn(args.embed_model)

    @functools.lru_cache(maxsize=None)
    def schema_index_for(db_id: str) -> SchemaIndex:
        idx = SchemaIndex(embed_fn=embed_fn)
        idx.build(engine_for(db_id))
        return idx

    cache = ResultCache(args.cache) if args.cache else None

    n_cells_est = sum(
        len(questions)
        for s in specs
        for r in rungs
        if not (s.tier == "frontier" and r.schema_mode != "dump")
    )
    print(f"Running {n_cells_est} cells: {len(specs)} model(s) x {len(rungs)} rung(s) "
          f"x {len(questions)} question(s) (frontier=R0 only).")
    print(f"  models: {[s.name for s in specs]}")
    print(f"  rungs:  {[r.name for r in rungs]}")
    print()

    state = {"i": 0, "ok": 0, "cost": 0.0}

    def progress(rr) -> None:
        state["i"] += 1
        state["ok"] += int(rr.ex_correct)
        state["cost"] += rr.usd_cost
        mark = "OK " if rr.ex_correct else ("ERR" if rr.error else "  x")
        err = f"  [{rr.error[:60]}]" if rr.error else ""
        print(f"[{state['i']:>3}/{n_cells_est}] {mark} {rr.model:<16} {rr.rung:<16} "
              f"q{rr.question_id:<6} {rr.db_id:<22} "
              f"${rr.usd_cost:.4f} {rr.latency_s:.1f}s{err}")

    results = run_matrix(
        questions, specs, rungs,
        engine_for=engine_for, db_path_for=db_path_for,
        model_loader=load_model, cost_fn=usd_cost,
        schema_index_for=schema_index_for, cache=cache,
        max_steps=args.max_steps, max_probe_rows=args.max_probe_rows,
        progress=progress,
    )

    write_results_csv(results, args.out)

    # --- summary ---
    print("\n" + "=" * 60)
    print(f"Wrote {len(results)} rows -> {args.out}")
    total_ok = sum(r.ex_correct for r in results)
    total_cost = sum(r.usd_cost for r in results)
    n_err = sum(1 for r in results if r.error)
    print(f"Execution accuracy: {total_ok}/{len(results)} = {total_ok/len(results):.1%}")
    print(f"Total cost: ${total_cost:.4f}   |   errors: {n_err}")

    print("\nAccuracy by model x rung:")
    cells: dict[tuple[str, str], list] = {}
    for r in results:
        cells.setdefault((r.model, r.rung), []).append(r)
    for (model, rung), rows in sorted(cells.items()):
        ok = sum(x.ex_correct for x in rows)
        cost = sum(x.usd_cost for x in rows)
        print(f"  {model:<16} {rung:<16} {ok}/{len(rows)} = {ok/len(rows):>5.1%}  ${cost:.4f}")


if __name__ == "__main__":
    main()
