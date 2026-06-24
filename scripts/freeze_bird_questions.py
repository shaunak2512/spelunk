"""Freeze the BIRD dev set into the reproducible ``data/questions.jsonl`` (Wave 3 scaffolding).

This is the one command that turns a downloaded BIRD dev set into the frozen eval slice
the runner consumes. It is a thin CLI over the already-tested
:mod:`spelunk.eval.dataset` functions — parse ``dev.json`` → stratified-sample ~150
questions across difficulty (and optionally a handful of databases) → write JSONL.

Usage (from the repo root)::

    python scripts/freeze_bird_questions.py --bird-root data/bird --out data/questions.jsonl
    python scripts/freeze_bird_questions.py --bird-root data/bird --n 150 --seed 0 \
        --dbs financial california_schools card_games

``--bird-root`` may point either at the directory containing ``dev.json`` or at the
``dev.json`` file itself (``dataset.load_bird_dev`` accepts both). The sample is
deterministic given ``--seed`` so the frozen file is reproducible.

No LLM calls, no API keys — this only reads the local dataset.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from spelunk.eval.dataset import load_bird_dev, stratified_sample, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bird-root",
        default="data/bird",
        help="BIRD dev directory (containing dev.json) or the dev.json file itself.",
    )
    parser.add_argument(
        "--out",
        default="data/questions.jsonl",
        help="Output JSONL path for the frozen sample.",
    )
    parser.add_argument("--n", type=int, default=150, help="Number of questions to sample.")
    parser.add_argument("--seed", type=int, default=0, help="Deterministic sample seed.")
    parser.add_argument(
        "--dbs",
        nargs="*",
        default=None,
        help="Optional db_id allow-list to restrict the pool (default: all databases).",
    )
    args = parser.parse_args()

    all_qs = load_bird_dev(Path(args.bird_root))
    print(f"Loaded {len(all_qs)} BIRD dev questions from {args.bird_root}")

    sample = stratified_sample(all_qs, n=args.n, seed=args.seed, dbs=args.dbs)
    write_jsonl(sample, args.out)

    by_difficulty = Counter(q.difficulty for q in sample)
    by_db = Counter(q.db_id for q in sample)
    print(f"Froze {len(sample)} questions -> {args.out}")
    print("  by difficulty: " + ", ".join(f"{k}={v}" for k, v in sorted(by_difficulty.items())))
    print(f"  across {len(by_db)} databases: "
          + ", ".join(f"{k}={v}" for k, v in by_db.most_common()))


if __name__ == "__main__":
    main()
