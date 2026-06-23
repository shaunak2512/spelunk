# Wave 1 · score  (NEW file + NEW tests)

- **Worktree:** `../spelunk-wt/score`  **Branch:** `wave1/score`
- **Create:** `spelunk/eval/score.py` **and** `tests/test_score.py` (write the tests **first**).

Read `_SETUP.md` first. Implement BIRD-style **execution accuracy** — adapt BIRD's official comparator;
don't invent the semantics.

## Functions
- `run_sql_raw(db_path: str, sql: str) -> list[tuple]`
  Plain `sqlite3` execute + `fetchall`. This module scores trusted gold *and* predicted SQL, so it
  deliberately bypasses `spelunk.core` guards.
- `compare_result_sets(a, b) -> bool`
  BIRD semantics: equal as **multisets of rows**, order-insensitive (unless you choose to respect
  `ORDER BY` — match BIRD's choice and document it).
- `execution_accuracy(pred_sql, gold_sql, db_path) -> bool`
  Run both; a prediction that errors → `False` (never raise).

## Tests
Build a small SQLite db (reuse the `conftest.sample_db` pattern). Assert: identical query → `True`;
semantically-equal-but-differently-written → `True`; different result → `False`; malformed prediction →
`False` (no exception).

## Done when
`uv run pytest tests/test_score.py` is green. Commit to `wave1/score`.
