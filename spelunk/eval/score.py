"""BIRD-style execution accuracy.

This is the scoring core of the eval layer. It executes a *gold* SQL and a *predicted*
SQL against the same SQLite database and decides whether the prediction is execution-
accurate, i.e. whether the two queries return the same result.

Comparator semantics (adapted from BIRD's official ``evaluation.py``)
---------------------------------------------------------------------
BIRD's reference comparator does ``set(predicted_res) == set(ground_truth_res)``:

  * **Order-insensitive.** Row order is ignored, so a prediction is *not* penalised for
    a missing/extra ``ORDER BY``. We match this — BIRD does not respect ``ORDER BY`` even
    when the gold query has one.
  * BIRD's literal use of ``set()`` additionally *dedupes* rows. We deliberately use
    **multiset** semantics instead (``sorted(a) == sorted(b)``): row multiplicity is
    preserved, so ``[(1,), (1,)]`` != ``[(1,)]``. This is the documented choice in this
    brief and is the stricter, more faithful comparison of two result tables — a query
    that emits duplicate rows is genuinely different from one that collapses them. It is
    a superset-correct refinement of BIRD's set comparison (anything BIRD calls unequal,
    we also call unequal; we additionally distinguish duplicate counts).

Rows are compared as whole tuples; cell values are compared with Python equality after a
light normalisation pass that maps each cell to a sort-stable, type-tagged key (so that a
mix of ``None``, numbers, and strings across rows can be sorted without raising
``TypeError`` on Python 3, and so ``int``/``float`` that are numerically equal — e.g.
``1`` vs ``1.0`` — compare equal, matching SQLite's loose numeric affinity).

Trust boundary
--------------
``run_sql_raw`` uses **plain ``sqlite3``** and intentionally bypasses ``spelunk.core``
guards: this module scores *trusted* gold SQL and model-predicted SQL during offline
evaluation, where the point is to observe exactly what each query returns (including
writes/errors) rather than to sandbox it. Guarded, read-only execution for the live agent
lives in ``spelunk.core.query`` instead.
"""
from __future__ import annotations

import sqlite3

__all__ = ["run_sql_raw", "compare_result_sets", "execution_accuracy"]


def run_sql_raw(db_path: str, sql: str) -> list[tuple]:
    """Execute ``sql`` against the SQLite DB at ``db_path`` and return all rows.

    Plain ``sqlite3.execute`` + ``fetchall`` — no guards, no row cap, no timeout. Rows are
    returned as a list of tuples. May raise ``sqlite3.Error`` (callers that must not raise,
    such as :func:`execution_accuracy`, catch it themselves).
    """
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(sql)
        rows = cur.fetchall()
    finally:
        con.close()
    # sqlite3 already yields tuples, but normalise defensively (e.g. if a Row factory
    # were ever set) so downstream comparison always sees plain tuples.
    return [tuple(row) for row in rows]


def _cell_key(value: object) -> tuple[int, object]:
    """Map a cell to a (type-rank, comparable) key.

    Lets heterogeneous rows (containing ``None``, ints/floats, strings, bytes) be sorted
    deterministically without ``TypeError``, while keeping numerically-equal ints and
    floats in the same bucket so ``1`` and ``1.0`` collate together.
    """
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        # bool is an int subclass; treat as numeric (True==1, False==0) like SQLite.
        return (1, float(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    if isinstance(value, bytes):
        return (3, value)
    return (2, str(value))


def _row_key(row: tuple) -> tuple:
    return tuple(_cell_key(cell) for cell in row)


def compare_result_sets(a: list[tuple], b: list[tuple]) -> bool:
    """Return ``True`` iff ``a`` and ``b`` are equal as **multisets of rows**.

    Order-insensitive (BIRD semantics); row multiplicity *is* significant (our documented
    refinement of BIRD's ``set()`` comparison). Cells are normalised so that ``None`` and
    mixed numeric/text types sort deterministically and ``int``/``float`` numeric equals
    collate together.
    """
    if len(a) != len(b):
        return False
    return sorted(a, key=_row_key) == sorted(b, key=_row_key)


def execution_accuracy(pred_sql: str, gold_sql: str, db_path: str) -> bool:
    """Return ``True`` iff ``pred_sql`` and ``gold_sql`` produce the same result set.

    Both queries run against the DB at ``db_path`` via :func:`run_sql_raw`. A prediction
    (or gold) that fails to execute — syntax error, unknown table/column, missing DB,
    etc. — yields ``False`` rather than raising: this function **never** propagates an
    exception, so a single bad query can't crash a scoring run.
    """
    try:
        pred_rows = run_sql_raw(db_path, pred_sql)
        gold_rows = run_sql_raw(db_path, gold_sql)
    except Exception:
        # Any execution failure (sqlite3.Error, and defensively anything else) => not
        # execution-accurate. Never raise out of the scorer.
        return False
    return compare_result_sets(pred_rows, gold_rows)
