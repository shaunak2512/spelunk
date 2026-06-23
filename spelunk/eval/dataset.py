"""Build the frozen ``questions.jsonl`` from the BIRD dev set.

Rows conform to :class:`spelunk.eval.schemas.BirdQuestion`. See ``data/README.md``
for the frozen file format.

BIRD ``dev.json`` is a JSON array of records using these field names:
``question_id`` (int), ``db_id`` (str), ``question`` (str), ``evidence`` (str,
possibly empty), ``SQL`` (the gold query), and ``difficulty``
(``"simple" | "moderate" | "challenging"``). We map ``SQL -> gold_sql`` and, if a
record lacks ``difficulty``, default it to ``"moderate"``.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from spelunk.eval.schemas import BirdQuestion, Difficulty

# Default difficulty when a BIRD record omits the field (some dev releases do).
_DEFAULT_DIFFICULTY: Difficulty = "moderate"


def _resolve_dev_json(bird_root: Path) -> Path:
    """Accept either the BIRD root dir (containing ``dev.json``) or the file itself."""
    p = Path(bird_root)
    if p.is_dir():
        return p / "dev.json"
    return p


def load_bird_dev(bird_root: Path) -> list[BirdQuestion]:
    """Parse BIRD ``dev.json`` into ``BirdQuestion`` rows.

    Maps ``question_id, db_id, question, evidence, SQL->gold_sql, difficulty``.
    Empty/missing ``evidence`` becomes ``None``; missing ``difficulty`` defaults
    to ``"moderate"``.
    """
    dev_path = _resolve_dev_json(bird_root)
    with dev_path.open(encoding="utf-8") as f:
        records = json.load(f)

    questions: list[BirdQuestion] = []
    for rec in records:
        evidence = rec.get("evidence")
        if evidence is not None and not str(evidence).strip():
            evidence = None  # BIRD uses "" for "no external knowledge".

        questions.append(
            BirdQuestion(
                question_id=int(rec["question_id"]),
                db_id=rec["db_id"],
                question=rec["question"],
                evidence=evidence,
                gold_sql=rec["SQL"],
                difficulty=rec.get("difficulty") or _DEFAULT_DIFFICULTY,
            )
        )
    return questions


def stratified_sample(
    qs: list[BirdQuestion],
    n: int = 150,
    seed: int = 0,
    dbs: list[str] | None = None,
) -> list[BirdQuestion]:
    """Deterministically draw ``n`` questions, stratified by ``difficulty``.

    - Optionally restrict the pool to ``dbs`` (by ``db_id``).
    - Allocation is roughly proportional to each difficulty's share of the pool,
      with largest-remainder rounding so the total equals ``min(n, pool_size)``.
    - Deterministic given ``seed`` (independent of input ordering).
    - Does not mutate ``qs``.
    """
    pool = list(qs)
    if dbs is not None:
        wanted = set(dbs)
        pool = [q for q in pool if q.db_id in wanted]

    if n >= len(pool):
        # Return everything, in a stable (deterministic) order.
        return sorted(pool, key=lambda q: q.question_id)

    # Group by difficulty, each group ordered deterministically.
    strata: dict[str, list[BirdQuestion]] = defaultdict(list)
    for q in pool:
        strata[q.difficulty].append(q)
    for items in strata.values():
        items.sort(key=lambda q: q.question_id)

    total = len(pool)
    # Proportional quota per stratum via largest-remainder (Hamilton) apportionment.
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for diff, items in strata.items():
        exact = n * len(items) / total
        base = int(exact)
        base = min(base, len(items))  # never exceed available
        quotas[diff] = base
        remainders.append((exact - base, diff))

    allocated = sum(quotas.values())
    leftover = n - allocated
    # Distribute leftover seats to strata with the largest fractional remainder
    # (ties broken by difficulty name for determinism), respecting availability.
    remainders.sort(key=lambda t: (-t[0], t[1]))
    i = 0
    guard = 0
    max_guard = leftover * (len(remainders) + 1) + 1
    while leftover > 0 and remainders and guard < max_guard:
        _, diff = remainders[i % len(remainders)]
        if quotas[diff] < len(strata[diff]):
            quotas[diff] += 1
            leftover -= 1
        i += 1
        guard += 1

    rng = random.Random(seed)
    selected: list[BirdQuestion] = []
    for diff in sorted(strata):  # deterministic stratum order
        items = strata[diff]
        k = quotas[diff]
        selected.extend(rng.sample(items, k))

    # Stable final ordering, independent of stratum iteration order.
    selected.sort(key=lambda q: q.question_id)
    return selected


def write_jsonl(qs: list[BirdQuestion], path: Path | str) -> None:
    """Write ``qs`` to ``path`` as JSONL (one ``BirdQuestion`` object per line)."""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for q in qs:
            f.write(q.model_dump_json())
            f.write("\n")


def read_jsonl(path: Path | str) -> list[BirdQuestion]:
    """Read a JSONL file back into ``BirdQuestion`` rows."""
    p = Path(path)
    questions: list[BirdQuestion] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            questions.append(BirdQuestion.model_validate_json(line))
    return questions
