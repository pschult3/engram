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
