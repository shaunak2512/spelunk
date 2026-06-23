# Wave 1 · guard

- **Worktree:** `../spelunk-wt/guard`  **Branch:** `wave1/guard`
- **File:** `spelunk/core/guard.py`  **Tests (already exist):** `tests/test_guard.py`

Read `_SETUP.md` first. Implement both functions using **sqlglot** (AST, never regex).

## `assert_read_only(sql, dialect="sqlite") -> None`
Parse with sqlglot (e.g. `sqlglot.parse(sql, read=dialect)`). Raise `UnsafeSQLError`
(from `spelunk.core.types`) when:
- input is not exactly **one** statement (reject multi-statement),
- the statement is not a `SELECT` — note a `WITH … SELECT` CTE renders as a `Select` with a `with` arg, so **allow** it,
- the tree contains any DML/DDL node: `Insert, Update, Delete, Merge, Drop, Create, Alter, TruncateTable, Grant` …
  (scan via `expr.find(exp.Insert, exp.Update, …)`).

**Fail closed:** any parse error → raise `UnsafeSQLError`.

## `enforce_limit(sql, dialect="sqlite", max_rows=1000) -> str`
Parse to a `Select`. No LIMIT → add `LIMIT max_rows`; LIMIT > `max_rows` → set to `max_rows`;
LIMIT ≤ `max_rows` → unchanged. Use sqlglot's `Select.limit()`, re-render with `.sql(dialect=dialect)`.

## Done when
`uv run pytest tests/test_guard.py` is green (read-only allowed; all writes/DDL/multi-statement rejected;
limit injected / clamped / preserved). Commit to `wave1/guard`.
