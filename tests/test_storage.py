from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


def _unit(store: Store, type_: MemoryType, title: str, body: str) -> MemoryUnit:
    u = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=type_,
        title=title,
        body=body,
    )
    saved, _ = store.upsert_memory(u)
    return saved


def test_upsert_dedupes_by_checksum(store: Store):
    a = _unit(store, MemoryType.fact, "auth uses pnpm", "we use pnpm workspaces")
    _, was_new = store.upsert_memory(
        MemoryUnit(
            id="y",
            project=store.project,
            type=MemoryType.fact,
            title="auth uses pnpm",
            body="we use pnpm workspaces",
        )
    )
    assert was_new is False
    assert len(store.list_memory(types=[MemoryType.fact])) == 1
    assert a.id


def test_search_finds_by_keyword(store: Store):
    _unit(store, MemoryType.decision, "use pnpm workspaces", "decided to standardize on pnpm")
    _unit(store, MemoryType.fact, "tests run on node 20", "CI pinned to node 20")
    hits = store.search("pnpm", top_k=5)
    assert len(hits) >= 1
    assert any("pnpm" in u.title for u, _ in hits)


def test_code_change_gets_ttl(store: Store):
    u = _unit(store, MemoryType.code_change, "edit: foo.py", "Edit on foo.py")
    assert u.valid_to is not None
    expires = datetime.fromisoformat(u.valid_to)
    now = datetime.now(timezone.utc)
    # default TTL is 14 days; allow a generous window
    assert expires - now > timedelta(days=13)
    assert expires - now < timedelta(days=15)


def test_expired_units_hidden_from_listing(store: Store):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    expired = MemoryUnit(
        id="exp",
        project=store.project,
        type=MemoryType.code_change,
        title="old change",
        body="ancient",
        valid_to=past,
    )
    store.upsert_memory(expired)
    active = store.list_memory(types=[MemoryType.code_change])
    assert all(u.title != "old change" for u in active)


def test_search_log(store: Store):
    store.log_search("hello world", 5, ["a", "b"])
    rows = store.conn.execute("SELECT query, top_k, hit_ids FROM search_log").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "hello world"


# ---------------------------------------------------------------------------
# Session-summary idempotency
# ---------------------------------------------------------------------------


def _session_summary(store: Store, session_id: str, body: str = None) -> MemoryUnit:
    u = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=MemoryType.session_summary,
        title=f"session {session_id[:8]}",
        body=body or f"Summary for session {session_id} with enough content to pass quality gate.",
        source_refs=[f"session:{session_id}"],
        tags=["session"],
    )
    store.upsert_memory(u)
    return u


def test_session_summary_idempotency_retires_prior(store: Store):
    sid = "idem-session-001"
    # Different bodies so checksum dedup doesn't fire before idempotency block.
    u1 = _session_summary(store, sid, "First compact summary for this session with unique content A.")
    u2 = _session_summary(store, sid, "Second compact summary for this session with unique content B.")

    active = store.list_memory(types=[MemoryType.session_summary])
    assert len(active) == 1
    assert active[0].id == u2.id

    row = store.conn.execute(
        "SELECT valid_to FROM memory_units WHERE id=?", [u1.id]
    ).fetchone()
    assert row is not None and row[0] is not None


def test_session_summary_different_sessions_both_active(store: Store):
    _session_summary(store, "sess-aaa")
    _session_summary(store, "sess-bbb")
    active = store.list_memory(types=[MemoryType.session_summary])
    assert len(active) == 2


def test_fact_not_retired_by_session_idempotency(store: Store):
    sid = "idem-session-002"
    for i in range(2):
        u = MemoryUnit(
            id=uuid.uuid4().hex[:12],
            project=store.project,
            type=MemoryType.fact,
            title=f"fact {i}",
            body=f"body {i}",
            source_refs=[f"session:{sid}"],
            tags=["fact"],
        )
        store.upsert_memory(u)
    active = store.list_memory(types=[MemoryType.fact])
    assert len(active) == 2
