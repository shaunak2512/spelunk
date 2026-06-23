# Build Spec — Database Agent Harness ("Spelunk")

*Working name: **Spelunk** — an agent that explores a database the way a coding agent explores a repo. Rename freely.*

> **One-line thesis (the README headline):**
> *"A lean, vendor-agnostic harness that lets a **cheap** model match an **expensive** one on real-schema SQL — measured on BIRD, not claimed."*

---

## 0. Scope, decisions, and non-goals

### Locked design decisions
| Decision | Choice | Why |
|---|---|---|
| Benchmark models | **Claude + OpenAI**, behind a vendor-agnostic loader | Tests the parity thesis *and* shows the harness is model-neutral. Agnosticism is ~free via `init_chat_model`. |
| Agent framework | **LangGraph** | Top CV strength to showcase; typed state + retries + tool-calling for free. |
| Agent behaviour | **Agentic exploration** | The agent browses schema and runs probe queries before answering — this *is* the differentiator. |
| Eval size | **Lean-credible** | ~150 BIRD questions, **3 ablation rungs**, cheap+frontier tiers. Convincing, low cost. |

### Non-goals (explicitly parked — do NOT build in v1)
Each is a real feature; none serves the benchmark, so all are deferred to keep v1 lean.
- **Materialization workspace** (results → DuckDB scratch + real `grep`/`awk`). BIRD scores a single SQL string; multi-pass scratch doesn't move the metric. *→ Extension.*
- **PII redaction** (Presidio). BIRD is public data. *→ Extension, only matters for the MCP path on real DBs.*
- **Cost caps / budgets / RBAC.** Not needed for a controlled benchmark. *→ Extension.*
- **Multi-dialect beyond SQLite.** BIRD ships SQLite; the eval needs nothing else. (MCP path adds Postgres only if pursued.)
- **Few-shot exemplar rung, semantic layer, web UI, write access, response caching-as-feature.** *→ Extensions (few-shot is the first one to add if you go "Fuller").*

### The build-vs-buy stance
The shared core is **assembled, not invented** (see §2). The differentiation budget goes entirely to the **eval result** (§3). Resist gold-plating the core.

---

## 1. Repository structure

```
spelunk/
├── pyproject.toml
├── .env                      # ANTHROPIC_API_KEY, OPENAI_API_KEY
├── README.md                 # headline + the ablation chart + honest caveats
├── configs/
│   ├── models.yaml           # model id, provider, $/Mtok in/out
│   └── rungs.yaml            # the 3 ablation configs
├── spelunk/
│   ├── core/                 # SHARED CORE — agent-agnostic, no LLM, no protocol
│   │   ├── connection.py     # SQLAlchemy engine, read-only enforced
│   │   ├── introspect.py     # list_objects(), describe()  (+ profiling)
│   │   ├── query.py          # run_sql()  (validate → LIMIT → execute)
│   │   ├── guard.py          # sqlglot: assert_read_only(), enforce_limit()
│   │   └── types.py          # TableInfo, TableDescription, QueryResult (pydantic)
│   ├── agent/                # EVAL/STANDALONE front-end (LangGraph)
│   │   ├── tools.py          # wrap core fns as LangChain tools
│   │   ├── graph.py          # the react-style loop + submit_sql terminator
│   │   ├── models.py         # init_chat_model(...) loader from models.yaml
│   │   └── rungs.py          # apply rung flags (schema_mode / rag / profile)
│   ├── rag/
│   │   └── schema_index.py   # embed + cosine retrieve top-k tables (numpy, no vector DB)
│   └── mcp/                  # OPTIONAL EXTRA — protocol front-end
│       └── server.py         # FastMCP: resources + run_query tool
├── eval/
│   ├── dataset.py            # load + stratified-sample BIRD → questions.jsonl
│   ├── runner.py             # (model × rung × question) → predictions + telemetry
│   ├── score.py              # execution accuracy (adapt BIRD comparator)
│   └── report.py             # results.csv + matplotlib charts
└── data/
    └── bird/                 # downloaded BIRD dev DBs + gold SQL (gitignored)
```

Three front-ends (`agent/`, `mcp/`) over **one** `core/`. The core never imports an LLM or a protocol.

---

## 2. Shared core (the library)

Pure Python, agent-agnostic. **Assembled from mature libraries** — your code is the thin glue and the policy choices.

| Core piece | Off-the-shelf lib | Your glue |
|---|---|---|
| Engine, pooling, dialects | **SQLAlchemy** | `connect(dsn, read_only=True)` |
| Schema introspection | **SQLAlchemy `Inspector`** | shape into `TableInfo` / `TableDescription` |
| Profiling (null %, cardinality, samples) | **DuckDB `SUMMARIZE`** or plain `SELECT` | only when `profile=True` |
| Read-only enforce + auto-`LIMIT` | **sqlglot** (parse AST, reject non-SELECT, inject LIMIT) | `guard.py` policy |
| Embeddings for schema-RAG | provider embeddings (`text-embedding-3-small`) or local | cosine in numpy |

