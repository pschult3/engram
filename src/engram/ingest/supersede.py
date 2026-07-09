"""Invalidate older memory units when a newer one supersedes them.

Conservative policy — err on the side of keeping units active:
  - Overlap types (decision, preference, open_question) are superseded when the
    new unit shares >= 2 file_paths OR >= 3 tags with an older one.
  - Text types (fact, lesson) rarely share files/tags, so they are superseded
    on title+body token similarity (Jaccard >= 0.6, and a shared tag unless the
    texts are near-identical). This catches reworded / EN-DE duplicate facts.
  - Sets valid_to = new_unit.created_at on superseded units (never deletes).
  - Records a 'supersedes' relation edge so the history is traceable.
"""

from __future__ import annotations

import logging
import re

from ..storage import Store
from ..storage.models import MemoryType, Relation, MemoryUnit

log = logging.getLogger("engram.supersede")

_SUPERSEDABLE = frozenset({
    MemoryType.decision,
    MemoryType.preference,
    MemoryType.open_question,
    MemoryType.fact,
    MemoryType.lesson,
})

# Types deduped by text similarity instead of file/tag overlap.
_TEXT_SIM_TYPES = frozenset({MemoryType.fact, MemoryType.lesson})

# Conservative overlap thresholds.
_MIN_SHARED_FILES = 2
_MIN_SHARED_TAGS = 3

# Text-similarity thresholds for _TEXT_SIM_TYPES.
_MIN_TEXT_SIM = 0.6
_HIGH_TEXT_SIM = 0.8

_WORD = re.compile(r"[a-z0-9äöüß]+")


def _tokens(unit: MemoryUnit) -> set[str]:
    return set(_WORD.findall(f"{unit.title} {unit.body}".lower()))


def _text_sim(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def supersede_older_units(store: Store, new_unit: MemoryUnit) -> int:
    """Invalidate active units that *new_unit* supersedes.

    Returns the count of units invalidated.
    Safe to call for any unit type — non-supersedable types return 0 immediately.
    """
    if new_unit.type not in _SUPERSEDABLE:
        return 0

    candidates = store.list_memory(types=[new_unit.type], limit=100)
    # Only consider units strictly older than the new one.
    candidates = [
        u for u in candidates
        if u.id != new_unit.id and u.created_at < new_unit.created_at
    ]
    if not candidates:
        return 0

    new_files = set(new_unit.file_paths)
    new_tags = set(new_unit.tags)
    text_mode = new_unit.type in _TEXT_SIM_TYPES
    new_tokens = _tokens(new_unit) if text_mode else set()
    invalidated = 0

    for candidate in candidates:
        shared_tags = len(new_tags & set(candidate.tags))
        if text_mode:
            sim = _text_sim(new_tokens, _tokens(candidate))
            if sim < _MIN_TEXT_SIM:
                continue
            # Require a shared tag unless the texts are near-identical.
            if shared_tags < 1 and sim < _HIGH_TEXT_SIM:
                continue
            reason = f"text_sim={sim:.2f}, shared_tags={shared_tags}"
        else:
            shared_files = len(new_files & set(candidate.file_paths))
            if shared_files < _MIN_SHARED_FILES and shared_tags < _MIN_SHARED_TAGS:
                continue
            reason = f"shared_files={shared_files}, shared_tags={shared_tags}"

        store.invalidate_memory(candidate.id, new_unit.created_at)
        store.upsert_relations([
            Relation(
                project=store.project,
                from_id=new_unit.id,
                to_id=candidate.id,
                relation_type="supersedes",
                weight=1.0,
                source="rule:supersede",
            )
        ])
        log.debug(
            "Unit %s supersedes %s (%s)",
            new_unit.id[:8],
            candidate.id[:8],
            reason,
        )
        invalidated += 1

    return invalidated
