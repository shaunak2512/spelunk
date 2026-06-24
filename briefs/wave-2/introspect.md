# Wave 2a · introspect

- **Worktree:** `../spelunk-wt/introspect`  **Branch:** `wave2/introspect`
- **File:** `spelunk/core/introspect.py`  **Tests (already exist):** `tests/test_introspect.py`
- **Depends on:** `connection`, `guard` (done on `main`).

Read `briefs/ORCHESTRATION.md` first. Install: `sqlalchemy pydantic pytest`.

Implement via SQLAlchemy's `Inspector`. **Do NOT import `query.run_sql`** — keep this leaf independent; run your own SELECTs via `engine.connect()`.

- `list_objects(engine) -> list[TableInfo]`: `Inspector.get_table_names()` + `get_view_names()`; set `kind` to `"table"`/`"view"`.
- `describe(engine, table, profile=True) -> TableDescription`:
  - columns from `get_columns` (name/type/nullable/pk), `primary_key` from `get_pk_constraint`, `foreign_keys` from `get_foreign_keys` → `ForeignKey(column, ref_table, ref_column)`.
  - `sample_rows`: `SELECT * FROM <table> LIMIT 5` as list of dicts.
  - `profile=True`: per column `null_fraction` = `(COUNT(*) - COUNT(col)) / COUNT(*)`, `distinct_count` = `COUNT(DISTINCT col)`, a few `sample_values` (distinct). Plain aggregate SELECTs are fine (DuckDB `SUMMARIZE` optional, not required).
  - **Quote identifiers safely** (use SQLAlchemy constructs / proper quoting) so reserved words / odd names don't break.

## Done when
`uv run pytest tests/test_introspect.py` (or the venv equivalent) is green. Commit to `wave2/introspect`.
