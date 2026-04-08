"""Tests for the embedding backend.

All tests run in stub mode (ENGRAM_EMBEDDER=stub) so no ML library is needed.
Stub mode produces deterministic, normalized 384-dim float vectors.
"""

from __future__ import annotations

import os
import struct

import pytest

from engram.retrieval.embeddings import (
    EMBED_DIM,
    Embedder,
    _stub_embed,
    get_embedder,
    reset_embedder,
)


@pytest.fixture(autouse=True)
def _use_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAM_EMBEDDER", "stub")
    reset_embedder()
    yield
    reset_embedder()


# ---------------------------------------------------------------------------
# Stub embed function
# ---------------------------------------------------------------------------


def test_stub_embed_returns_correct_dim():
    vec = _stub_embed("hello world")
    assert len(vec) == EMBED_DIM


def test_stub_embed_is_deterministic():
    a = _stub_embed("some text")
    b = _stub_embed("some text")
    assert a == b


def test_stub_embed_differs_for_different_inputs():
    a = _stub_embed("auth middleware")
    b = _stub_embed("database migration")
    assert a != b


def test_stub_embed_is_normalized():
    vec = _stub_embed("unit test")
    magnitude = sum(x * x for x in vec) ** 0.5
    assert abs(magnitude - 1.0) < 1e-5


# ---------------------------------------------------------------------------
# Embedder class
# ---------------------------------------------------------------------------


def test_embedder_create_stub():
    embedder = Embedder.create()
    assert embedder is not None


def test_embedder_embed_one_returns_correct_dim():
    embedder = Embedder.create()
    assert embedder is not None
    vec = embedder.embed_one("test query")
    assert len(vec) == EMBED_DIM


def test_embedder_embed_many():
    embedder = Embedder.create()
    assert embedder is not None
    vecs = embedder.embed_many(["first", "second", "third"])
    assert len(vecs) == 3
    assert all(len(v) == EMBED_DIM for v in vecs)
    # different inputs → different vecs
    assert vecs[0] != vecs[1]


def test_embedder_model_name_stub():
    embedder = Embedder.create()
    assert embedder is not None
    assert embedder.model_name == "stub"


def test_embedder_serialize_produces_correct_bytes():
    embedder = Embedder.create()
    assert embedder is not None
    vec = embedder.embed_one("serialize me")
    blob = embedder.serialize(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == EMBED_DIM * 4  # 4 bytes per float32
    # Round-trip: deserialize and compare
    recovered = list(struct.unpack(f"<{EMBED_DIM}f", blob))
    assert len(recovered) == EMBED_DIM
    assert abs(recovered[0] - vec[0]) < 1e-6


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_get_embedder_returns_stub():
    embedder = get_embedder()
    assert embedder is not None
    assert embedder.model_name == "stub"


def test_get_embedder_is_singleton():
    e1 = get_embedder()
    e2 = get_embedder()
    assert e1 is e2


def test_embedder_none_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENGRAM_EMBEDDER", "none")
    reset_embedder()
    embedder = get_embedder()
    assert embedder is None
