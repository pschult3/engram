"""Invalidate older memory units when a newer one supersedes them.

Conservative policy — err on the side of keeping units active:
  - Only applies to: decision, preference, open_question
  - A candidate is superseded when the new unit shares
      >= 2 file_paths  OR  >= 3 tags  with it
  - Sets valid_to = new_unit.created_at on superseded units (never deletes)
  - Records a 'supersedes' relation edge so the history is traceable
"""

from __future__ import annotations

import logging

from ..storage import Store
from ..storage.models import MemoryType, Relation, MemoryUnit

log = logging.getLogger("engram.supersede")

_SUPERSEDABLE = frozenset({
    MemoryType.decision,
    MemoryType.preference,
    MemoryType.open_question,
})

# Conservative overlap thresholds.
_MIN_SHARED_FILES = 2
_MIN_SHARED_TAGS = 3


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
    invalidated = 0

    for candidate in candidates:
        shared_files = len(new_files & set(candidate.file_paths))
        shared_tags = len(new_tags & set(candidate.tags))

        if shared_files < _MIN_SHARED_FILES and shared_tags < _MIN_SHARED_TAGS:
            continue

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
            "Unit %s supersedes %s (shared_files=%d, shared_tags=%d)",
            new_unit.id[:8],
            candidate.id[:8],
            shared_files,
            shared_tags,
        )
        invalidated += 1

    return invalidated
