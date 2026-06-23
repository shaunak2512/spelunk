"""SQL safety guards (Wave 1). Parse with sqlglot (AST), never regex.

Both functions are pure (string -> string / None); no DB connection needed, which is
why they are the cleanest first thing to implement and test.
"""
from __future__ import annotations

from .types import UnsafeSQLError  # noqa: F401  (raised by the implementation)


def assert_read_only(sql: str, dialect: str = "sqlite") -> None:
    """Raise ``UnsafeSQLError`` unless ``sql`` is a single read-only statement.

    Contract:
      * allow exactly ONE statement, and it must be a ``SELECT`` (leading CTEs / ``WITH`` ok).
      * reject DML (INSERT/UPDATE/DELETE/MERGE/REPLACE/UPSERT), DDL
        (CREATE/DROP/ALTER/TRUNCATE), GRANT/REVOKE, write PRAGMAs, and any multi-statement input.
      * parse via ``sqlglot.parse(sql, read=dialect)`` and inspect the AST — do not regex.
      * on unparseable input, raise ``UnsafeSQLError`` (fail closed).
    """
    raise NotImplementedError("Wave 1: implement read-only AST check with sqlglot")


def enforce_limit(sql: str, dialect: str = "sqlite", max_rows: int = 1000) -> str:
    """Return ``sql`` rewritten so the outermost SELECT yields at most ``max_rows`` rows.

    Contract:
      * no existing LIMIT            -> inject ``LIMIT max_rows``.
      * existing LIMIT > max_rows    -> clamp to ``max_rows``.
      * existing LIMIT <= max_rows   -> leave unchanged.
      * rewrite on the sqlglot AST and re-render in ``dialect``; preserve the query otherwise.
    """
    raise NotImplementedError("Wave 1: implement LIMIT injection/clamping with sqlglot")
