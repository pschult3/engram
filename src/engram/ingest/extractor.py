"""Deterministic event → memory unit extraction.

V1 deliberately avoids LLM calls. The point of the architecture is that most
useful coding events are already structured (file edits, test runs, errors)
and can be persisted directly. An LLM-backed curator can be added later
without changing the storage layer.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from ..storage.models import Event, MemoryType, MemoryUnit


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def extract_units_from_event(event: Event) -> list[MemoryUnit]:
    """Map a single raw event into 0..N candidate memory units."""
    t = event.type
    p = event.payload or {}

    if t == "file_edit":
        path = p.get("path", "<unknown>")
        summary = p.get("summary") or "edited"
        return [
            MemoryUnit(
                id=_uid(),
                project=event.project,
                type=MemoryType.code_change,
                title=f"edit: {path}",
                body=summary,
                file_paths=[path],
                source_refs=[f"event:{event.id}"],
                tags=["code_change"],
            )
        ]

    if t == "test_failure":
        name = p.get("name") or "tests"
        msg = p.get("message") or ""
        return [
            MemoryUnit(
                id=_uid(),
                project=event.project,
                type=MemoryType.incident,
                title=f"test failure: {name}",
                body=msg,
                source_refs=[f"event:{event.id}"],
                tags=["test", "incident"],
                confidence=0.7,
            )
        ]

    if t == "decision":
        return [
            MemoryUnit(
                id=_uid(),
                project=event.project,
                type=MemoryType.decision,
                title=p.get("title", "decision"),
                body=p.get("body", ""),
                tags=p.get("tags", []) or ["decision"],
                source_refs=[f"event:{event.id}"],
                confidence=0.9,
            )
        ]

    if t == "open_question":
        return [
            MemoryUnit(
                id=_uid(),
                project=event.project,
                type=MemoryType.open_question,
                title=p.get("title", "open question"),
                body=p.get("body", ""),
                tags=["open_question"],
                source_refs=[f"event:{event.id}"],
                confidence=0.6,
            )
        ]

    if t == "command":
        # Only failed commands become durable memory; "ok" commands stay as
        # raw events for telemetry but don't pollute retrieval.
        if p.get("status") != "fail":
            return []
        cmd = p.get("command") or "command"
        return [
            MemoryUnit(
                id=_uid(),
                project=event.project,
                type=MemoryType.incident,
                title=f"command failed: {cmd[:60]}",
                body=cmd,
                source_refs=[f"event:{event.id}"],
                tags=["command", "incident"],
                confidence=0.6,
            )
        ]

    if t == "fact":
        return [
            MemoryUnit(
                id=_uid(),
                project=event.project,
                type=MemoryType.fact,
                title=p.get("title", "fact"),
                body=p.get("body", ""),
                tags=p.get("tags", []) or ["fact"],
                source_refs=[f"event:{event.id}"],
            )
        ]

    return []


def summarize_session(
    project: str,
    session_id: str,
    events: Iterable[Event],
    compact_summary: str | None = None,
) -> MemoryUnit | None:
    """Build a single session_summary unit from raw events.

    If Claude Code passed us a compact_summary (from PostCompact), we keep it
    verbatim — it is already a good condensed view. Otherwise we fall back to
    a deterministic event-count summary.
    """
    events = list(events)
    if not events and not compact_summary:
        return None

    if compact_summary:
        body = compact_summary.strip()
    else:
        counts: dict[str, int] = {}
        for e in events:
            counts[e.type] = counts.get(e.type, 0) + 1
        body = "Session events: " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())
        )

    return MemoryUnit(
        id=_uid(),
        project=project,
        type=MemoryType.session_summary,
        title=f"session {session_id[:8]}",
        body=body,
        source_refs=[f"session:{session_id}"],
        tags=["session"],
        confidence=0.7,
    )
