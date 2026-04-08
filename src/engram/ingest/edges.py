"""Rule-based edge extraction between memory units.

Three deterministic rules (v1):
  co_occurs_in_file  — two units share at least one file path
  temporal_follows   — incident created ≤30 min after a code_change on the same file
  references_entity  — two units share at least one tag

All rules produce directed Relation objects.  For symmetric rules
(co_occurs_in_file, references_entity) only the A→B direction is created here;
the reverse B→A edge is created when B is processed.  The UNIQUE index on
(project, from_id, to_id, relation_type) prevents double-insertion.
"""

from __future__ import annotations

from ..storage import Store
from ..storage.models import MemoryUnit, Relation


def derive_edges_for_unit(
    store: Store,
    unit: MemoryUnit,
) -> list[Relation]:
    """Return candidate Relation objects between *unit* and existing units.

    Callers should pass the result to store.upsert_relations().
    """
    edges: list[Relation] = []

    # Rule 1: co_occurs_in_file
    if unit.file_paths:
        for other in store.units_sharing_files(unit.file_paths, exclude_id=unit.id):
            edges.append(
                Relation(
                    project=unit.project,
                    from_id=unit.id,
                    to_id=other.id,
                    relation_type="co_occurs_in_file",
                    weight=1.0,
                    source="rule:co_file",
                )
            )

    # Rule 2: temporal_follows — incident after a code_change on the same file (≤30 min)
    if unit.type.value == "incident" and unit.file_paths:
        for cc in store.recent_code_changes_on_files(
            unit.file_paths, minutes=30, exclude_id=unit.id
        ):
            edges.append(
                Relation(
                    project=unit.project,
                    from_id=cc.id,
                    to_id=unit.id,
                    relation_type="temporal_follows",
                    weight=0.9,
                    source="rule:temporal",
                )
            )

    # Rule 3: references_entity — share a tag
    if unit.tags:
        for other in store.units_sharing_tags(unit.tags, exclude_id=unit.id):
            edges.append(
                Relation(
                    project=unit.project,
                    from_id=unit.id,
                    to_id=other.id,
                    relation_type="references_entity",
                    weight=0.6,
                    source="rule:entity",
                )
            )

    return edges
