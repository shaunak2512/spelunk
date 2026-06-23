"""Acceptance tests for ``spelunk.eval.dataset``.

No 33 GB download: we build a tiny synthetic BIRD ``dev.json`` in ``tmp_path`` and
assert field mapping, deterministic stratified sampling, and JSONL round-trip.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from spelunk.eval.dataset import (
    load_bird_dev,
    read_jsonl,
    stratified_sample,
    write_jsonl,
)
from spelunk.eval.schemas import BirdQuestion


# --- synthetic BIRD dev.json ------------------------------------------------

def _bird_record(qid: int, db_id: str, difficulty: str | None, *, with_evidence: bool = True) -> dict:
    """Build one record using BIRD's real field names (``SQL``, not ``gold_sql``)."""
    rec = {
        "question_id": qid,
        "db_id": db_id,
        "question": f"Question number {qid}?",
        "SQL": f"SELECT {qid} FROM t",
    }
    if with_evidence:
        rec["evidence"] = f"hint for {qid}"
    if difficulty is not None:
        rec["difficulty"] = difficulty
    return rec


@pytest.fixture
def bird_root(tmp_path: Path) -> Path:
    """A fake BIRD root whose ``dev.json`` spans difficulties and dbs."""
    records = [
        _bird_record(0, "financial", "simple"),
        _bird_record(1, "financial", "simple"),
        _bird_record(2, "financial", "moderate"),
        _bird_record(3, "movies", "moderate"),
        _bird_record(4, "movies", "moderate"),
        _bird_record(5, "movies", "challenging"),
        _bird_record(6, "school", "challenging"),
        # No 'difficulty' key -> must default to "moderate".
        _bird_record(7, "school", None),
        # No 'evidence' key -> must become None.
        _bird_record(8, "school", "simple", with_evidence=False),
    ]
    (tmp_path / "dev.json").write_text(json.dumps(records), encoding="utf-8")
    return tmp_path


# --- load_bird_dev ----------------------------------------------------------

def test_load_bird_dev_field_mapping(bird_root: Path):
    qs = load_bird_dev(bird_root)
    assert len(qs) == 9
    assert all(isinstance(q, BirdQuestion) for q in qs)

    by_id = {q.question_id: q for q in qs}

    q0 = by_id[0]
    assert q0.db_id == "financial"
    assert q0.question == "Question number 0?"
    assert q0.evidence == "hint for 0"
    assert q0.gold_sql == "SELECT 0 FROM t"  # SQL -> gold_sql
    assert q0.difficulty == "simple"


def test_load_bird_dev_missing_difficulty_defaults_moderate(bird_root: Path):
    qs = load_bird_dev(bird_root)
    by_id = {q.question_id: q for q in qs}
    assert by_id[7].difficulty == "moderate"


def test_load_bird_dev_missing_evidence_is_none(bird_root: Path):
    qs = load_bird_dev(bird_root)
    by_id = {q.question_id: q for q in qs}
    assert by_id[8].evidence is None


def test_load_bird_dev_accepts_path_to_dev_json_directly(bird_root: Path):
    """Passing the dev.json file itself (not its parent dir) also works."""
    qs = load_bird_dev(bird_root / "dev.json")
    assert len(qs) == 9


# --- stratified_sample ------------------------------------------------------

def test_stratified_sample_respects_n(bird_root: Path):
    qs = load_bird_dev(bird_root)
    sample = stratified_sample(qs, n=4, seed=0)
    assert len(sample) == 4
    assert all(isinstance(q, BirdQuestion) for q in sample)


def test_stratified_sample_n_larger_than_pool_returns_all(bird_root: Path):
    qs = load_bird_dev(bird_root)
    sample = stratified_sample(qs, n=1000, seed=0)
    assert len(sample) == len(qs)
    assert {q.question_id for q in sample} == {q.question_id for q in qs}


def test_stratified_sample_is_deterministic(bird_root: Path):
    qs = load_bird_dev(bird_root)
    a = stratified_sample(qs, n=5, seed=0)
    b = stratified_sample(qs, n=5, seed=0)
    assert [q.question_id for q in a] == [q.question_id for q in b]


def test_stratified_sample_seed_changes_selection(bird_root: Path):
    qs = load_bird_dev(bird_root)
    a = stratified_sample(qs, n=5, seed=0)
    b = stratified_sample(qs, n=5, seed=1)
    # Same size, both valid; different seed should generally reorder/reselect.
    assert len(a) == len(b) == 5
    assert {q.difficulty for q in a} <= {"simple", "moderate", "challenging"}


def test_stratified_sample_preserves_strata_proportions(bird_root: Path):
    qs = load_bird_dev(bird_root)
    # Pool difficulty counts (after default): simple=3, moderate=4, challenging=2 -> total 9.
    pool = Counter(q.difficulty for q in qs)
    assert pool == {"simple": 3, "moderate": 4, "challenging": 2}

    sample = stratified_sample(qs, n=6, seed=0)
    sc = Counter(q.difficulty for q in sample)
    # Every stratum present in the pool should be represented; none over-drawn.
    assert set(sc) == set(pool)
    for diff, cnt in sc.items():
        assert cnt <= pool[diff]
    # Roughly proportional: moderate (largest stratum) should not be smallest.
    assert sc["moderate"] >= sc["challenging"]


def test_stratified_sample_dbs_filter(bird_root: Path):
    qs = load_bird_dev(bird_root)
    sample = stratified_sample(qs, n=100, seed=0, dbs=["movies"])
    assert {q.db_id for q in sample} == {"movies"}
    # movies has 3 records (qids 3,4,5).
    assert len(sample) == 3


def test_stratified_sample_dbs_multi(bird_root: Path):
    qs = load_bird_dev(bird_root)
    sample = stratified_sample(qs, n=100, seed=0, dbs=["financial", "school"])
    assert {q.db_id for q in sample} == {"financial", "school"}


def test_stratified_sample_no_mutation(bird_root: Path):
    qs = load_bird_dev(bird_root)
    before = [q.question_id for q in qs]
    _ = stratified_sample(qs, n=3, seed=0)
    after = [q.question_id for q in qs]
    assert before == after


# --- write_jsonl / read_jsonl ----------------------------------------------

def test_write_read_jsonl_roundtrip(bird_root: Path, tmp_path: Path):
    qs = load_bird_dev(bird_root)
    out = tmp_path / "questions.jsonl"
    write_jsonl(qs, out)

    # One JSON object per line.
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(qs)
    for line in lines:
        obj = json.loads(line)
        assert set(obj) >= {"question_id", "db_id", "question", "gold_sql", "difficulty"}

    back = read_jsonl(out)
    assert all(isinstance(q, BirdQuestion) for q in back)
    assert back == qs


def test_write_jsonl_accepts_str_path(bird_root: Path, tmp_path: Path):
    qs = load_bird_dev(bird_root)
    out = tmp_path / "questions.jsonl"
    write_jsonl(qs, str(out))
    assert read_jsonl(str(out)) == qs
