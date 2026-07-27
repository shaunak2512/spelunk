# Spelunk claim register

The docs are the spec. This directory turns every falsifiable assertion in
`README.md`, `CLAUDE.md`, the MCP server instructions, and the tool/CLI descriptions into a
numbered **claim**, each with the observation that would prove it false.

The register is the eval's coverage denominator: not lines of code, not test count, but
*claims that spelunk makes and has not yet been forced to defend*.

## Rules

1. **Written from the docs, not the code.** Claims are extracted from user-facing text and
   falsifiers are written blind to the implementation. A test written by someone who read the
   implementation confirms the implementation; it cannot catch a doc that overclaims.
2. **A claim is a promise to a user.** If no reader would change their behaviour based on it,
   it isn't a claim — it's prose. Internal design notes ("uses stdlib urllib") only become
   claims where they have an observable consequence.
3. **A mismatch is always a finding.** Either the code is wrong or the doc is wrong. The
   register forces the choice instead of letting it drift.
4. **`covered_by` must resolve.** Every listed test node id is checked to exist by
   `coverage.py`. A renamed test breaks the build rather than silently orphaning a claim.

## Schema

```yaml
- id: SRC-001                  # <AREA>-<n>, stable forever; never renumber
  claim: >                     # the promise, quoted or tightly paraphrased from the doc
    Database sources are attached READ_ONLY.
  source: CLAUDE.md#sources.py # where the claim is made (file#anchor)
  mode: invariant              # invariant | behavioral | quantitative | affordance
  falsifier: >                 # the observation that would prove the claim false
    Any INSERT/UPDATE/DDL against an attached database succeeds.
  status: verified             # verified | partial | unverified | refuted
  covered_by:                  # resolvable pytest node ids (empty when unverified)
    - tests/test_sources.py::TestAttachAll::test_attached_sqlite_is_read_only
  gap: >                       # what is still unchecked (required unless status is verified)
    Only SQLite is exercised; Postgres/MySQL read-only is asserted nowhere.
  note: >                      # optional: how the claim was settled, kept for the record
    Refuted when first tested on 2026-07-28, then fixed.
```

## Modes — these decide which harness can falsify the claim

| Mode | Shape | Harness |
|---|---|---|
| `invariant` | must hold on *all* inputs | property-based / fuzz + taint tracking |
| `behavioral` | given X, do Y | fixture tests against a hostile fixture world |
| `quantitative` | a measured threshold | instrumented runs with budgets |
| `affordance` | an agent *can* / *will* do X | agent-in-the-loop, with an ablation arm |

`affordance` claims cannot be falsified by any unit test. They are claims about what a model
does when handed this surface, and they stay `unverified` until an agent harness exists.

## Status meanings

- **verified** — a listed test would fail if the claim were false.
- **partial** — tests exercise the claim on some inputs, but a stated part of it is unchecked.
  `gap` says which part.
- **unverified** — no test would fail if the claim were false.
- **refuted** — the claim is false as written. Fix the code or fix the doc.

## Running

```powershell
.\.venv\Scripts\python.exe evals\coverage.py             # coverage report
.\.venv\Scripts\python.exe evals\coverage.py --strict    # non-zero exit on any problem
.\.venv\Scripts\python.exe evals\coverage.py --unverified # just the gaps, for planning
```

`--strict` fails on: a duplicate id, an unresolvable `covered_by` node id, a `verified` claim
with no coverage, or a non-`verified` claim with no `gap`. Wire that into CI once the register
is stable, so a claim can never be added without either a falsifier or an admission that there
isn't one.
