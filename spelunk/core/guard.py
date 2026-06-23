"""SQL safety guards (Wave 1). Parse with sqlglot (AST), never regex.

Both functions are pure (string -> string / None); no DB connection needed, which is
why they are the cleanest first thing to implement and test.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .types import UnsafeSQLError

# DML/DDL node types that must never appear anywhere in a read-only statement.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
)


def assert_read_only(sql: str, dialect: str = "sqlite") -> None:
    """Raise ``UnsafeSQLError`` unless ``sql`` is a single read-only statement.

    Contract:
      * allow exactly ONE statement, and it must be a ``SELECT`` (leading CTEs / ``WITH`` ok).
      * reject DML (INSERT/UPDATE/DELETE/MERGE/REPLACE/UPSERT), DDL
        (CREATE/DROP/ALTER/TRUNCATE), GRANT/REVOKE, write PRAGMAs, and any multi-statement input.
      * parse via ``sqlglot.parse(sql, read=dialect)`` and inspect the AST — do not regex.
      * on unparseable input, raise ``UnsafeSQLError`` (fail closed).
    """
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except SqlglotError as err:  # fail closed on any parse error
        raise UnsafeSQLError(f"could not parse SQL: {err}") from err

    # Drop empty statements (e.g. trailing ';' yields a None entry).
    statements = [stmt for stmt in statements if stmt is not None]

    if len(statements) != 1:
        raise UnsafeSQLError(
            f"expected exactly one statement, got {len(statements)}"
        )

    stmt = statements[0]

    # A WITH ... SELECT CTE parses to a Select carrying a `with` arg, so the
    # top-level node must itself be a Select (or a set op of selects).
    if not isinstance(stmt, (exp.Select, exp.Union, exp.Subquery)):
        raise UnsafeSQLError(
            f"statement is not a SELECT: {type(stmt).__name__}"
        )

    # Defense in depth: reject any embedded DML/DDL node anywhere in the tree
    # (e.g. CTEs or subqueries that smuggle in writes).
    forbidden = stmt.find(*_FORBIDDEN_NODES)
    if forbidden is not None:
        raise UnsafeSQLError(
            f"statement contains a non-read-only node: {type(forbidden).__name__}"
        )


def enforce_limit(sql: str, dialect: str = "sqlite", max_rows: int = 1000) -> str:
    """Return ``sql`` rewritten so the outermost SELECT yields at most ``max_rows`` rows.

    Contract:
      * no existing LIMIT            -> inject ``LIMIT max_rows``.
      * existing LIMIT > max_rows    -> clamp to ``max_rows``.
      * existing LIMIT <= max_rows   -> leave unchanged.
      * rewrite on the sqlglot AST and re-render in ``dialect``; preserve the query otherwise.
    """
    expression = sqlglot.parse_one(sql, read=dialect)

    limit = expression.args.get("limit")
    if limit is None:
        # No LIMIT present: inject one. Select.limit() returns a new expression.
        expression = expression.limit(max_rows)
    else:
        # Existing LIMIT: clamp only if it is a plain integer literal above the cap.
        limit_expr = limit.expression
        if isinstance(limit_expr, exp.Literal) and limit_expr.is_int:
            if int(limit_expr.name) > max_rows:
                expression = expression.limit(max_rows)
        else:
            # Non-literal LIMIT (e.g. a placeholder/expression): clamp to be safe.
            expression = expression.limit(max_rows)

    return expression.sql(dialect=dialect)
