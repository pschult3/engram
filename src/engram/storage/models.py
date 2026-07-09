"""Typed memory units and raw events.

Memory units are the canonical durable knowledge. Markdown digests rendered
later are just a derived view over these.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryType(str, Enum):
    fact = "fact"
    decision = "decision"
    preference = "preference"
    task = "task"
    incident = "incident"
    entity_relation = "entity_relation"
    session_summary = "session_summary"
    code_change = "code_change"
    open_question = "open_question"
    lesson = "lesson"


_LESSON_BODY_CAP = 400


class MemoryUnit(BaseModel):
    id: str
    project: str
    type: MemoryType
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    valid_from: str = Field(default_factory=_now)
    valid_to: str | None = None
    confidence: float = 0.8
    checksum: str | None = None

    @field_validator("body")
    @classmethod
    def _cap_lesson_body(cls, v: str, info):
        # Keep lesson bodies concise (CLAUDE.md: 2-3 sentences). Other types
        # (esp. facts with a trailing "Verified: ..." source) stay intact.
        if info.data.get("type") != MemoryType.lesson or len(v) <= _LESSON_BODY_CAP:
            return v
        cut = v[:_LESSON_BODY_CAP]
        boundary = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("; "), cut.rfind("\n"))
        cut = cut[: boundary + 1] if boundary >= int(_LESSON_BODY_CAP * 0.6) else cut.rstrip()
        return cut.rstrip() + " …"


class Event(BaseModel):
    id: str | None = None
    project: str
    session_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now)
    processed: bool = False


class Relation(BaseModel):
    """Directed edge between two memory units."""

    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    project: str
    from_id: str
    to_id: str
    relation_type: str  # co_occurs_in_file | temporal_follows | references_entity
    weight: float = 1.0
    source: str  # rule:co_file | rule:temporal | rule:entity
    created_at: str = Field(default_factory=_now)
