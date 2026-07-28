# Potential extensions

Ideas surfaced during development that aren't worth building yet — with the context needed to
pick them up later.

> Larger feature designs live in their own docs: cross-source join-key discovery (`relate`),
> durable notebook mode, and API sources are explored in `design_join_discovery_and_notebook.md`.

## Workspace GC: distinguish "locked" from "unopenable" in the sweep probe

`_probe_workspace` (core/duck.py) treats **any** `duckdb.connect` failure as "live owner holds
the lock" and skips the dir. That is the safe default, but it conflates two cases:

- **locked** — a live server owns the workspace (never touch; correct today);
- **unopenable** — the file is corrupt, truncated, or written by an incompatible DuckDB version.
  These dirs can never be reclaimed by the sweep, ever, because the probe permanently
  misclassifies them as live.

Refinement: inspect the exception. DuckDB's lock contention raises `IOException` whose message
names the holding process ("File is already open in ... (PID n)"); other failures (corrupt
header, version mismatch) have distinct messages. A dir that is *unopenable* (not merely locked)
and past the grace window could be reclaimed — or at least surfaced in a startup stderr warning
instead of silently accumulating. Parsing error text is brittle, so pair it with a liveness
cross-check (does the PID in the message exist?) before deleting anything.

Context (2026-07-12): found while diagnosing why empty workspace dirs survived the new
empty-workspace reaping — in that case the probes were failing because the owners really were
alive (orphaned `claude.exe` sessions from July 10), so the sweep was correct. But the
diagnosis showed the probe can't tell that case apart from a corrupt file. Not urgent: corrupt
workspaces don't accumulate the way reconnect churn does.

## BUG(?): a live server can lose its on-disk workspace and silently run memory-only

Under MCP reconnect churn, a **live** per-process server was observed with **no backing file on
disk** — its DuckDB workspace was running memory-only, silently voiding the durability guarantee
(results + lineage that are supposed to survive reopen). Everything in the session evaporates
when the process exits; a hard kill loses it all with no warning.

Symptoms observed (2026-07-13, diagnosing "where is the `agent1_pipe` flow's .duckdb file?"):
- `SELECT * FROM duckdb_databases()` on the live server reports its `workspace` path as the
  intended `<session-dir>/<pid>-<rand>/workspace.duckdb` (not `:memory:`, not an ephemeral temp
  fallback), yet that file **and its `.wal` do not exist** (confirmed via `ls`, `find`, and
  PowerShell `Test-Path`).
- Writing a fresh 50k-row table into the live server produced **no file and no WAL** at that path
  or anywhere under `%TEMP%` — proof the connection is genuinely memory-only, not merely
  awaiting a checkpoint (a file-backed DuckDB connection writes a `.wal` the instant a table is
  created; verified in a standalone repro).
- The workspace **dir** still exists, containing only a non-empty `tool-calls.jsonl` and an empty
  `spill/` — the DB file + WAL are the only things missing.

Leading hypothesis (NOT fully proven): the workspace GC (`_reclaim_old_workspaces`) reclaimed
this session's own on-disk workspace during a transient window where it looked reapable — the
idle gap between server startup and its first query, i.e. past the 60s grace window, with zero
user tables → the "empty → reclaim even within the keep window" branch. The `rmtree` deletes the
DB file + WAL, then throws when it reaches the still-open `tool-calls.jsonl` (`except OSError:
pass`), leaving exactly the half-reclaimed dir observed. The server kept serving from its
in-memory catalog. Contributing factor: the process list showed 6+ concurrent
`spelunk.mcp.server` processes, several launched in **identical-timestamp pairs** (the MCP client
appears to spawn the server twice per connect) — lots of simultaneous startups = lots of sweeps.

Caveats / open questions before acting:
- On Windows an **open** DuckDB file resists deletion (`rmtree` on a dir with the DB still open by
  another process fails with `WinError 32` and leaves the DB file intact — verified). So for the
  file to have been deleted, the owning server must NOT have held it at that instant. That's
  consistent with the idle-startup-window theory but I couldn't reproduce the exact race from
  outside the process. It's possible the deletion path differs (e.g. the paired twin process, or
  the in-process cached-instance issue — see the probe note above: a same-process
  `duckdb.connect` shares the cached instance and would misjudge the dir as reapable, though that
  only bites on same-process double-open, and the pairs here are distinct PIDs).
- Related to the probe-classification section above — both are about the sweep touching a dir it
  shouldn't. This one is worse because the victim is *live*, not a stale husk.