### Public API (the only surface both front-ends call)
```python
# core/connection.py
def connect(dsn: str, *, read_only: bool = True) -> Engine: ...

# core/introspect.py
def list_objects(engine) -> list[TableInfo]:                 # tables/views = the "files"
def describe(engine, table: str, *, profile: bool = True) -> TableDescription:
    # columns, types, PK/FK; if profile: + sample rows, null %, distinct count

# core/query.py
def run_sql(engine, sql: str, *, max_rows: int = 1000, timeout_s: int = 30) -> QueryResult:
    # 1) guard.assert_read_only(sql)  2) guard.enforce_limit(sql, max_rows)  3) execute

# core/guard.py
def assert_read_only(sql: str, dialect: str) -> None:        # raise on DDL/DML/multi-statement
def enforce_limit(sql: str, dialect: str, max_rows: int) -> str:  # inject/clamp LIMIT
```

### Minimal governance (v1)
Only what a benchmark needs: **read-only** (sqlglot default-deny on anything but a single `SELECT`/CTE, *and* open the SQLite/DuckDB connection read-only as defence-in-depth), **auto-`LIMIT`**, **statement timeout**. Nothing else. (PII/caps/audit → extensions.)

---

## 3. Eval wrapper (the differentiator)

### 3.1 The agent (LangGraph)
A tool-calling ("ReAct") loop. Lean path: start from LangGraph's **`create_react_agent`** with custom tools; drop to a hand-wired `StateGraph` only if you need tighter control.

**Tools given to the model** (thin wrappers over `core/`):
- `list_tables()` → `list_objects`
- `describe_table(name)` → `describe`
- `run_query(sql)` → `run_sql` (a *probe*; returns rows so the agent can inspect data)
- `submit_sql(sql)` → terminates the loop and records the final answer

**State** (`pydantic`): `question`, `db_id`, `messages`, `final_sql`, `n_tool_calls`, `tokens`, `error`.
**Termination:** `submit_sql` called **or** `max_steps` (e.g. 12) reached. Cap probe rows hard (e.g. 50) so exploration can't blow context.

### 3.2 The ablation — 3 additive rungs
Each rung isolates one harness component's contribution. Configured in `rungs.yaml`, not forked code.

| Rung | Schema access | Profiling | RAG | Isolates |
|---|---|---|---|---|
| **R0 — Baseline** | Full schema DDL dumped into context | off | off | classic text-to-SQL (+ execute-and-retry) |
| **R1 — +Discovery-FS** | No dump; agent calls `list_tables`/`describe_table` to explore | **on** (sample rows + value stats) | off | does *exploring* > *dumping*? value-grounding |
| **R2 — +Schema-RAG** | R1 + retriever pre-selects top-k relevant tables to focus on | on | **on** | does retrieval help grounding on bigger schemas? |

> **Honesty caveat to put in the README:** BIRD dev schemas are *moderate*, so R1/R2's edge over R0 may be small — the harness pays off most as schema size grows. Either accept the modest delta as an honest finding, or include 1–2 of BIRD's largest-schema DBs to make the effect visible. Don't over-claim.

### 3.3 The model matrix (lean, cross-vendor, proves parity)
Vendor-agnostic via `init_chat_model`; models declared in `models.yaml`.

```
                 R0      R1      R2
cheap · Claude   ●       ●       ●      ← Haiku 4.5
cheap · OpenAI   ●       ●       ●      ← e.g. gpt-5-mini (confirm current cheap tier)
frontier ref     ●(bare only)           ← Claude Opus 4.8 / OpenAI frontier — the "expensive, no harness" bar
```
8 model-config cells × ~150 questions. **Headline chart:** cheap-model accuracy *climbing across R0→R1→R2*, with the frontier-bare scores drawn as horizontal reference lines. **Parity is proven when a cheap line crosses a frontier line.** Secondary chart: **$ per correct answer** — cheap+harness should win decisively on cost even at equal accuracy.

> Model IDs known: `anthropic:claude-haiku-4-5`, `anthropic:claude-opus-4-8` (Sonnet 4.6 = `claude-sonnet-4-6` if you want a cheaper "frontier" bar). **Confirm current OpenAI cheap/frontier IDs and per-token prices when you fill `models.yaml`.**

### 3.4 Dataset
- Download **BIRD dev** (ships SQLite DBs + gold SQL + column descriptions).
- `dataset.py`: **stratified sample ~150** questions across BIRD's `difficulty` labels (simple / moderate / challenging) and 3–5 databases. Freeze to `questions.jsonl` (reproducibility).

