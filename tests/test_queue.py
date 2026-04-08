from __future__ import annotations

from engram.storage import EventQueue


def test_append_and_drain_roundtrip(queue: EventQueue):
    queue.append({"type": "file_edit", "payload": {"path": "a.py"}})
    queue.append({"type": "file_edit", "payload": {"path": "b.py"}})
    drained = list(queue.drain())
    assert [e["payload"]["path"] for e in drained] == ["a.py", "b.py"]
    # File is gone after drain.
    assert not queue.path.exists()


def test_drain_empty_queue_is_safe(queue: EventQueue):
    assert list(queue.drain()) == []


def test_corrupt_lines_are_skipped(queue: EventQueue):
    queue.append({"type": "ok", "payload": {}})
    with open(queue.path, "a", encoding="utf-8") as f:
        f.write("not json\n")
    queue.append({"type": "ok2", "payload": {}})
    drained = list(queue.drain())
    assert [e["type"] for e in drained] == ["ok", "ok2"]


def test_pending_count(queue: EventQueue):
    assert queue.pending_count() == 0
    queue.append({"type": "x", "payload": {}})
    queue.append({"type": "x", "payload": {}})
    assert queue.pending_count() == 2
