# Wave 2b · rag  (NEW file + NEW tests)

- **Worktree:** `../spelunk-wt/rag`  **Branch:** `wave2/rag`
- **Create:** `spelunk/rag/schema_index.py` **and** `tests/test_schema_index.py` (write the tests **first**).
- **Depends on:** `introspect` **merged to `main`** (Wave 2a).

Read `briefs/ORCHESTRATION.md` first. Install: `numpy sqlalchemy pydantic pytest`.

Retrieve the top-k relevant tables for a question by embedding similarity. **Make the embedder INJECTABLE** so tests run offline.
- `class SchemaIndex:`
  - `__init__(self, embed_fn: Callable[[list[str]], np.ndarray])` — `embed_fn` maps texts → `(n, d)` array.
  - `build(self, engine) -> None` — one "document" per table (name + column names + comment) via `core.introspect`, then embed.
  - `retrieve(self, question: str, k: int = 5) -> list[str]` — cosine top-k table names.
- Provide a default provider-embedder factory (e.g. OpenAI `text-embedding-3-small`) but **do not call it in tests**.

Tests (offline): inject a deterministic fake `embed_fn` (e.g. bag-of-words / hashing vectors); `build` over `sample_db`; assert `retrieve("orders placed by each customer")` ranks `orders`/`customers` at the top. Cosine in numpy.

## Done when
`tests/test_schema_index.py` green. Commit to `wave2/rag`.
