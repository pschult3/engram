"""Tests for the added dedup (fact/lesson text-similarity supersede),
lesson body cap, and distinct feedback titles."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from engram.hooks.handlers import _feedback_title
from engram.ingest.supersede import supersede_older_units
from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


def _put(store: Store, type_, title, body, tags=None, minutes_ago=0) -> MemoryUnit:
    created = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat(timespec="seconds")
    unit = MemoryUnit(
        id=uuid.uuid4().hex[:12], project=store.project, type=type_,
        title=title, body=body, tags=tags or [],
        created_at=created, valid_from=created,
    )
    saved, _ = store.upsert_memory(unit)
    return saved


# ---- #2 fact/lesson text-similarity supersede ----

def test_supersedes_near_duplicate_fact(store: Store) -> None:
    _put(store, MemoryType.fact, "vtweg 10 Geraete",
         "vtweg 10 ist Geraete. Verified: BQ query.", tags=["vtweg"], minutes_ago=60)
    new = _put(store, MemoryType.fact, "vtweg 10 Geraete Vertrieb",
               "vtweg 10 ist Geraete im Vertrieb. Verified: BQ query.", tags=["vtweg"])
    assert supersede_older_units(store, new) == 1
    active = store.list_memory(types=[MemoryType.fact])
    assert [u.id for u in active] == [new.id]


def test_does_not_supersede_dissimilar_facts(store: Store) -> None:
    old = _put(store, MemoryType.fact, "Umsatz",
               "Umsatz 2023 war 5 Mio. Verified: BQ.", tags=["finance"], minutes_ago=60)
    new = _put(store, MemoryType.fact, "Kosten",
               "Kosten 2024 stiegen stark. Verified: BQ.", tags=["finance"])
    assert supersede_older_units(store, new) == 0
    ids = {u.id for u in store.list_memory(types=[MemoryType.fact])}
    assert old.id in ids and new.id in ids


# ---- #3 lesson body cap (facts stay intact) ----

def test_lesson_body_is_capped() -> None:
    u = MemoryUnit(id="x" * 12, project="p", type=MemoryType.lesson,
                   title="t", body="Ein sehr langer Satz. " * 40)
    assert len(u.body) <= 402
    assert u.body.endswith("…")


def test_fact_body_not_capped() -> None:
    long_body = "Detail. " * 100 + "Verified: BQ."
    u = MemoryUnit(id="y" * 12, project="p", type=MemoryType.fact,
                   title="t", body=long_body)
    assert u.body == long_body


# ---- #1 distinct feedback titles ----

def test_feedback_title_is_distinct() -> None:
    a = _feedback_title("- Missing: KST mapping context\n- Helpful: x")
    b = _feedback_title("- Suggested: add vtweg semantics to AGENTS.md")
    assert a != b
    assert a.startswith("feedback: ") and "KST mapping" in a


def test_feedback_title_empty_fallback() -> None:
    assert _feedback_title("") == "compact feedback"
