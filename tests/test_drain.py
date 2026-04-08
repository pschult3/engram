from __future__ import annotations

from engram.ingest import drain_queue
from engram.storage import EventQueue, Store
from engram.storage.models import MemoryType


def test_drain_creates_memory_units(store: Store, queue: EventQueue):
    queue.append(
        {
            "project": store.project,
            "type": "file_edit",
            "payload": {"path": "src/auth/login.ts", "summary": "Edit on login"},
            "created_at": Store._now_iso(),
        }
    )
    stats = drain_queue(store, queue)
    assert stats["events_processed"] == 1
    assert stats["memory_units_created"] == 1
    units = store.list_memory(types=[MemoryType.code_change])
    assert len(units) == 1
    assert "login.ts" in units[0].title
