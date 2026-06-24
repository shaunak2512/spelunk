# Wave 2b · mcp  (NEW file + NEW tests) — OPTIONAL EXTRA

- **Worktree:** `../spelunk-wt/mcp`  **Branch:** `wave2/mcp`
- **Create:** `spelunk/mcp/server.py` **and** `tests/test_mcp_server.py` (write the tests **first**).
- **Depends on:** `introspect` + `query` **merged to `main`** (Wave 2a). **Build only if pursuing the MCP front-end** (it's an explicit optional extra in the spec).

Read `briefs/ORCHESTRATION.md` first. Install: `fastmcp sqlalchemy sqlglot pydantic pytest`.

Expose `spelunk.core` over MCP (FastMCP), reusing core verbatim. **No model, no loop** — Claude Code is the agent.
- `build_server(engine) -> FastMCP` with:
  - a resource listing tables (wraps `core.list_objects`),
  - resource `db://{table}` → `describe()` output (wraps `core.describe`) — the discovery-FS as MCP resources,
  - tool `run_query(sql)` → rows (wraps `core.run_sql`; governed/read-only).
- Provide a `__main__` entry that reads a `--dsn` arg and serves over stdio (for Claude Code via `.mcp.json`).

Tests (offline, no server spin-up): assert resources/tools are registered and the underlying handlers return correct data against `sample_db` (call the handler callables directly, or FastMCP's in-process client if simple).

## Done when
`tests/test_mcp_server.py` green (or the suite cleanly skips if you defer MCP). Commit to `wave2/mcp`.