Directions to consider:
1. **Own-file self-check**: server logs its resolved workspace path at startup, and periodically
   (or on each tool call) verifies its backing file still exists; if gone, re-create/re-checkpoint
   or at minimum emit a loud stderr warning so silent data-loss becomes visible.
2. **Claim protection**: once a process owns its workspace, make the dir un-reapable for the
   process lifetime regardless of transient emptiness/lock state — e.g. a sentinel lock file the
   sweep honors, or holding an exclusive OS handle, rather than relying on the DuckDB write-lock
   being held at the exact sweep instant.
3. **Don't reap "empty" within the keep window on the idle assumption**: a fresh server that
   hasn't run a query yet is empty-but-alive, not garbage; empty is transient mid-session, not
   terminal. Gate the empty-reap on a stronger liveness signal than the write-lock probe.
4. **Reduce churn at the source**: figure out why the MCP client spawns paired simultaneous
   servers; fewer concurrent startups shrinks the race window.

Repro aid: reproduces most easily under real reconnect churn (many servers starting within
seconds). A synthetic repro would start server A (per_process), leave it idle >60s with an empty
workspace, then start server B and let B's sweep run while A is still alive, and assert A's
`workspace.duckdb` still exists and A can still checkpoint.

## UX papercut: `*` in a remote URL is rejected as an unsupported HTTP glob

A `query` that reads a remote API endpoint whose URL contains a literal `*` fails before any
data is fetched:

```
Invalid Input Error: Globs (`*`) for generic HTTP file is are not supported.
Consider `SET allow_asterisks_in_http_paths = true;` to allow this behaviour
```

DuckDB's httpfs treats `*` anywhere in an HTTP path/query string as a filename glob and refuses
it. The most common trigger is a REST/SoQL analytics URL used as a CSV source — e.g. Chicago's
Socrata portal with `...?$select=...count(*)%20as%20cnt&$group=...`, where the `count(*)` lands
in the URL. It bites the moment an agent points `read_csv_auto()` at an aggregating API instead
of a plain file, which is a natural and increasingly common pattern.

Context (2026-07-16): observed in a workflow-subagent run joining NOAA GHCN weather to the
Chicago crimes Socrata dataset. The agent hit this on its first crimes probe (`count(*)`),
diagnosed it, and self-recovered by URL-encoding `*` to `%2A` in one retry — so a capable agent
routes around it, but it costs a round trip and a less capable one could stall. The DuckDB hint
(`SET allow_asterisks_in_http_paths = true`) is a second escape hatch, but the guard/DDL path
constructs the wrapping `CREATE TABLE` itself, so it's unclear an agent can reliably issue that
SET through `query` — worth checking.

Directions to consider:
1. **Detect and guide**: when a `query` error matches this glob message *and* the SQL contains a
   remote `read_*` URL with a raw `*`, rewrite the tool error into an actionable hint — "a `*` in
   a remote URL is read as a file glob; URL-encode it as `%2A`, or the source is an aggregating
   API — encode literals in the query string." A one-line nudge turns a dead end into a fix.
2. **Auto-encode (careful)**: optionally percent-encode a bare `*` inside a recognized remote URL
   literal before handing SQL to DuckDB. Risky — must not touch `SELECT *` or other legitimate
   `*` outside the URL string, so scope strictly to the contents of a `read_*('...')` argument.
3. **Session default**: consider `SET allow_asterisks_in_http_paths = true` at `open()` if it
   doesn't weaken any intended glob behavior for legitimate multi-file remote sources — this
   trades one footgun (rejected literal `*`) for permitting real globs, so weigh against the
   file-source glob semantics before enabling.
4. **Docs**: note the `%2A` workaround wherever remote/API URLs as sources are described, since
   "point it at a Socrata/REST CSV export" is exactly the case that trips it.

## Remove the "materialize the source vs `add_source`" decision from the agent

**Problem.** A source can enter the workspace two ways, and the agent is forced to choose between
them per source: `query("SELECT * FROM read_X(url)", name=...)` materializes a real TABLE (eager,
copied, cached), while `add_source` / `--source` registers a lazy VIEW (re-scanned, pushdown for
parquet). The "right" choice depends on format (CSV has no pushdown and re-fetches wholesale on
every scan; parquet range-reads only needed row groups), size (fits disk or not), and reuse
pattern (queried once vs many times). That's real per-source cognitive load, and it's easy to get
wrong in the expensive direction — e.g. `SELECT * materialize` of a massive remote parquet
downloads the whole file and defeats pushdown, exactly what you must NOT do. The current
wholesale-copy *nudge* flags this but leaves the decision (and the mistake) with the agent.

