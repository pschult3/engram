"""Tests for graph-based retrieval (expand_with_graph) and Store.neighbors()."""

from __future__ import annotations

import uuid

import pytest

from engram.retrieval.graph import expand_with_graph
from engram.storage import Relation, Store
from engram.storage.models import MemoryType, MemoryUnit


def _unit(store: Store, title: str, body: str = "body") -> MemoryUnit:
    u = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=MemoryType.fact,
        title=title,
        body=body,
    )
    saved, _ = store.upsert_memory(u)
    return saved


def _edge(store: Store, from_unit: MemoryUnit, to_unit: MemoryUnit, weight: float = 1.0) -> None:
    r = Relation(
        project=store.project,
        from_id=from_unit.id,
        to_id=to_unit.id,
        relation_type="co_occurs_in_file",
        weight=weight,
        source="rule:co_file",
    )
    store.upsert_relations([r])


# ---------------------------------------------------------------------------
# Store.neighbors()
# ---------------------------------------------------------------------------


def test_neighbors_direct_forward(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    _edge(store, a, b)

    neighbors = store.neighbors(a.id, depth=1)
    ids = [u.id for u, _ in neighbors]
    assert b.id in ids
    assert a.id not in ids


def test_neighbors_direct_reverse(store: Store):
    """Neighbors should surface units pointing TO the queried unit too."""
    a = _unit(store, "A")
    b = _unit(store, "B")
    _edge(store, a, b)  # edge is A→B

    # querying from B's perspective should return A (reverse direction)
    neighbors = store.neighbors(b.id, depth=1)
    ids = [u.id for u, _ in neighbors]
    assert a.id in ids


def test_neighbors_no_edges(store: Store):
    a = _unit(store, "A")
    _unit(store, "B")  # no edge

    neighbors = store.neighbors(a.id)
    assert neighbors == []


def test_neighbors_returns_weight(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    _edge(store, a, b, weight=0.75)

    neighbors = store.neighbors(a.id)
    b_entry = next((w for u, w in neighbors if u.id == b.id), None)
    assert b_entry is not None
    assert abs(b_entry - 0.75) < 1e-6


def test_neighbors_excludes_expired_units(store: Store):
    from datetime import datetime, timedelta, timezone

    a = _unit(store, "A")
    b = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=MemoryType.fact,
        title="B expired",
        body="body",
        valid_to=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),
    )
    store.upsert_memory(b)
    _edge(store, a, b)

    neighbors = store.neighbors(a.id)
    assert all(u.id != b.id for u, _ in neighbors)


# ---------------------------------------------------------------------------
# expand_with_graph()
# ---------------------------------------------------------------------------


def test_expand_depth1_returns_direct_neighbors(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    c = _unit(store, "C")
    _edge(store, a, b)
    # C is not connected

    neighbors = expand_with_graph(store, [a.id], depth=1, max_extra=10)
    ids = [u.id for u, _ in neighbors]
    assert b.id in ids
    assert c.id not in ids
    assert a.id not in ids  # seed excluded


def test_expand_depth2_reaches_two_hops(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    c = _unit(store, "C")
    _edge(store, a, b)
    _edge(store, b, c)

    neighbors = expand_with_graph(store, [a.id], depth=2, max_extra=10)
    ids = [u.id for u, _ in neighbors]
    assert b.id in ids
    assert c.id in ids


def test_expand_depth2_scores_decay(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    c = _unit(store, "C")
    _edge(store, a, b, weight=1.0)  # hop 1
    _edge(store, b, c, weight=1.0)  # hop 2

    neighbors = expand_with_graph(store, [a.id], depth=2, max_extra=10)
    score_b = next((s for u, s in neighbors if u.id == b.id), None)
    score_c = next((s for u, s in neighbors if u.id == c.id), None)
    assert score_b is not None
    assert score_c is not None
    # B is closer, should score higher
    assert score_b > score_c


def test_expand_respects_max_extra(store: Store):
    a = _unit(store, "seed")
    neighbors_units = [_unit(store, f"neighbor {i}") for i in range(8)]
    for n in neighbors_units:
        _edge(store, a, n)

    result = expand_with_graph(store, [a.id], depth=1, max_extra=3)
    assert len(result) <= 3


def test_expand_no_edges_returns_empty(store: Store):
    a = _unit(store, "A")
    result = expand_with_graph(store, [a.id], depth=1, max_extra=5)
    assert result == []


def test_expand_excludes_seed_ids(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    _edge(store, a, b)
    _edge(store, b, a)

    # Both A and B are seeds — neither should appear in expansion
    result = expand_with_graph(store, [a.id, b.id], depth=1, max_extra=5)
    ids = [u.id for u, _ in result]
    assert a.id not in ids
    assert b.id not in ids


# ---------------------------------------------------------------------------
# get_relations
# ---------------------------------------------------------------------------


def test_get_relations_returns_both_directions(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    _edge(store, a, b)
    _edge(store, b, a)

    rels_a = store.get_relations(a.id)
    from_ids = [r["from_id"] for r in rels_a]
    to_ids = [r["to_id"] for r in rels_a]
    assert a.id in from_ids or a.id in to_ids


# ---------------------------------------------------------------------------
# count_relations
# ---------------------------------------------------------------------------


def test_count_relations_groups_by_type(store: Store):
    a = _unit(store, "A")
    b = _unit(store, "B")
    c = _unit(store, "C")

    store.upsert_relations([
        Relation(project=store.project, from_id=a.id, to_id=b.id,
                 relation_type="co_occurs_in_file", weight=1.0, source="rule:co_file"),
        Relation(project=store.project, from_id=b.id, to_id=c.id,
                 relation_type="references_entity", weight=0.6, source="rule:entity"),
    ])

    counts = store.count_relations()
    assert counts.get("co_occurs_in_file", 0) >= 1
    assert counts.get("references_entity", 0) >= 1
