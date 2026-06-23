# Wave 1 — shared setup & rules

You are implementing **one** module in an isolated git worktree. Other agents are implementing sibling
modules in parallel. Stay in your lane.

## Setup (run once, in your worktree)
- Preferred: `uv sync --extra dev`
- Fallback: `python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"`

## Rules (non-negotiable)
1. Edit **only** the file(s) named in your brief — your module, plus its test file if you create one.
2. Do **not** edit any of: `pyproject.toml`, `configs/*.yaml`, any `__init__.py`. These are
   **orchestrator-owned**; package exports are wired at the merge barrier. If you think you need a new
   dependency, **stop and flag it** — do not add it (all expected deps are already in `pyproject.toml`).
3. Do **not** touch other modules. Import from `spelunk.core.types` / `spelunk.eval.schemas`; never change them.
4. **Definition of done:** your acceptance tests pass (`uv run pytest tests/<your_test>.py`) **and** the
   whole suite still collects (`uv run pytest --co -q`). Then commit to your branch.

## Frozen contracts you build against
- Core types: `spelunk/core/types.py`
- Eval data schemas: `spelunk/eval/schemas.py`
- Config shapes: `configs/models.yaml`, `configs/rungs.yaml`
- For existing modules, your signature + contract docstring is already in the file. For new files, the
  contract is in your brief.

## When done
Commit to your branch with a clear message. The **orchestrator** merges all Wave 1 branches to `main`
and wires `__init__.py` exports at the barrier — do not merge to `main` yourself.
