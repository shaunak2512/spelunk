# Wave 1 · connection

- **Worktree:** `../spelunk-wt/connection`  **Branch:** `wave1/connection`
- **File:** `spelunk/core/connection.py`  **Tests (already exist):** `tests/test_connection.py`

Read `_SETUP.md` first. Implement `connect(dsn, *, read_only=True) -> sqlalchemy.engine.Engine`.
(The stub guards the `Engine` import under `TYPE_CHECKING` just to keep the contract import-light — import it normally now.)

## Behaviour
- `create_engine(dsn)` with SQLAlchemy's default pooling.
- `read_only=True` must block writes:
  - **SQLite (the tested path):** rewrite the URL to read-only URI form so writes raise `OperationalError`,
    e.g. `sqlite:///file:<abs path>?mode=ro&uri=true`. Parse the incoming `sqlite:///<path>` and convert.
    With `read_only=False`, return a normal read-write engine.
  - **Other dialects (best-effort, not tested):** attach a connect/begin event setting the session
    read-only (e.g. Postgres `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`). Document; don't over-build.

## Done when
`uv run pytest tests/test_connection.py` is green: the engine connects, and an `INSERT` under
`read_only=True` raises. Commit to `wave1/connection`.
