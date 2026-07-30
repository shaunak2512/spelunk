# Design: join-key discovery, durable notebook mode, API sources

Status: **brainstorm / not scheduled** (2026-07-25). Nothing here is implemented. This doc
captures the design space in enough detail to pick any feature up cold.

Motivating observation (see also the 3-round agent experiment notes): spelunk's differentiation
today is the provenance layer (lineage/replay/isolation), not insight quality. Both features
below deepen that moat and cut agent round-trips, rather than trying to make the agent smarter
via retrieval. A related idea that was considered and **rejected**: semantic/RAG search over
past queries within a session — the corpus (tens of queries) fits in one `catalog`/`lineage`
response, per-process workspaces reset every run so there is usually nothing to search, and
embeddings would break the no-LLM-in-server property. Search only becomes worth having *after*
the notebook exists (see §2.6).

---

## 1. Cross-source join-key discovery (`relate`)

### 1.1 Problem and scale

Agents burn most of their discovery turns figuring out **which columns join across sources**:
describe A, describe B, sample both, guess, retry on a type mismatch. Cross-source joins are
spelunk's core pitch, so a server-side answer is genuinely differentiating — plain
Claude-Code-plus-DuckDB rediscovers join keys every session.

The academic framing of this problem (JOSIE, LSH Ensemble, LakeBench — references in §3) is
"find joinable columns among millions of data-lake tables", which forces MinHash/LSH index
structures. **Spelunk is not that.** A session has tens of tables and hundreds of columns, so no
LSH index is needed — a staged **cheap→expensive funnel over column pairs** works, where each
stage kills most candidates before the next spends I/O. The constraint that *does* bite is
spelunk's own: sources can be larger than RAM and remote (Postgres over the wire, Parquet on
S3), so "just `DISTINCT` every column" is not viable. Every stage below has bounded I/O.

### 1.2 The funnel

**Stage 0 — free metadata (zero data scanned).**

- Enumerate columns of every queryable object. Machinery exists: `_objects_for_source`,
  `_columns_of`, `describe` (core/duck.py). Include **flow results**, not just sources —
  "joins to the thing I built two steps ago" is a real case.
- **Type-class pruning:** bucket types into families (integer-ish, string, date/time, float);
  only compare compatible families. Keep an explicit int↔varchar bridge (IDs stored as strings
  on one side is the most common real-world join), surfaced as a `needs_cast` flag in the
  output rather than silently dropped.
- **Name similarity in SQL, for free:** DuckDB ships `jaro_winkler_similarity`, `levenshtein`,
  and `jaccard` as built-in string functions — name scoring is a self-join over a small
  column-catalog temp table, no Python string libs. Normalise first: lowercase, strip
  `_id`/`_key`/`_fk` suffixes, singular/plural stem, so `customer_id` ↔ `customers.id` scores
  high.
- **Declared keys first:** `describe()` already surfaces PKs for attached DBs. The sources' own
  FK metadata is reachable through the attachment — Postgres via the postgres extension's
  `postgres_query()` passthrough reading `pg_constraint`; SQLite via `pragma_foreign_key_list`.
  Declared intra-source FKs cost nothing, rank above every inferred edge, and seed naming
  conventions ("this schema uses `<table>_id`") that improve cross-source inference.

**Stage 1 — one cheap scan per table.**

Exactly like `profile()` already does, compute per-column in a *single* aggregate pass:
`approx_count_distinct` (HLL), null rate, min/max. Two strong prunes fall out:

- **Key-ness:** uniqueness ratio ≈ 1 → candidate PK side. A 3-valued column is never a join key.
- **Range disjointness:** numeric/date min/max windows that don't overlap → containment is 0,
  kill the pair without comparing values. For Parquet sources this stage can be **scan-free**:
  `parquet_metadata()` reads min/max/null stats from footers.

**Stage 2 — value overlap, survivors only.** Two mechanisms, in adoption order:

- **Sampled containment probe (v1).** Take N distinct values from the smaller/more-unique side
  (`USING SAMPLE`, N ≈ 200–1000), check what fraction exists in the other column. Cost-control
  detail that matters: against a remote attached DB, express the probe as
  `WHERE col IN (<literal list>)` — filter pushdown works for a literal IN-list, whereas a
  semi-join against a workspace temp table forces a full remote scan. **Containment, not
  Jaccard**, is the right statistic: an FK is *contained in* its PK, and Jaccard punishes the
  size asymmetry between the two sides.
