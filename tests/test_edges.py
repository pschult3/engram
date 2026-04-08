"""Tests for rule-based edge extraction."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from engram.ingest.edges import derive_edges_for_unit
from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


def _unit(
    store: Store,
    type_: MemoryType,
    title: str,
    body: str,
    file_paths: list[str] | None = None,
    tags: list[str] | None = None,
    created_at: str | None = None,
) -> MemoryUnit:
    kwargs: dict = {}
    if created_at:
        kwargs["created_at"] = created_at
        kwargs["valid_from"] = created_at
    u = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=type_,
        title=title,
        body=body,
        file_paths=file_paths or [],
        tags=tags or [],
        **kwargs,
    )
    saved, _ = store.upsert_memory(u)
    return saved


# ---------------------------------------------------------------------------
# co_occurs_in_file
# ---------------------------------------------------------------------------


def test_co_occurs_in_file_creates_edge(store: Store):
    a = _unit(store, MemoryType.decision, "decision A", "body A", file_paths=["src/auth.py"])
    b = _unit(store, MemoryType.fact, "fact B", "body B", file_paths=["src/auth.py"])

    edges = derive_edges_for_unit(store, b)
    types = [e.relation_type for e in edges]
    from_to = [(e.from_id, e.to_id) for e in edges]

    assert "co_occurs_in_file" in types
    assert (b.id, a.id) in from_to


def test_co_occurs_in_file_no_edge_without_overlap(store: Store):
    _unit(store, MemoryType.decision, "decision A", "body A", file_paths=["src/auth.py"])
    b = _unit(store, MemoryType.fact, "fact B", "body B", file_paths=["src/other.py"])

    edges = derive_edges_for_unit(store, b)
    assert not any(e.relation_type == "co_occurs_in_file" for e in edges)


def test_co_occurs_in_file_no_edge_without_file_paths(store: Store):
    _unit(store, MemoryType.decision, "decision A", "body A", file_paths=["src/auth.py"])
    b = _unit(store, MemoryType.fact, "fact B", "body B")  # no file_paths

    edges = derive_edges_for_unit(store, b)
    assert not any(e.relation_type == "co_occurs_in_file" for e in edges)


# ---------------------------------------------------------------------------
# temporal_follows
# ---------------------------------------------------------------------------


def test_temporal_follows_incident_after_code_change(store: Store):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat(timespec="seconds")

    cc = _unit(
        store,
        MemoryType.code_change,
        "edit auth.py",
        "changed auth middleware",
        file_paths=["src/auth.py"],
        created_at=recent,
    )
    incident = _unit(
        store,
        MemoryType.incident,
        "auth crash",
        "app crashed after auth change",
        file_paths=["src/auth.py"],
    )

    edges = derive_edges_for_unit(store, incident)
    temporal = [e for e in edges if e.relation_type == "temporal_follows"]
    assert len(temporal) >= 1
    assert temporal[0].from_id == cc.id
    assert temporal[0].to_id == incident.id


def test_temporal_follows_no_edge_for_old_code_change(store: Store):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat(timespec="seconds")

    _unit(
        store,
        MemoryType.code_change,
        "old edit",
        "changed auth long ago",
        file_paths=["src/auth.py"],
        created_at=old,
    )
    incident = _unit(
        store,
        MemoryType.incident,
        "auth crash",
        "crashed recently",
        file_paths=["src/auth.py"],
    )

    edges = derive_edges_for_unit(store, incident)
    assert not any(e.relation_type == "temporal_follows" for e in edges)


def test_temporal_follows_not_created_for_non_incident(store: Store):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=5)).isoformat(timespec="seconds")

    _unit(
        store,
        MemoryType.code_change,
        "edit foo.py",
        "body",
        file_paths=["foo.py"],
        created_at=recent,
    )
    # fact type — should not trigger temporal_follows rule
    fact = _unit(store, MemoryType.fact, "fact about foo", "body", file_paths=["foo.py"])

    edges = derive_edges_for_unit(store, fact)
    assert not any(e.relation_type == "temporal_follows" for e in edges)


# ---------------------------------------------------------------------------
# references_entity (tags)
# ---------------------------------------------------------------------------


def test_references_entity_creates_edge(store: Store):
    a = _unit(store, MemoryType.decision, "decision A", "body", tags=["jwt", "auth"])
    b = _unit(store, MemoryType.fact, "fact B", "body", tags=["jwt", "performance"])

    edges = derive_edges_for_unit(store, b)
    entity_edges = [e for e in edges if e.relation_type == "references_entity"]
    assert any(e.to_id == a.id for e in entity_edges)


def test_references_entity_no_edge_without_tag_overlap(store: Store):
    _unit(store, MemoryType.decision, "decision A", "body", tags=["pnpm"])
    b = _unit(store, MemoryType.fact, "fact B", "body", tags=["docker"])

    edges = derive_edges_for_unit(store, b)
    assert not any(e.relation_type == "references_entity" for e in edges)


# ---------------------------------------------------------------------------
# No spurious edges for unrelated units
# ---------------------------------------------------------------------------


def test_no_edges_for_completely_unrelated_units(store: Store):
    _unit(store, MemoryType.fact, "fact A", "body A")
    b = _unit(store, MemoryType.fact, "fact B", "body B")

    edges = derive_edges_for_unit(store, b)
    assert edges == []


# ---------------------------------------------------------------------------
# Store upsert_relations deduplication
# ---------------------------------------------------------------------------


def test_upsert_relations_deduplicates(store: Store):
    a = _unit(store, MemoryType.fact, "fact A", "body", file_paths=["x.py"])
    b = _unit(store, MemoryType.fact, "fact B", "body", file_paths=["x.py"])

    edges = derive_edges_for_unit(store, b)
    n1 = store.upsert_relations(edges)
    n2 = store.upsert_relations(edges)  # same edges again

    assert n1 >= 1
    assert n2 == 0  # all duplicates