**Root confusion.** The dichotomy conflates two different axes that shouldn't be weighed against
each other:
- **Availability** — making a source *referenceable*. That's registration (`add_source` /
  `--source`), and it is always a lazy handle (VIEW for files/scans, `ATTACH` for DBs). No bytes
  copied — even binding a view over a 50GB parquet reads only the footer.
- **Materialization** — putting bytes into the workspace as a table. That's `query(name=...)`.

They're layers, not alternatives. Collapse the decision by giving each layer exactly one answer:

> **Invariant: a source is always a lazy handle; only *derived results* are ever materialized;
> whether a source's bytes are cached locally is the engine's decision, never the agent's.**

Under that invariant the agent's rule is one sentence — *never materialize a source; your first
`query` reduces it, and that reduced result is your cache* — because if the first query against a
source is a reduction that gets materialized, the source is scanned exactly once regardless.

**Enforceable rules (not nudges):**

1. **R1 — Registration is the only lazy entry point.** `query` cannot introduce a source; every
   `add_source` yields a VIEW/ATTACH with no wholesale copy at registration time. (Essentially
   true today — state it as an invariant and keep it.)
2. **R2 — Guard forbids/rewrites a wholesale source copy.** Detect `CREATE TABLE x AS SELECT *
   FROM <single source>` with no projection-expression / `WHERE` / `GROUP BY` (a bare passthrough)
   via the sqlglot guard and, instead of nudging, **rewrite `x` into a view/alias of the source**
   rather than a table. Semantically safe *precisely* because `SELECT * FROM source` equals the
   source; the agent still gets a queryable name, minus the copy. (Reject-with-error is the
   simpler alternative but needs an escape hatch or it thrashes.) This still permits the
   legitimate full materialize — e.g. `SELECT CAST(...), VALUE/10.0 ... FROM noaa` has real
   projection/transform, so it's a derived result, not a passthrough; the rule fires only on
   *large + bare* `SELECT *`.
3. **R3 — Local caching is a deterministic engine policy, not a choice.** Decide "should these
   remote bytes live locally?" as a pure function of `(format, size, locality)` at registration,
   invisible to the agent: local file / DB / remote parquet → lazy (parquet range-reads, never
   copied); small remote CSV/JSON (below a threshold) → auto-cache once into a local parquet and
   repoint the view; large remote CSV/JSON → lazy view + optional streamed `COPY` to local parquet
   (one fetch, then local).
4. **R4 — Remove the *performance* reason to materialize a source, so R2 never hurts.** The honest
   reason an agent reaches for `SELECT * materialize` is "so I don't re-fetch every query." Kill
   that incentive at the engine: enable DuckDB's caching at `open()` — external file cache
   (`enable_external_file_cache`) and HTTP metadata cache (`enable_http_metadata_cache`), and
   consider the `cache_httpfs` extension for on-disk caching. Then re-referencing a remote source
   is already cheap without a local table. (Confirm which pragmas the pinned DuckDB version
   actually exposes — these have shifted across versions.)

**Edge cases any rule set must survive:**
- *"I need all columns and rows locally for 20 analyses."* Small → R3 auto-caches it; you never
  asked. Large → you shouldn't hold a full local copy anyway — pushdown + a materialized reduction
  is correct.
- *Genuine full-copy need* (offline snapshot) → give it one honest verb (`export` already exists,
  or a `cache_source(name)`), so it's explicit and named for what it is, not smuggled through
  `query`.
- *Lineage under R2's rewrite* → record the rewritten passthrough as a source-alias, not a derived
  result, so `replay` doesn't try to rebuild a source.

**Where to start:** R2 (nudge → guard rule, rewrite-to-view flavor) plus R4 (turn on the caching
pragmas so the rule costs no latency) get ~90% of the value and are both mechanical, not judgment
calls. R3's size/format policy is the more ambitious follow-up.

Context (2026-07-18): crystallized while reviewing the NOAA×Chicago-crimes subagent run (see the
glob papercut above) and discussing whether an agent should `add_source` a remote parquet vs
materialize it. The run itself showed the materialize-once economics paying off (19s remote load →
sub-ms local analysis), which is exactly the behavior these rules would make automatic instead of
agent-chosen.
