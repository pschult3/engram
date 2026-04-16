"""Retrieval over the canonical store.

Retrieval pipeline:
  1. FTS5 lexical search (always)
  2. Vector search via sqlite-vec (if extension loaded + unit is embedded)
  3. Reciprocal Rank Fusion of the two ranked lists
  4. TYPE_WEIGHTS multiplier
  5. Graph expansion of the top seeds (if relations exist)

When vec or graph are unavailable the function degrades to FTS5-only — same
call site, same return type, no configuration required.
"""

from __future__ import annotations

from ..storage import Store
from ..storage.models import MemoryUnit, MemoryType


# ---------------------------------------------------------------------------
# Type weights
# ---------------------------------------------------------------------------

TYPE_WEIGHTS: dict[MemoryType, float] = {
    MemoryType.decision: 1.0,
    MemoryType.open_question: 0.95,
    MemoryType.preference: 0.9,
    MemoryType.fact: 0.85,
    MemoryType.lesson: 0.85,
    MemoryType.incident: 0.8,
    MemoryType.task: 0.75,
    MemoryType.entity_relation: 0.7,
    MemoryType.session_summary: 0.15,
    MemoryType.code_change: 0.0,
}


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

def _rrf_fuse(
    rankings: list[list[tuple[str, float]]],
    k: int = 60,
) -> dict[str, float]:
    """Reciprocal Rank Fusion over multiple ranked lists of (unit_id, score).

    Returns a mapping of unit_id → fused score (higher = better).
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (uid, _) in enumerate(ranking):
            fused[uid] = fused.get(uid, 0.0) + 1.0 / (k + rank + 1)
    return fused


# ---------------------------------------------------------------------------
# Main retrieval
# ---------------------------------------------------------------------------

def search_memory(
    store: Store,
    query: str,
    top_k: int = 8,
) -> list[tuple[MemoryUnit, float]]:
    """Hybrid retrieval: FTS5 + (optional) vec + (optional) graph expansion.

    Always returns at most *top_k* results, ordered by relevance.
    """
    candidate_k = top_k * 2

    # --- step 1: lexical search ---
    fts_hits = store.search(query, top_k=candidate_k)
    rankings: list[list[tuple[str, float]]] = [
        [(u.id, s) for u, s in fts_hits]
    ]

    # --- step 2: vector search ---
    if store.vec_enabled:
        from .embeddings import get_embedder

        embedder = get_embedder()
        if embedder is not None:
            try:
                query_vec = embedder.embed_one(query)
                vec_hits = store.search_vec(query_vec, top_k=candidate_k)
                if vec_hits:
                    rankings.append([(u.id, s) for u, s in vec_hits])
            except Exception:
                pass  # vec failure is non-fatal

    # --- step 3: RRF fusion ---
    all_units: dict[str, MemoryUnit] = {}
    for hits in [fts_hits] + (
        [store.search_vec([], top_k=0)] if store.vec_enabled else []
    ):
        for u, _ in hits:
            all_units[u.id] = u

    # Collect all candidate units from all rankings.
    for ranking in rankings:
        for uid, _ in ranking:
            if uid not in all_units:
                u = store.get_memory(uid)
                if u:
                    all_units[uid] = u

    fused_scores = _rrf_fuse(rankings)

    # --- step 4: type-weight multiplier ---
    weighted: list[tuple[MemoryUnit, float]] = []
    for uid, fused_score in fused_scores.items():
        unit = all_units.get(uid)
        if unit is None:
            continue
        tw = TYPE_WEIGHTS.get(unit.type, 0.6)
        weighted.append((unit, fused_score * tw))

    weighted.sort(key=lambda x: x[1], reverse=True)
    top = weighted[:top_k]

    # --- step 5: graph expansion ---
    try:
        from .graph import expand_with_graph

        seed_ids = [u.id for u, _ in top[:4]]
        neighbors = expand_with_graph(store, seed_ids, depth=1, max_extra=4)
        existing_ids = {u.id for u, _ in top}
        for neighbor, nscore in neighbors:
            if neighbor.id not in existing_ids:
                # Penalise graph neighbors so they rank below direct hits.
                tw = TYPE_WEIGHTS.get(neighbor.type, 0.6)
                top.append((neighbor, nscore * 0.5 * tw))
        top.sort(key=lambda x: x[1], reverse=True)
        top = top[:top_k]
    except Exception:
        pass

    return top


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def rank_for_prompt(hits: list[tuple[MemoryUnit, float]], max_chars: int = 1200) -> str:
    """Render hits as a compact bulleted block, capped by total characters.

    The character cap is the cheapest way to keep the injected context tiny.
    A token-aware cap can replace this once we standardize on a tokenizer.
    """
    if not hits:
        return ""
    lines: list[str] = ["Relevant memory:"]
    used = len(lines[0])
    for unit, _ in hits:
        snippet = unit.body.strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        line = f"- [{unit.type.value}] {unit.title}: {snippet}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