### 3.5 Scoring & telemetry
- **Execution Accuracy (EX):** run predicted vs gold SQL on the DB, compare result sets. **Adapt BIRD's official comparator** — don't hand-roll set-equality (handle ordering, NULLs, dup rows). VES/efficiency: deferred.
- **Per-run telemetry:** `predicted_sql`, `ex_correct`, `n_llm_calls`, `n_tool_calls`, `prompt_tokens`, `completion_tokens`, `usd_cost` (tokens × `models.yaml` price), `latency_s`, `error`.
- **Cache LLM responses by hash** of (model, prompt) so re-runs are free — this is a *dev-cost* measure, not a product feature.

### 3.6 Outputs
`results.csv` (one row per run) → `report.py` →
1. **Accuracy × rung** grouped bars per cheap model + frontier reference lines (the headline).
2. **$/correct-answer** by model×rung.
3. **Error taxonomy** (wrong table / bad join / value mismatch / syntax) — qualitative depth for the writeup.

---

## 4. MCP wrapper — OPTIONAL EXTRA

Build only after the eval lands. Reuses `core/` verbatim; **no model, no loop** (Claude Code is the agent).

- **Library:** **FastMCP** (Python) — reuses `core/` directly, no second language.
- **Surface (idiomatic MCP):**
  - **Resources** — discovery-FS done protocol-native: a root resource lists tables; `db://{table}` returns `describe()` output. (Tables-as-readable-resources = your filesystem metaphor.)
  - **Tool** — `run_query(sql)` → governed `run_sql`.
  - *(Optional)* **Prompt** — a short "how to explore this DB" template.
- **Transport:** stdio (local), so it drops into Claude Code via `.mcp.json`.
- **Deliberately minimal:** SQLite/DuckDB (+ Postgres only if you want one server dialect), read-only, no PII/caps/audit in v1.

```jsonc
// .mcp.json — how Claude Code connects
{ "mcpServers": { "spelunk": { "command": "uvx", "args": ["spelunk-mcp", "--dsn", "sqlite:///data/app.db"] } } }
```

---

## 5. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12, `pyproject.toml` (uv) |
| DB / dialects | SQLAlchemy; SQLite for eval; DuckDB for profiling |
| SQL safety | sqlglot (AST read-only + LIMIT) |
| Agent | LangGraph (`create_react_agent`) |
| Models | LangChain `init_chat_model` → Anthropic + OpenAI (config-driven) |
| RAG | provider embeddings + numpy cosine (no vector DB) |
| Eval scoring | adapted BIRD comparator |
| Charts | matplotlib / pandas |
| MCP (optional) | FastMCP, stdio |

---

## 6. Build phasing (~3 weeks, lean)

**Week 1 — Shared core + agent skeleton.** Assemble `core/` (connection, introspect, query, guard) over one BIRD SQLite DB. Wrap as LangGraph tools; get `create_react_agent` answering ~5 questions by hand. *Exit:* agent explores a DB and emits correct SQL interactively.

**Week 2 — Eval harness + the result.** `dataset.py` (freeze 150 Q) → `runner.py` (model×rung×question, with response cache) → `score.py` (BIRD comparator) → `report.py` (charts). Run the lean matrix. *Exit:* the headline accuracy chart + cost chart exist.

**Week 3 — Writeup (+ optional MCP).** README with the chart, the honest small-schema caveat, and the error taxonomy. *Then, only if time:* the FastMCP wrapper + a Claude Code screenshot. *Exit:* portfolio-ready repo.

---

## 7. Risks & mitigations
- **Result risk** (cheap may not reach parity): an honest *"the harness closes X of the gap; here's where cheap models still fail"* is still a strong, senior story. The error taxonomy carries it either way.
- **Cost** of the matrix: lean size + response cache + run cheap models first; reserve frontier calls for the bare reference cells.
- **Eval correctness**: reuse BIRD's comparator; never trust hand-rolled set-equality.
- **Small-schema dilution** of the harness effect: state it plainly, or add a large-schema DB to surface it.
- **Scope creep**: §0 non-goals are the guardrail. Every "wouldn't it be cool if…" goes to Extensions, not v1.

---

## 8. Portfolio framing
README leads with the **chart**, then one paragraph: *"NL-to-SQL is commodity; what's scarce is knowing whether your harness actually helps. I built a vendor-agnostic exploration harness on commodity libraries and **measured** its lift — a cheap model + harness reaches frontier-model accuracy at a fraction of the cost."* Then the honest caveat. That sequence — result first, composed-not-reinvented core, candid limitations — is the senior signal that separates this from a LangChain demo.

---

## 9. Deferred extensions (the backlog, ranked)
1. **Few-shot exemplar rung** (R3) — first add if going "Fuller".
2. **Materialization workspace** (DuckDB scratch + real shell) — enables true multi-pass analysis beyond single-SQL benchmarks.
3. **Larger / harder benchmarks** — Spider 2.0, big enterprise schemas (where the harness should shine).
4. **Governance for real DBs** — Presidio PII redaction, cost caps, audit log (matters once the MCP path points at production).
5. **More vendors** — Gemini, local Ollama (one `models.yaml` line each).
6. **FK-as-symlink discovery, `git diff` for query results, notebook export** — from the original idea analysis.
