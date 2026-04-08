"""Drain queued events into the canonical store.

Lives in `ingest/` rather than `storage/` to avoid a circular import:
extraction depends on storage models, and the drain step depends on
extraction.
"""

from __future__ import annotations

from ..storage import EventQueue, Store
from ..storage.models import Event
from .edges import derive_edges_for_unit
from .extractor import extract_units_from_event
from .supersede import supersede_older_units


def drain_queue(store: Store, queue: EventQueue) -> dict[str, int]:
    """Move every queued event into the SQLite store and run extraction.

    After each unit is created, rule-based edges are derived and stored.
    Safe to call from any read-side hook (SessionStart, UserPromptSubmit) —
    if the queue is empty, this is essentially free.
    """
    processed = 0
    units_created = 0
    relations_created = 0
    units_superseded = 0
    for raw in queue.drain():
        event = Event(
            project=raw.get("project") or store.project,
            session_id=raw.get("session_id"),
            type=raw.get("type", "unknown"),
            payload=raw.get("payload") or {},
            created_at=raw.get("created_at") or Store._now_iso(),
        )
        event = store.append_event(event)
        for unit in extract_units_from_event(event):
            _, was_new = store.upsert_memory(unit)
            if was_new:
                units_created += 1
                edges = derive_edges_for_unit(store, unit)
                relations_created += store.upsert_relations(edges)
                units_superseded += supersede_older_units(store, unit)
        if event.id:
            store.mark_processed([event.id])
        processed += 1
    return {
        "events_processed": processed,
        "memory_units_created": units_created,
        "relations_created": relations_created,
        "units_superseded": units_superseded,
    }
