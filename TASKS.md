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

## Wave 2 — Composition + side front-ends (parallel; depend on Wave 1)
- [ ] `core/introspect.py` — `list_objects`, `describe` (+profile). → `tests/test_introspect.py`
- [ ] `core/query.py` — `run_sql` (guarded pipeline). → `tests/test_query.py`
- [ ] `agent/tools.py` — wrap core fns as LangChain tools + `submit_sql` terminator
- [ ] `rag/schema_index.py` — embed schema + numpy-cosine retrieve top-k tables
- [ ] `mcp/server.py` — FastMCP resources + `run_query` tool (**OPTIONAL extra**)

## Wave 3 — Integration & run (mostly serial)
- [ ] `agent/rungs.py` — apply R0 / R1 / R2 flags from `configs/rungs.yaml`
- [ ] `agent/graph.py` — `create_react_agent` loop, max-steps cap, probe-row cap
- [ ] `eval/runner.py` — `(model × rung × question)` matrix + telemetry + response cache
- [ ] end-to-end smoke (~5 questions) → then the lean matrix run (~150 Q)
- [ ] `README` writeup: headline chart + honest small-schema caveat + error taxonomy

---

### Open action item
Confirm current **OpenAI** cheap/frontier model IDs + per-token prices in `configs/models.yaml`
(placeholders + TODOs are in there now). Claude IDs are set.
