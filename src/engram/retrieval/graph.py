"""Graph-based retrieval — neighbor expansion around seed memory units.

expand_with_graph() takes the top hits from FTS+vec retrieval and widens the
result set by following relation edges outward.  Deeper neighbors are penalised
with an exponential decay (0.6 ** depth) so they rank below direct hits.
"""

from __future__ import annotations

from ..storage import Store
from ..storage.models import MemoryUnit


def expand_with_graph(
    store: Store,
    seed_ids: list[str],
    depth: int = 1,
    max_extra: int = 4,
) -> list[tuple[MemoryUnit, float]]:
    """Return neighbor units not already in *seed_ids*, scored by edge proximity.

    Args:
        store:      The open store to query.
        seed_ids:   Unit IDs already in the retrieval result (will be excluded).
        depth:      How many hops to follow (1 or 2 recommended).
        max_extra:  Maximum number of neighbor units to return.

    Returns:
        List of (unit, score) pairs sorted by score descending.  Scores are
        edge_weight × 0.6**hop_distance, so a weight-1.0 edge at depth=1 gives
        0.6 and at depth=2 gives 0.36.
    """
    seen: set[str] = set(seed_ids)
    # uid → best score found so far
    candidates: dict[str, float] = {}

    # BFS frontier: list of (unit_id, score_so_far)
    frontier: list[tuple[str, float]] = [(uid, 1.0) for uid in seed_ids]

    for hop in range(depth):
        decay = 0.6 ** (hop + 1)
        next_frontier: list[tuple[str, float]] = []
        for uid, _ in frontier:
            for neighbor_unit, edge_weight in store.neighbors(uid, depth=1, max_nodes=20):
                nid = neighbor_unit.id
                if nid in seen:
                    continue
                score = edge_weight * decay
                if score > candidates.get(nid, 0.0):
                    candidates[nid] = score
                seen.add(nid)
                next_frontier.append((nid, score))
        frontier = next_frontier
        if not frontier:
            break

    # Fetch and return the top-scoring candidates.
    ranked = sorted(candidates.items(), key=lambda x: x[1], reverse=True)[:max_extra]
    result: list[tuple[MemoryUnit, float]] = []
    for uid, score in ranked:
        unit = store.get_memory(uid)
        if unit is not None:
            result.append((unit, score))

    return result
