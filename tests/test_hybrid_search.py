"""Tests for hybrid search: RRF fusion and graceful vec degradation."""

from __future__ import annotations

import uuid

import pytest

from engram.retrieval.embeddings import reset_embedder
from engram.retrieval.search import _rrf_fuse, search_memory
from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


def _unit(store: Store, type_: MemoryType, title: str, body: str, tags: list[str] | None = None) -> MemoryUnit:
    u = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=type_,
        title=title,
        body=body,
        tags=tags or [],
    )
    saved, _ = store.upsert_memory(u)
    return saved


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------


def test_rrf_fuse_single_list():
    rankings = [[("a", 1.0), ("b", 0.8), ("c", 0.5)]]
    scores = _rrf_fuse(rankings)
    # a is rank 0, b rank 1, c rank 2
    assert scores["a"] > scores["b"] > scores["c"]


def test_rrf_fuse_boost_for_overlap():
    # "x" appears in both lists; "a" only in first; "b" only in second
    r1 = [("x", 0.9), ("a", 0.5)]
    r2 = [("x", 0.8), ("b", 0.5)]
    scores = _rrf_fuse([r1, r2])
    # x should be top-ranked because it appears in both
    assert scores["x"] > scores["a"]
    assert scores["x"] > scores["b"]


def test_rrf_fuse_empty_lists():
    assert _rrf_fuse([]) == {}
    assert _rrf_fuse([[]]) == {}


def test_rrf_fuse_k_parameter():
    rankings = [[("a", 1.0)]]
    scores_k60 = _rrf_fuse(rankings, k=60)
    scores_k1 = _rrf_fuse(rankings, k=1)
    # lower k means higher weight for top ranks
    assert scores_k1["a"] > scores_k60["a"]


# ---------------------------------------------------------------------------
# search_memory — FTS5 fallback (vec disabled)
# ---------------------------------------------------------------------------


def test_search_memory_fts_only(store: Store):
    """search_memory works correctly without vec."""
    _unit(store, MemoryType.decision, "use pnpm", "standardized on pnpm workspaces")
    _unit(store, MemoryType.fact, "node version", "CI pinned to node 20")

    hits = search_memory(store, "pnpm", top_k=5)
    assert len(hits) >= 1
    assert any("pnpm" in u.title for u, _ in hits)


def test_search_memory_returns_empty_on_no_match(store: Store):
    _unit(store, MemoryType.fact, "database", "we use postgres")
    hits = search_memory(store, "kubernetes", top_k=5)
    assert hits == []


def test_search_memory_respects_type_weights(store: Store):
    """decision (weight 1.0) should outrank code_change (weight 0.4) for same query."""
    _unit(store, MemoryType.code_change, "edit pnpm file", "changed pnpm lockfile")
    _unit(store, MemoryType.decision, "pnpm decision", "decided to use pnpm workspaces")

    hits = search_memory(store, "pnpm", top_k=5)
    types = [u.type for u, _ in hits]
    # decision should appear before code_change
    assert MemoryType.decision in types
    if MemoryType.code_change in types:
        assert types.index(MemoryType.decision) < types.index(MemoryType.code_change)


# ---------------------------------------------------------------------------
# search_memory — with stub vec
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_store(tmp_config, monkeypatch):
    """Store opened with stub embedder enabled."""
    monkeypatch.setenv("ENGRAM_EMBEDDER", "stub")
    reset_embedder()
    # Reopen store — vec table creation depends on extension availability.
    # Since sqlite-vec may not be installed, just test graceful degradation.
    from engram.storage import open_store
    s = open_store(tmp_config.db_path, tmp_config.project)
    yield s
    s.close()
    reset_embedder()


def test_search_memory_with_stub_vec_returns_results(stub_store: Store):
    """search_memory degrades safely whether or not vec extension is present."""
    _unit(stub_store, MemoryType.fact, "auth flow", "JWT-based auth with refresh tokens")
    hits = search_memory(stub_store, "auth", top_k=5)
    # At minimum FTS5 should find the unit
    assert len(hits) >= 1


def test_search_memory_top_k_respected(store: Store):
    for i in range(10):
        _unit(store, MemoryType.fact, f"fact {i} pnpm", f"body about pnpm number {i}")
    hits = search_memory(store, "pnpm", top_k=3)
    assert len(hits) <= 3
