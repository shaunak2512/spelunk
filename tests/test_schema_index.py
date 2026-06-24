"""Tests for spelunk.rag.schema_index — runs fully offline via a fake embed_fn."""
from __future__ import annotations

import numpy as np
import pytest

from spelunk.core.connection import connect
from spelunk.rag.schema_index import SchemaIndex


# ---------------------------------------------------------------------------
# Fake deterministic embedder
# ---------------------------------------------------------------------------

# A small fixed vocabulary.  Each word that appears in a document gets a 1
# in the corresponding position; words outside the vocab are ignored.  The
# resulting sparse bag-of-words vector is l2-normalised so cosine similarity
# is just a dot product — same as the production path.
_VOCAB = [
    "customers", "customer", "orders", "order", "id", "name", "city",
    "signup", "date", "amount", "status", "customer_id",
]


def _bow(texts: list[str]) -> np.ndarray:
    """Bag-of-words over _VOCAB, l2-normalised. Shape (n, len(_VOCAB))."""
    n = len(texts)
    d = len(_VOCAB)
    mat = np.zeros((n, d), dtype=float)
    for i, text in enumerate(texts):
        words = text.lower().replace(",", " ").replace("(", " ").replace(")", " ").split()
        for w in words:
            if w in _VOCAB:
                mat[i, _VOCAB.index(w)] += 1.0
    # l2-normalise each row (avoid div-by-zero)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return mat / norms


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def schema_index(sample_db) -> SchemaIndex:
    """A built SchemaIndex over the sample_db, using the offline fake embedder."""
    idx = SchemaIndex(embed_fn=_bow)
    engine = connect(sample_db, read_only=False)
    idx.build(engine)
    return idx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSchemaIndexBuild:
    def test_build_discovers_tables(self, schema_index: SchemaIndex):
        """After build(), both tables in the sample DB are indexed."""
        assert set(schema_index.table_names) == {"customers", "orders"}

    def test_vectors_shape(self, schema_index: SchemaIndex):
        """Stored vectors have shape (n_tables, embedding_dim)."""
        n, d = schema_index.vectors.shape
        assert n == 2, "expected 2 tables"
        assert d == len(_VOCAB), "expected vocab-width embeddings"

    def test_build_idempotent(self, sample_db):
        """Calling build() twice replaces the index cleanly."""
        idx = SchemaIndex(embed_fn=_bow)
        engine = connect(sample_db, read_only=False)
        idx.build(engine)
        first_names = list(idx.table_names)
        idx.build(engine)
        assert list(idx.table_names) == first_names


class TestSchemaIndexRetrieve:
    def test_retrieve_returns_list(self, schema_index: SchemaIndex):
        result = schema_index.retrieve("orders placed by each customer")
        assert isinstance(result, list)

    def test_retrieve_length_capped_at_k(self, schema_index: SchemaIndex):
        """retrieve() returns at most k results, even when k > n_tables."""
        assert len(schema_index.retrieve("anything", k=1)) == 1
        # k larger than the number of tables — clamp to available tables
        assert len(schema_index.retrieve("anything", k=10)) == 2

    def test_retrieve_orders_customers_top(self, schema_index: SchemaIndex):
        """'orders placed by each customer' should surface both tables."""
        result = schema_index.retrieve("orders placed by each customer", k=2)
        assert set(result) == {"orders", "customers"}, (
            f"Expected both tables in top-2, got {result}"
        )

    def test_retrieve_orders_is_top1(self, schema_index: SchemaIndex):
        """A query focused on 'orders' alone should rank 'orders' first."""
        result = schema_index.retrieve("order amount status", k=1)
        assert result[0] == "orders", f"Expected 'orders' as top result, got {result}"

    def test_retrieve_customers_is_top1(self, schema_index: SchemaIndex):
        """A query focused on 'customers' alone should rank 'customers' first."""
        result = schema_index.retrieve("customer name city signup", k=1)
        assert result[0] == "customers", f"Expected 'customers' as top result, got {result}"

    def test_retrieve_unknown_question_still_returns_k(self, schema_index: SchemaIndex):
        """Even a query with no vocab overlap must return k results (not crash)."""
        result = schema_index.retrieve("zzz_unknown_xyz", k=2)
        assert len(result) == 2
        assert all(t in {"customers", "orders"} for t in result)


class TestSchemaIndexDocumentContent:
    def test_document_contains_table_name(self, sample_db):
        """The document built for a table must contain its name."""
        idx = SchemaIndex(embed_fn=_bow)
        engine = connect(sample_db, read_only=False)
        idx.build(engine)
        # We expose _docs for white-box verification
        for doc, name in zip(idx._docs, idx.table_names):
            assert name in doc, f"Table name '{name}' missing from its document: {doc!r}"

    def test_document_contains_column_names(self, sample_db):
        """The document for 'orders' must mention its column names."""
        idx = SchemaIndex(embed_fn=_bow)
        engine = connect(sample_db, read_only=False)
        idx.build(engine)
        docs_by_name = dict(zip(idx.table_names, idx._docs))
        orders_doc = docs_by_name["orders"]
        for col in ("customer_id", "amount", "status"):
            assert col in orders_doc, f"Column '{col}' missing from orders doc: {orders_doc!r}"
