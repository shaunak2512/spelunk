"""Database connection management (Wave 1)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def connect(dsn: str, *, read_only: bool = True) -> "Engine":
    """Return a pooled SQLAlchemy ``Engine`` for ``dsn``.

    Contract:
      * ``read_only=True`` must block writes as defence-in-depth alongside ``guard`` —
        e.g. SQLite ``?mode=ro`` / immutable, or a read-only role/transaction for server DBs.
      * use SQLAlchemy's default connection pooling.
      * ``dsn`` is a standard SQLAlchemy URL, e.g. ``sqlite:///path/to.db``.
    """
    raise NotImplementedError("Wave 1: create_engine + read-only enforcement")
