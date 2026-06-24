# Spelunk — Build Backlog (wave-ordered)

**Coordination model:** wave-based dependency DAG, *not* autonomous ticket pickup. Parallelise *within*
a wave; integrate at the barrier *between* waves. One agent owns one module/file; give each parallel
agent its own **git worktree**.

**Definition of done (every task):** its acceptance tests are **green** *and* an independent reviewer
agent approves. If a module has no tests yet, the agent's **first** action is to write them against the
frozen contract (signatures in the module + types in `spelunk/core/types.py` + `spelunk/eval/schemas.py`).

**Orchestrator-owned — never edit inside a parallel agent:** `pyproject.toml`, `configs/*.yaml`, every
`__init__.py` export. Touch these only at a barrier.

---

## Wave 0 — Foundations  ✅ (this scaffold)
- [x] `core/types.py`, frozen `core` signatures, `eval/schemas.py`, `configs/*`, test scaffold

## Wave 1 — Core leaves  ✅ (merged to main, 0 conflicts)
- [x] `core/guard.py` — `assert_read_only`, `enforce_limit` (sqlglot AST). → `tests/test_guard.py`
- [x] `core/connection.py` — `connect` + read-only enforcement. → `tests/test_connection.py`
- [x] `eval/dataset.py` — download + stratified-sample BIRD → `questions.jsonl` (`BirdQuestion`)
- [x] `eval/score.py` — execution accuracy (adapt BIRD comparator) over `(pred_sql, gold_sql, db)`
- [x] `eval/report.py` — `results.csv` (`RunResult`) → headline + cost charts (matplotlib)
- [x] `agent/models.py` — `load_model(name)` via `init_chat_model` from `configs/models.yaml`

## Wave 2 — Composition + side front-ends
Briefs in `briefs/wave-2/`; process in `briefs/ORCHESTRATION.md`. Has an internal sub-wave split:

**Wave 2a (parallel leaves; depend on done `connection`/`guard`):**  ✅ (merged to main, 0 conflicts)
- [x] `core/introspect.py` — `list_objects`, `describe` (+profile). → `tests/test_introspect.py`
- [x] `core/query.py` — `run_sql` (guarded pipeline). → `tests/test_query.py`

**Barrier**, then **Wave 2b (parallel; depend on 2a merged to `main`):**  ✅ (merged to main, 0 conflicts)
- [x] `agent/tools.py` — wrap core fns as LangChain tools + `submit_sql` terminator
- [x] `rag/schema_index.py` — embed schema + numpy-cosine retrieve top-k tables
- [x] `mcp/server.py` — FastMCP resources + `run_query` tool (**OPTIONAL extra**)

## Wave 3 — Integration & run (mostly serial)
Code modules built directly on Opus (serial, interdependent) + run scaffolding staged. The
**paid run** (smoke + matrix) and README are deferred — they need a `.env` with API keys.
- [x] `agent/rungs.py` — typed `RungConfig` loader for the R0/R1/R2 ablation. → `tests/test_rungs.py`
- [x] `agent/graph.py` — hand-wired ReAct loop (step/probe caps, `submit_sql` terminator, telemetry). → `tests/test_graph.py`
- [x] `eval/runner.py` — `(model × rung × question)` matrix + frontier→R0 filter + response cache + telemetry → `results.csv`. → `tests/test_runner.py`
- [x] Run scaffolding staged: BIRD dev downloaded (gitignored), `data/questions.jsonl` frozen (150 Q, 5 DBs), `scripts/freeze_bird_questions.py`.
- [ ] **(deferred — needs API keys)** end-to-end smoke (~5 questions) → then the lean matrix run (~150 Q)
- [ ] **(deferred — needs run results)** `README` writeup: headline chart + honest small-schema caveat + error taxonomy

---

### Open action item
~~Confirm current OpenAI cheap/frontier model IDs + per-token prices in `configs/models.yaml`.~~
✅ Resolved 2026-06-24: `configs/models.yaml` now carries confirmed IDs/prices for both vendors —
Claude (`claude-haiku-4-5` $1/$5, `claude-opus-4-8` $5/$25) and OpenAI (`gpt-5.4-mini` $0.75/$4.50,
`gpt-5.5` $5/$30). Re-confirm prices before a real billed run.
