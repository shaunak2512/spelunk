# Orchestration Playbook — building Spelunk in parallel waves

Canonical process for implementing a wave of modules with parallel agents. **Any session (or a human)
can follow this from the repo alone.** `TASKS.md` is the live backlog; the design is in
`database-agent-harness-build-spec.md`.

## Model
Wave-based dependency DAG. Parallelise *within* a wave; integrate at a **barrier** between waves. One
agent owns one module file (+ its own test file). Each parallel agent works in its own **git worktree** so
writes can't collide. **Definition of done** per module: its tests are green, `pytest --co -q` still
collects, and the orchestrator independently re-runs the tests (reviewer step) before merge.

## Non-negotiable rules for module agents
1. Edit ONLY your module file (and its test file if new).
2. Never edit orchestrator-owned files: `pyproject.toml`, `configs/*.yaml`, any `__init__.py`. All expected
   deps are already in `pyproject.toml`; if you think you need a new one, **stop and flag it**.
3. Don't touch other modules. Import from `spelunk.core.types` / `spelunk.eval.schemas`; never change them.
4. **Minimal-deps fast path** — install only what your tests need (cheat sheet below), not a full `uv sync`.
5. Write tests first for new modules; make them green; keep collection clean.

## Orchestrator steps (per wave)
1. From `TASKS.md`, pick the wave's parallel modules. Mind sub-wave dependencies (below).
2. Create a worktree per module (run in repo root):
   `git worktree add -b waveN/<mod> ../spelunk-wt/<mod> main`
3. Spawn one `general-purpose` agent per worktree using the prompt template below. **Default the
   subagent model to Sonnet (`claude-sonnet-4-6`)** — wave modules are contract-/test-driven, so Sonnet
   is sufficient and much cheaper than Opus.
4. **Reviewer step** — for each worktree:
   `cd ../spelunk-wt/<mod>; .\.venv\Scripts\python.exe -m pytest tests\test_<mod>.py -q`
   `git diff --name-only main HEAD` (only the intended files)  ·  `git status --short` (clean)
5. **Barrier merge** — in repo root, per branch:
   `git merge --no-ff waveN/<mod> -m "Merge waveN/<mod> into main (WaveN barrier)" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"`
   then full-suite check in a main venv:
   `py -3 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -q <suite deps>; .\.venv\Scripts\python.exe -m pytest -q`
6. Check the wave off in `TASKS.md` (commit). Retire worktrees:
   `git worktree remove ../spelunk-wt/<mod>; git branch -d waveN/<mod>`

## Agent prompt template (fill the `<…>`)
> You are implementing ONE module of the "Spelunk" project in an isolated git worktree on Windows. Work
> strictly inside `C:\Users\shaun\repo\spelunk-wt\<mod>` (branch `waveN/<mod>`). Windows 11, PowerShell,
> Python 3.14 (`py -3`), git.
> 1. Read `briefs\ORCHESTRATION.md` (rules) and `briefs\wave-N\<mod>.md` (your task), plus the contract files it names.
> 2. Create a venv and install ONLY: `<minimal deps>`. (No full sync.)
>    `py -3 -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -q <minimal deps>`
> 3. Implement `<file>` per its contract; for a NEW module, write `tests\test_<mod>.py` FIRST.
> 4. Make your tests green; confirm `.\.venv\Scripts\python.exe -m pytest --co -q` collects cleanly.
> 5. Edit ONLY `<file>` (+ your test). Do NOT touch `__init__.py`/`pyproject.toml`/`configs`/other modules.
> 6. Commit to `waveN/<mod>`, message ending `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Don't merge/push.
> Report back: test summary, did `--co` collect, commit hash+subject, full file contents, edge cases.

## Minimal-deps cheat sheet
| Module | Install |
|---|---|
| core/guard | `sqlglot pydantic pytest` |
| core/connection | `sqlalchemy pydantic pytest` |
| core/introspect | `sqlalchemy pydantic pytest` |
| core/query | `sqlalchemy sqlglot pydantic pytest` |
| eval/dataset | `pydantic pytest` |
| eval/score | `pydantic pytest` |
| eval/report | `pandas matplotlib pydantic pytest` |
| agent/models | `pydantic pyyaml pytest` (lazy-import langchain) |
| agent/tools | `langchain-core sqlalchemy sqlglot pydantic pytest` |
| rag/schema_index | `numpy sqlalchemy pydantic pytest` |
| mcp/server | `fastmcp sqlalchemy sqlglot pydantic pytest` |

Full-suite (main) deps through Wave 1: `pydantic sqlglot sqlalchemy pandas matplotlib pyyaml pytest`
(add `langchain-core numpy` once Wave 2b lands).

## Sub-wave dependencies (IMPORTANT)
- **Wave 2a (parallel leaves):** `introspect`, `query` — depend only on done `connection`/`guard`.
- **Barrier**, then **Wave 2b (parallel):** `agent/tools`, `rag/schema_index`, optional `mcp/server` —
  all depend on `introspect`+`query` being merged to `main`. **Do NOT fan out 2b before 2a is merged.**

## Notes
- Each worktree gets its own `.venv` (gitignored). `py -3 -m venv` + pip is fine; uv (if present) hardlinks from cache.
- `eval/score` uses multiset row comparison (stricter than BIRD's `set()` dedupe) — decide parity when wiring the Wave 3 runner.
- Package `__init__` files intentionally do NOT re-export submodules (keeps `import spelunk.eval` from dragging in pandas/matplotlib). Import from submodules: `from spelunk.eval.dataset import ...`.
- The assistant won't spawn agents unless you ask — say "spawn the agents" / "use subagents".
- **Model:** default worker subagents to **Sonnet** (`claude-sonnet-4-6`) to save tokens. Run the orchestrator on Opus for sharper reviewer/merge judgment, or Sonnet for max savings — fine for simple/independent waves; prefer Opus once waves get interdependent.
- Current state: Wave 0 + Wave 1 merged to `main`. Remaining red tests = `introspect` + `query` (Wave 2a).
