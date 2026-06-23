# Wave 1 · dataset  (NEW file + NEW tests)

- **Worktree:** `../spelunk-wt/dataset`  **Branch:** `wave1/dataset`
- **Create:** `spelunk/eval/dataset.py` **and** `tests/test_dataset.py` (write the tests **first**).

Read `_SETUP.md` first. Produce the frozen `questions.jsonl` (rows = `spelunk.eval.schemas.BirdQuestion`)
from the BIRD dev set.

## Functions (the contract you define)
- `load_bird_dev(bird_root: Path) -> list[BirdQuestion]`
  Parse BIRD `dev.json`; map `question_id, db_id, question, evidence, SQL→gold_sql, difficulty`.
  If a record lacks `difficulty`, default to `"moderate"` (and note it).
- `stratified_sample(qs, n=150, seed=0, dbs: list[str] | None = None) -> list[BirdQuestion]`
  Stratify by `difficulty` (roughly proportional); optionally restrict to `dbs`. **Deterministic** (seed).
- `write_jsonl(qs, path)` / `read_jsonl(path) -> list[BirdQuestion]`.

## Tests (no 33 GB download!)
Build a tiny synthetic `dev.json` in `tmp_path` (≈5–10 fake records spanning difficulties/dbs) and assert:
field mapping is correct; `stratified_sample` is deterministic, respects `n` and `dbs`, preserves strata;
`write_jsonl`/`read_jsonl` round-trips to valid `BirdQuestion`.

## Done when
`uv run pytest tests/test_dataset.py` is green. Commit to `wave1/dataset`.
