"""RAG schema index — retrieves the most relevant tables for a natural-language question.

Usage (offline / test)::

    from spelunk.rag.schema_index import SchemaIndex

    def my_embed(texts: list[str]) -> np.ndarray: ...   # shape (n, d)

    idx = SchemaIndex(embed_fn=my_embed)
    idx.build(engine)
    tables = idx.retrieve("orders placed by each customer", k=3)

Usage (production with OpenAI)::

    from spelunk.rag.schema_index import SchemaIndex, openai_embed_fn

    idx = SchemaIndex(embed_fn=openai_embed_fn())
    idx.build(engine)
    tables = idx.retrieve("total revenue per region", k=5)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import numpy as np

from spelunk.core.introspect import describe, list_objects

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class SchemaIndex:
    """Vector index over table documents for cosine-similarity retrieval.

    Parameters
    ----------
    embed_fn:
        A callable that maps a list of *n* strings to a numpy array of shape
        ``(n, d)`` where *d* is the embedding dimension.  The function must
        accept an arbitrary-length list and return a 2-D float array.
        Normalisation (l2 or otherwise) is the caller's responsibility; the
        ``retrieve`` method computes raw dot-products after normalising the
        stored vectors and the query vector itself, so the results are always
        proper cosine similarities regardless.
    """

    def __init__(self, embed_fn: Callable[[list[str]], np.ndarray]) -> None:
        self._embed_fn = embed_fn
        # Populated by build():
        self.table_names: list[str] = []
        self.vectors: np.ndarray = np.empty((0, 0))
        self._docs: list[str] = []

    # ------------------------------------------------------------------
    # Building the index
    # ------------------------------------------------------------------

    def build(self, engine: "Engine") -> None:
        """Discover all tables via ``core.introspect``, embed one doc per table.

        One document per table is constructed from:
        * the table name
        * all column names (comma-separated)
        * the table comment (if any)

        The documents are embedded in a single ``embed_fn`` call and stored as
        a normalised matrix for fast cosine retrieval.
        """
        tables = list_objects(engine)

        names: list[str] = []
        docs: list[str] = []

        for tbl in tables:
            td = describe(engine, tbl.name, profile=False)
            col_names = ", ".join(c.name for c in td.columns)
            parts = [tbl.name, col_names]
            if tbl.comment:
                parts.append(tbl.comment)
            doc = " ".join(parts)
            names.append(tbl.name)
            docs.append(doc)

        if not docs:
            self.table_names = []
            self._docs = []
            self.vectors = np.empty((0, 0))
            return

        raw = np.array(self._embed_fn(docs), dtype=float)
        # l2-normalise rows so dot-product == cosine similarity at query time
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normalised = raw / norms

        self.table_names = names
        self._docs = docs
        self.vectors = normalised

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(self, question: str, k: int = 5) -> list[str]:
        """Return the top-k table names most similar to *question*.

        Similarity is cosine similarity computed as a dot product against the
        l2-normalised stored vectors (``build`` normalises them; we normalise
        the query vector here).

        If the index is empty, returns an empty list.
        If *k* exceeds the number of tables, all tables are returned (ranked).
        """
        if not self.table_names:
            return []

        # Embed and normalise the query
        q_raw = np.array(self._embed_fn([question]), dtype=float)  # (1, d)
        q_norm = np.linalg.norm(q_raw)
        if q_norm == 0:
            q_vec = q_raw[0]
        else:
            q_vec = q_raw[0] / q_norm

        # Cosine similarities: dot product against already-normalised rows
        sims = self.vectors @ q_vec  # shape (n_tables,)

        # Rank descending, clip k to available tables
        effective_k = min(k, len(self.table_names))
        top_indices = np.argsort(sims)[::-1][:effective_k]

        return [self.table_names[i] for i in top_indices]


# ---------------------------------------------------------------------------
# Default provider factory (lazy-imported — safe to import without openai SDK)
# ---------------------------------------------------------------------------

def openai_embed_fn(
    model: str = "text-embedding-3-small",
) -> Callable[[list[str]], np.ndarray]:
    """Return an ``embed_fn`` backed by the OpenAI Embeddings API.

    The ``openai`` package is imported lazily inside the returned closure so
    that importing this module does **not** require the SDK to be installed.

    Example::

        from spelunk.rag.schema_index import SchemaIndex, openai_embed_fn
        idx = SchemaIndex(embed_fn=openai_embed_fn())
        idx.build(engine)
    """
    def _embed(texts: list[str]) -> np.ndarray:
        # Lazy import — only runs when the factory result is actually called.
        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "openai package is required for the default embed_fn. "
                "Install it with: pip install openai"
            ) from exc

        client = OpenAI()
        response = client.embeddings.create(input=texts, model=model)
        vectors = [item.embedding for item in response.data]
        return np.array(vectors, dtype=float)

    return _embed