- **KMV / bottom-k sketches (v2, cacheable).** Per column keep the k smallest values of
  `hash(col)` (k ≈ 256). The DuckDB trick that makes this cheap: `UNPIVOT` the table to
  `(column_name, value)`, hash, `row_number() OVER (PARTITION BY column_name ORDER BY h)`,
  keep rank ≤ k — **every column's sketch in one table scan**. Sketches are tiny; store them in
  `_spelunk_meta.column_sketches` keyed by (object, column, snapshot fingerprint). All pairwise
  containment/Jaccard estimates then run sketch-vs-sketch over the meta table — zero further
  source I/O, durable for the session, reusable across calls. (This is the small-N version of
  MIT Aurum's column-profile knowledge graph.)

**Stage 3 — evidence on the top-k pairs.**

For the finalists, run an exact bounded check and report **evidence, not just a score**:
containment in both directions, cardinality shape (1:1 / 1:N / N:M via a capped GROUP BY), null
rates, `needs_cast` — plus a **paste-ready JOIN snippet** with correctly quoted qualified names
(`"pg"."orders" o JOIN "sales" s ON o.customer_id = s.cust_id`). The consumer is an agent:
give it the *why* so it can reject a false positive that a bare score would smuggle through.

### 1.3 API surface

A dedicated **read-only tool** — not `db://tables` enrichment, because discovery costs real I/O
and should be an explicit, parameterised call. Cache results in `_spelunk_meta` so `db://{table}`
can surface known candidates cheaply afterwards.

```
relate(target?: "pg.orders" | "pg.orders.customer_id",   # omit → survey everything
       candidates?: [...], sample_rows?, min_score?, limit?)
→ { pairs: [ { left, right, score,
               evidence: { name_sim, containment_lr, containment_rl,
                           uniqueness, cardinality: "1:N", needs_cast },
               join_sql } ],
    skipped: [ { object, reason } ] }
```

`skipped` is required output (no-silent-caps): report what was pruned and why ("remote table X:
stats-only, no value probe"). Everything is SELECT-only → composes with the existing sqlglot
guard. Scoring = weighted blend of name similarity, type compatibility, containment,
uniqueness; weights are constants until real usage says otherwise.

### 1.4 Known hard cases (design for explainability, not perfection)

- **Everything is called `id`:** dense small-integer surrogate keys overlap numerically with
  each other and with zip codes, quantities, years. Penalise dense-integer domains with low max
  values; weight name+containment jointly, never either alone.
- **Composite keys:** pairwise matching can't see `(order_id, line_no)`. v2: when several
  columns of the same table pair each show moderate containment (0.3–0.8), try the
  concatenated-pair sketch.
- **Semantic false positives** (`state` joins `status` at 40% by accident): statistically
  unavoidable; the evidence block plus agent judgment is the mitigation. `relate` ranks
  *hypotheses for an agent* — it never auto-joins.

### 1.5 Phasing

- **v0:** Stage 0 only — name/type/declared-PK heuristics, zero scans. ~A day of work; already
  beats Power BI autodetect (name/type only).
- **v1:** + Stage 1 stats pass + sampled containment + evidence block + join snippet.
- **v2:** cached KMV sketches in `_spelunk_meta`, composite keys, `parquet_metadata` shortcut,
  notebook cross-check (§2.8).

---

## 2. Durable notebook mode

### 2.1 The reframe that makes it tractable

The obvious design — "persist the workspace across sessions" — is what `--shared-workspace`
already does badly: single-writer contention, second server falls back to ephemeral. The
insight: **persist the *recipes*, not the *data*.** A lineage row (SQL + deps + description +
sources) is a few KB and fully rebuilds its result via the `replay` machinery. The notebook is
therefore a tiny, low-write-rate metadata store shareable across processes **without touching
per-process workspace isolation**. Workspaces stay isolated data; the notebook is shared
knowledge.

### 2.2 What an entry is

`publish` captures a result's **whole upstream closure from `lineage()`** — the pipeline, not a
single query. `publish("churn_by_cohort")` snapshots every recorded step that built it, in
dependency `order`, plus:

- per-step descriptions (already stored in lineage rows);
- **source fingerprints** — per source leaf: name, kind, schema hash (column names+types), and
  a cheap size signal (row count / file mtime). This is what makes recall trustworthy (§2.5);
- provenance/usage stats: created_at, times restored, last validated, last-known row counts.

**Curation model:** automatic capture of everything reproduces the junk-table problem (`tmp`,
`tmp2`) in permanent storage. Explicit `publish` matches notebook semantics and keeps the corpus
high-signal. Session lineage remains the automatic working log (true today); `publish`
promotes; a `prune` counterpart deletes.

### 2.3 Storage — the real decision

| Option | Multi-process writes | Queryable in-engine | Git-shareable | Verdict |
|---|---|---|---|---|
| DuckDB file, ATTACHed | ✗ one writer; RO/RW mix across processes disallowed | native | poor (binary) | fights DuckDB's concurrency model |
| **SQLite file (WAL)** | ✓ battle-tested, stdlib `sqlite3` | ✓ spelunk already attaches SQLite read-only | poor (binary) | **winner for the live store** |
| **JSONL append-log** | ✓-ish (atomic appends; supersede records for edits) | ✓ `read_json_auto` | **excellent** | **winner for the shared/committed notebook** |
| DuckLake catalog | ✓ | native | poor | overkill |

The two winners are compatible, and each targets a different ambition:

- **Live store = SQLite** at `<session-dir>/notebook.sqlite`. Writes go through stdlib
  `sqlite3` (WAL mode, `busy_timeout`) — one row per publish, contention is a non-issue. The
  file is **attached read-only into every session** as a source-like catalog
  (`"_notebook"."entries"`), so the notebook is *queryable with plain SQL by the agent* — the
  same move as the tool-log JSONL being queryable via `read_json_auto`. Zero new query
  machinery.
- **Interchange = JSONL** via `notebook export` (or an auto-mirror). A JSONL notebook checked
  into the repo is a *team-shared, code-reviewed query library*: readable diffs, per-entry
  merge conflicts. If team sharing becomes the goal, JSONL is the product and SQLite is the
  local cache of it.

Scoping falls out naturally: the notebook lives under `--session-dir`, which is per-project —
knowledge is project-scoped. Note the workspace GC sweep must ignore `notebook.*` (it only
touches per-process subdirs today, so this holds; keep it true).

### 2.4 API surface

- `publish(name, flow?, note?)` — snapshot the lineage closure into the notebook.
- `recall(text?, source?, limit?)` — search entries; each hit returns recipe, description,
  sources, usage stats, and a **freshness verdict** (§2.5).
- `restore(entry_id, into_flow?, dry_run?)` — materialise a recipe into the current session.
  Implementation-wise this is `replay` pointed at notebook rows instead of
  `_spelunk_meta.lineage`; `dry_run` mirrors replay's plan mode.
- **`db://notebook` resource** — entry count + titles relevant to the currently-attached
  sources, so agents *discover* the notebook without being told. Discoverability is otherwise
  the feature's biggest failure mode.

### 2.5 Freshness — what separates this from grepping shell history

On `recall`/`restore`, diff each entry's source fingerprints against the live session:

- **fresh** — schema hash matches → recipe should run as-is;
- **drifted** — source present, schema changed → list the changed columns;
- **orphaned** — references a source not attached → name it (the agent can `add_source` when
  enabled, or tell the user).

A retrieval result that says "this still runs against what you have attached" is categorically
more useful than raw text. Post-restore, compare step row counts to the recorded ones and
report drift — this is also the natural hook for a future `diff` tool and for assertions.

### 2.6 Search

At notebook scale (hundreds of entries), substring/`LIKE` + returning full entries is enough
for v1. If it outgrows that, the DuckDB **FTS extension** gives BM25 over
name+description+SQL. Its index doesn't auto-update, but the write rate is one row per publish,
so rebuild-on-write (`overwrite := 1`) is fine (newer FTS also has trigger-based incremental
indexes). Embeddings (VSS extension) remain out: HNSW persistence is still experimental
(corruption risk on unclean shutdown), and an embedding model breaks the no-LLM-in-server
property. Only revisit as an optional extra after FTS demonstrably fails.

### 2.7 Risks to design around

- **Secrets.** Published SQL can embed sensitive literals; locators can embed DSN credentials.
  Store source *names* + fingerprints, never raw locators with credentials — non-negotiable the
  moment a JSONL notebook can be committed to git.
- **Machine-specific paths** — same caveat `.mcp.json` carries; fingerprints + `orphaned`
  verdicts turn silent breakage into an actionable message.
- **Write conflicts on a name** — entries are immutable + versioned (supersede, not overwrite);
  two agents publishing `churn_model` never destroy each other's work; `recall` shows latest,
  history available.
- **Isolation story** — state it in docs: the notebook shares *recipes*; data isolation and the
  process-per-agent model are untouched.

### 2.8 Phasing, and how the two features compound

- **v0:** `publish` writes JSONL; auto-attach it read-only. Agent queries it with plain SQL —
  nearly zero new machinery.
- **v1:** SQLite live store, `recall`/`restore` tools, fingerprints + freshness,
  `db://notebook`.
- **v2:** FTS search, usage/validation stats, committed team notebook.

Compounding: `relate`'s discovered join keys and cached sketches are exactly the
expensive-to-recompute knowledge the notebook should persist, and a recalled pipeline
implicitly documents *how these sources were joined last time* — the single most valuable thing
a returning agent can learn. Natural v2: `relate` consults the notebook first and reports
"this pair is joined in 4 published recipes" as its strongest evidence tier.

---

## 3. API sources — four layers

How spelunk could read data from HTTP APIs, ordered by machinery required. The recommendation:
ship Layer 0 now (docs + secret plumbing), design Layer 2 as the real feature (it compounds
with lineage/replay/notebook), document Layer 3 as a recipe, and skip the middle path.

### 3.1 Layer 0 — simple JSON APIs already work (zero code)

`sources.py` was built for this without saying so: the format-override prefix exists for "an
extensionless API URL", and `https://` locators route through httpfs. Today:

```
--source events=json:https://api.example.com/v1/events
```

creates a view over `read_json_auto('https://...')` — DuckDB fetches the endpoint and parses
the JSON response into a table; nested structs/lists unnest fine in SQL.

**Auth works with no new source machinery** because DuckDB has HTTP secrets:
`CREATE SECRET (TYPE http, BEARER_TOKEN '...')` or
`EXTRA_HTTP_HEADERS MAP {'Authorization': ...}` applies to subsequent httpfs requests. The gap
is that spelunk offers no way to *run* that `CREATE SECRET` — the sqlglot guard blocks it from
`query`, correctly. So the cheapest real change is **config plumbing, not a new source kind**:
a `--http-secret` flag (or env-var-driven setup in `_remote_setup`) issuing the `CREATE SECRET`
at open, keeping tokens out of the spec string (specs get logged and echoed; secrets must not).

Layer 0 limits: no pagination (one URL = one fetch; `read_json_auto(['u1','u2'])` needs pages
known up front), GET-only (no POST/GraphQL bodies), no retries/rate-limit handling, and a view
**re-fetches the endpoint on every query** — slow, and rude to rate-limited APIs.

### 3.2 Layer 1 — the agent is the ETL (zero code, already true)

The agent driving spelunk usually has its own HTTP access: fetch the API, write JSON/CSV to
disk, `add_source` it (needs `--allow-add-source`). For one-off enrichment this is the right
division of labor — spelunk is the SQL engine, not a connector library. Weakness: provenance.
Lineage records the *file* as the source leaf and knows nothing about the fetch, so `replay`
rebuilds from a stale snapshot with no way to refresh or even notice staleness.

### 3.3 Layer 2 — an `api:` source kind: snapshot-on-attach (the one to build)

> **Status: implemented** (feat/api-source-snapshot) — `spelunk/core/apifetch.py` + the `api`
> kind in `sources.py`. Five pagination styles (page/offset/cursor/keyset/link); all but
> keyset live-verified against public APIs incl. authenticated TMDB (`tests/live_api_check.py`);
> keyset (Stripe-style seek) is mock-tested — no public no-auth keyset API to verify against.
> Refresh is re-attach (remove_source + add_source); a dedicated `refresh_source` tool remains
> future work.

Materialize-by-default, applied to the network boundary: an API source is **fetched into a
local snapshot at attach time, then registered as a view over the snapshot**.

- **Spec grammar stays consistent:** `gh=api:https://api.github.com/repos/x/y/issues`, with
  optional params covering the three pagination styles that handle ~90% of REST APIs
  (page-number, offset/limit, cursor-follow), a JSON-path to the records array, and an auth
  env-var *name* (never the token itself). Fetching is stdlib `urllib` (consistent with the
  no-SQLAlchemy ethos) with retries + rate-limit backoff, writing NDJSON/Parquet under the
  workspace dir.
- **Fixes the provenance gap of Layers 0/1:** the source fingerprint becomes
  `{url, params, fetched_at, row_count}`, recorded like any source leaf — lineage shows *when*
  data was pulled, and the notebook's freshness verdicts (§2.5) extend naturally to
  "snapshot is 6 days old".
- **Refresh is explicit** — re-attach (`remove_source` then `add_source`), never implicit; a
  dedicated `refresh_source(name)` tool remains future work. This resolves
  the reproducibility tension cleanly: `query` and `replay` run against a *pinned* snapshot
  (deterministic, no rate-limit surprises mid-pipeline); going stale is a visible, deliberate
  choice. Same "recipes vs data" split as §2.1.
- **Security:** an `api:` kind under `--allow-add-source` means the agent can make the server
  issue GET requests anywhere it can reach — the same SSRF-shaped trust boundary that flag
  already documents, but restate it there when this ships.

Considered and rejected middle path: the community `http_client` extension (`http_get`/
`http_post` as SQL functions). It drags in community-extension trust questions and puts
fetching *inside* queries — exactly the re-fetch-per-query behaviour the snapshot design
avoids.

### 3.4 OpenAPI endpoint catalogs (implemented)

> **Status: implemented** — `spelunk/core/openapi.py`. `openapi:<url-or-path>` materializes an
> OpenAPI 3.x JSON spec as a queryable catalog: one row per (path, method) with params, auth
> shape (securitySchemes mapped onto `auth_env=`/`header=`/`param=`, incl. the
> apiKey-named-Authorization bearer quirk), heuristic pagination/records hints, and a
> paste-ready `suggested_spec` `api:` string for GETs (`<SET_ME>` marks the credential env
> var). Guidance-as-data: the agent finds endpoints with SQL and feeds `suggested_spec` to
> `add_source`. Verified end-to-end against TMDB's 148-path spec (catalog → suggested_spec →
> live fetch). YAML and Swagger 2.0 are rejected with conversion pointers; spec discovery
> (probing /openapi.json) remains future work.

### 3.5 Layer 3 — generic connectors: don't build

Manifest-driven API configs (auth flows, incremental sync, schema evolution) is the
Airbyte/Singer/dlt product — a swamp. The right move is a documented recipe: **dlt** already
loads REST APIs into DuckDB natively, and spelunk attaches the resulting `.duckdb` (or Parquet)
as a source. One paragraph of docs buys the whole connector ecosystem.

### 3.6 Entity fan-out — `fetch(rows_from=...)` (implemented)

> **Status: implemented** (feat/api-source-snapshot) — but *not* as the `api:` source option
> this section originally designed. The shipped contract is connection-scoped:
> `fetch(source, path, name, rows_from=<[flow.]result>)` on an attached `openapi:` connection.
> The rest of this section is kept as the record of why it exists; where the design and the
> shipped surface differ, the deltas below are authoritative.

**Motivating evidence.** A/B eval on the TMDB API (2026-07-25): a spelunk-only agent vs a
curl+Python agent, same brief. Quality was comparable, but the script agent's insight was
*richer* (genre ROI economics) for one structural reason: budget/revenue live only in the
per-movie `/movie/{id}` **detail endpoint**, and the script agent fan-out-fetched 240 of them
in a threaded loop. Spelunk had no primitive for per-entity detail fetches — attaching 240
`api:` sources is absurd — so the spelunk agent's analysis was silently *shaped by what list
endpoints expose*. This was the highest-value `api:` follow-up: list→detail is the canonical
two-step of nearly every REST API (movies→credits, repos→contributors, orders→line items).

**What shipped: URL templates bound to a result's columns, on the `fetch` tool.**

```python
add_source("tmdb=openapi:https://developer.themoviedb.org/openapi/... auth_env=TMDB_TOKEN")
query(sql="SELECT id AS movie_id FROM top_rated ORDER BY vote_count DESC LIMIT 200",
      name="ids", description="The 200 most-voted chart movies to fetch details for")
fetch(source="tmdb", path="/3/movie/{movie_id}", rows_from="ids", name="details")
```

- **`rows_from` is a `fetch` argument, not an `api:` source option.** The API is attached once
  as an `openapi:` connection; every endpoint of it — list, detail, fan-out — is a `fetch`
  call against that one source. A fan-out produces a flow-scoped **result**, not a source, so
  it is droppable, appears in `lineage`, and never multiplies the source list.
- Each `{placeholder}` in `path` binds to the column of that name in the referenced result.
  One URL per *distinct* row of the bound columns (nulls dropped), ordered deterministically
  (ORDER BY the columns) so the snapshot is reproducible. The prep `query` IS the fan-out
  spec — selecting/aliasing/limiting rows is plain SQL, which composes with everything
  (filter to top-N, anti-join against already-fetched, etc.).
- Multi-placeholder templates fall out for free: `path="/repos/{owner}/{repo}"` binds two
  columns. The single-id case is the 1-column special case.
- **Continuity with the `openapi:` catalog:** the catalog row for `/3/movie/{movie_id}`
  already carries the `{movie_id}` template — the agent aliases a column to the placeholder
  name and passes `rows_from=`. Catalog → prep query → fan-out is a 3-call pipeline.

**Fetch semantics (as shipped).**

- Each response contributes its records (same `records=`/auto-detect per response; a detail
  endpoint's single object → one row), each row stamped `_key_<placeholder>` so joining back
  to the prep result is trivial even when the response omits the id. `paginate` must be
  `none` with `rows_from` (error otherwise) — the two axes (many-URLs vs many-pages) do not
  multiply.
- **Caps & courtesy:** `max_urls` — exceeding it is an ERROR telling the agent to LIMIT the
  prep query, never a silent truncation. A thread pool shares one `HostLimiter`, so a 429
  backs off every worker together (honouring Retry-After).
- **Partial failure policy:** a per-entity **404 is data, not an error** (deleted entity) —
  skipped and reported. 401/403 aborts the whole fetch (credentials are wrong for
  everything). Other failures: retry per URL, then abort — a half-fetched detail table is a
  footgun for aggregate queries.

**Provenance — v2 shipped, not v1.** The design hedged that the fan-out would be a leaf; it
isn't. A fetch result gets a `kind='fetch'` lineage node whose deps are passed explicitly
(there is no SQL to parse them out of), so `top_rated → ids → details → roi` renders
end-to-end. `replay` **preserves** fetch results rather than re-fetching them: they are pinned
inputs, like a source, and re-issuing N requests inside a deterministic rebuild would import
network latency, rate limits, and a changed upstream. They are reported under `preserved`
(and copied when rebuilding `into` a fresh flow). Refresh = `fetch` again.

**Safety.** Template values are percent-encoded as single path segments at substitution (a
value containing `/`, `?`, or `#` cannot redirect the request), and `fetch` is confined to its
connection's host + base path — absolute URLs, `..`, and traversal through a bound placeholder
are all refused. Same `--allow-add-source` trust boundary as `add_source`, amplified by N —
another reason `max_urls` is a hard error, not a soft cap.

**Non-goals (still):** pagination inside each detail fetch; POST bodies; recursive fan-out
(details-of-details) — chain two fan-outs instead; incremental append (the
anti-join-in-prep-query pattern covers "only fetch new ids" well enough).

**Minor UX fix, same eval — done:** the catalog's `method` column tripped the agent
(`WHERE method='get'` → 0 rows against `'GET'`). Catalog rows now store the method
**lowercase**, matching the OpenAPI document's own keys.

---

## 4. References

- JOSIE: overlap set similarity search for joinable tables —
  <https://www.cs.toronto.edu/~fnargesian/JOSIE_Overlap_Set_Similarity_Search_for_Finding_Joinable_Tables_in_Data_Lakes.pdf>
- LakeBench (VLDB'24), joinable/unionable discovery benchmark —
  <https://www.vldb.org/pvldb/vol17/p1925-chai.pdf>
- DuckDB FTS extension — <https://duckdb.org/docs/current/core_extensions/full_text_search>
- DuckDB VSS extension — <https://duckdb.org/docs/current/core_extensions/vss>
- VSS status ("what's new") — <https://duckdb.org/2024/10/23/whats-new-in-the-vss-extension>
- MotherDuck on combined FTS + embedding search in DuckDB —
  <https://motherduck.com/blog/search-using-duckdb-part-3/>
- DuckDB httpfs HTTP(S) support (HTTP secrets, headers) —
  <https://duckdb.org/docs/lts/core_extensions/httpfs/https>
- EXTRA_HTTP_HEADERS / bearer-auth how-to discussion —
  <https://github.com/duckdb/duckdb/discussions/14165>
- MotherDuck "DuckDB 1.1 hidden gems" (HTTP secrets + read_json over APIs) —
  <https://motherduck.com/blog/duckdb-110-hidden-gems/>
- dlt REST API → DuckDB loading — <https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb>
