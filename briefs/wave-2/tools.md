# Wave 2b · tools  (NEW file + NEW tests)

- **Worktree:** `../spelunk-wt/tools`  **Branch:** `wave2/tools`
- **Create:** `spelunk/agent/tools.py` **and** `tests/test_tools.py` (write the tests **first**).
- **Depends on:** `introspect` + `query` **merged to `main`** (Wave 2a). Confirm they're implemented before starting.

Read `briefs/ORCHESTRATION.md` first. Install: `langchain-core sqlalchemy sqlglot pydantic pytest`.

Expose the core functions as LangChain tools the agent can call:
- `make_tools(engine, *, profile=True) -> list[BaseTool]` returning:
  - `list_tables()` — wraps `core.list_objects` (the "ls").
  - `describe_table(name)` — wraps `core.describe` (schema + sample).
  - `run_query(sql)` — wraps `core.run_sql` (the exploratory probe; read-only/guarded).
  - `submit_sql(sql)` — terminator: record/return the final SQL answer (Wave 3's graph wires actual loop termination).
- Use `langchain_core.tools` (`@tool` or `StructuredTool`). Tools must return strings/JSON the model can read (serialize `QueryResult`/`TableDescription`).

Tests (offline, NO LLM): build tools over the conftest `sample_db` engine and invoke each tool callable directly — assert `list_tables` finds `customers`/`orders`, `describe_table` returns columns + FK, `run_query` returns rows, `submit_sql` captures the SQL.

## Done when
`tests/test_tools.py` green + suite collects. Commit to `wave2/tools`.
