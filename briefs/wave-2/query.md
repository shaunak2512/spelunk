# Wave 2a · query

- **Worktree:** `../spelunk-wt/query`  **Branch:** `wave2/query`
- **File:** `spelunk/core/query.py`  **Tests (already exist):** `tests/test_query.py`
- **Depends on:** `connection`, `guard` (done on `main`).

Read `briefs/ORCHESTRATION.md` first. Install: `sqlalchemy sqlglot pydantic pytest`.

Implement `run_sql(engine, sql, *, max_rows=1000, timeout_s=30) -> QueryResult`. **Reuse `guard`; don't reimplement.**

1. `dialect = engine.dialect.name`
2. `guard.assert_read_only(sql, dialect)`  → lets `UnsafeSQLError` propagate on writes.
3. `sql2 = guard.enforce_limit(sql, dialect, max_rows)`
4. Execute `sql2` via `engine.connect()`; fetch rows; `columns` from `result.keys()`.
5. `truncated = (len(rows) == max_rows)`  — i.e. we hit the cap.
6. Return `QueryResult(columns, rows=[list(r) for r in rows], row_count=len(rows), truncated=..., elapsed_s=..., sql_executed=sql2)`.

Statement timeout: best-effort (SQLite has no portable per-statement timeout — document; Postgres could `SET statement_timeout`). Not exercised by tests.

## Done when
`uv run pytest tests/test_query.py` (or the venv equivalent) is green: SELECT returns rows; auto-LIMIT truncates; a write raises `UnsafeSQLError`. Commit to `wave2/query`.
