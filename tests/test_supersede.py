"""Tests for the supersession logic (active invalidation of older units)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from engram.ingest.supersede import supersede_older_units, _MIN_SHARED_FILES, _MIN_SHARED_TAGS
from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


def _make_unit(
    store: Store,
    type_: MemoryType,
    title: str,
    tags: list[str] | None = None,
    file_paths: list[str] | None = None,
    minutes_ago: int = 0,
) -> MemoryUnit:
    created_at = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat(timespec="seconds")
    unit = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=type_,
        title=title,
        body=f"body of {title}",
        tags=tags or [],
        file_paths=file_paths or [],
        created_at=created_at,
        valid_from=created_at,
    )
    saved, _ = store.upsert_memory(unit)
    return saved


# ---------------------------------------------------------------------------
# Happy path — supersession fires
# ---------------------------------------------------------------------------


def test_supersedes_by_shared_files(store: Store) -> None:
    files = ["src/auth.py", "src/token.py"]
    old = _make_unit(store, MemoryType.decision, "use JWT", file_paths=files, minutes_ago=60)
    new = _make_unit(store, MemoryType.decision, "use session cookies", file_paths=files)

    count = supersede_older_units(store, new)

    assert count == 1
    # Old unit must now be invalid.
    reloaded = store.get_memory(old.id)
    assert reloaded is not None
    assert reloaded.valid_to is not None

    # New unit still active.
    active = store.list_memory(types=[MemoryType.decision])
    assert any(u.id == new.id for u in active)
    assert all(u.id != old.id for u in active)


def test_supersedes_by_shared_tags(store: Store) -> None:
    tags = ["auth", "jwt", "session"]
    old = _make_unit(store, MemoryType.preference, "prefer stateless", tags=tags, minutes_ago=30)
    new = _make_unit(store, MemoryType.preference, "prefer sessions", tags=tags)

    count = supersede_older_units(store, new)

    assert count == 1
    reloaded = store.get_memory(old.id)
    assert reloaded is not None
    assert reloaded.valid_to is not None


def test_supersedes_open_question_resolved_by_new_decision(store: Store) -> None:
    tags = ["db", "migration", "postgres"]
    q = _make_unit(store, MemoryType.open_question, "which DB?", tags=tags, minutes_ago=10)
    # A new open_question with the same tags can supersede the old one.
    new_q = _make_unit(store, MemoryType.open_question, "postgres confirmed?", tags=tags)

    count = supersede_older_units(store, new_q)
    assert count == 1
    reloaded = store.get_memory(q.id)
    assert reloaded is not None
    assert reloaded.valid_to is not None


# ---------------------------------------------------------------------------
# Conservative threshold — does NOT fire
# ---------------------------------------------------------------------------


def test_does_not_supersede_with_one_shared_file(store: Store) -> None:
    """Sharing only 1 file is below the threshold — keep both active."""
    old = _make_unit(
        store, MemoryType.decision, "old decision",
        file_paths=["src/auth.py", "src/other.py"],
        minutes_ago=60,
    )
    new = _make_unit(
        store, MemoryType.decision, "new decision",
        file_paths=["src/auth.py", "src/completely_different.py"],
    )

    count = supersede_older_units(store, new)

    assert count == 0
    # Both still active.
    active = store.list_memory(types=[MemoryType.decision])
    ids = {u.id for u in active}
    assert old.id in ids
    assert new.id in ids


def test_does_not_supersede_with_two_shared_tags(store: Store) -> None:
    """Sharing only 2 tags is below the threshold — keep both active."""
    old = _make_unit(store, MemoryType.preference, "old pref", tags=["auth", "jwt", "x"], minutes_ago=20)
    new = _make_unit(store, MemoryType.preference, "new pref", tags=["auth", "jwt", "y"])

    count = supersede_older_units(store, new)

    assert count == 0


def test_does_not_supersede_disjoint_units(store: Store) -> None:
    old = _make_unit(
        store, MemoryType.decision, "frontend decision",
        tags=["react", "css"], file_paths=["src/ui.tsx"],
        minutes_ago=60,
    )
    new = _make_unit(
        store, MemoryType.decision, "backend decision",
        tags=["postgres", "redis"], file_paths=["src/db.py"],
    )

    count = supersede_older_units(store, new)
    assert count == 0


# ---------------------------------------------------------------------------
# Non-supersedable types — never fires
# ---------------------------------------------------------------------------


def test_fact_type_never_superseded(store: Store) -> None:
    files = ["src/auth.py", "src/token.py"]
    _make_unit(store, MemoryType.fact, "fact A", file_paths=files, minutes_ago=60)
    new = _make_unit(store, MemoryType.fact, "fact B", file_paths=files)

    count = supersede_older_units(store, new)
    assert count == 0


def test_newer_does_not_supersede_older_with_future_timestamp(store: Store) -> None:
    """A unit cannot supersede one with a later created_at."""
    files = ["src/auth.py", "src/token.py"]
    # 'old' was created later (future-ish), so it should NOT be superseded.
    future_ts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    newer = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=MemoryType.decision,
        title="future decision",
        body="from the future",
        file_paths=files,
        created_at=future_ts,
        valid_from=future_ts,
    )
    store.upsert_memory(newer)
    new = _make_unit(store, MemoryType.decision, "current decision", file_paths=files)

    count = supersede_older_units(store, new)
    assert count == 0


# ---------------------------------------------------------------------------
# Relation edge is recorded
# ---------------------------------------------------------------------------


def test_supersedes_relation_edge_created(store: Store) -> None:
    files = ["src/auth.py", "src/session.py"]
    old = _make_unit(store, MemoryType.decision, "old way", file_paths=files, minutes_ago=60)
    new = _make_unit(store, MemoryType.decision, "new way", file_paths=files)

    supersede_older_units(store, new)

    relations = store.get_relations(new.id)
    supersedes = [r for r in relations if r["relation_type"] == "supersedes"]
    assert len(supersedes) == 1
    assert supersedes[0]["from_id"] == new.id
    assert supersedes[0]["to_id"] == old.id
