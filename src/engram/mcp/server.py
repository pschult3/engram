"""Local stdio MCP server exposing the memory store to Claude Code.

We expose a small, coarse tool surface — search, get, log_event, bootstrap,
flush — plus a handful of resources for stable references like the current
project digest. This matches the architecture's "small tool surface, do the
filtering locally" rule.

Every read tool drains the JSONL queue first, so memory written by hooks
during the session is visible to the next search call without a separate
worker process.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import load_config
from ..digest import build_bootstrap_capsule
from ..ingest import drain_queue, extract_units_from_event
from ..retrieval import rank_for_prompt, search_memory
from ..storage import EventQueue, Store, open_store
from ..storage.models import Event, MemoryType


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_server() -> FastMCP:
    cfg = load_config()
    store: Store = open_store(cfg.db_path, cfg.project)
    queue: EventQueue = EventQueue(cfg.queue_path)

    def _drain() -> None:
        # Cheap if the queue file doesn't exist or is empty.
        drain_queue(store, queue)

    mcp = FastMCP("engram")

    # ------------------------------ tools ------------------------------

    @mcp.tool()
    def memory_bootstrap(session_id: str | None = None) -> str:
        """Return a tiny project memory capsule for session warm-start.

        Use this once at the beginning of a session. The capsule is
        intentionally small — pull more via memory_search when needed.
        """
        _drain()
        if session_id:
            store.start_session(session_id, _now())
        return build_bootstrap_capsule(store)

    @mcp.tool()
    def memory_search(query: str, top_k: int = 8) -> str:
        """Search durable memory for items relevant to a query.

        Returns a compact bulleted block (capped to ~1200 chars) so it can be
        injected into the model context cheaply.
        """
        _drain()
        hits = search_memory(store, query, top_k=top_k)
        store.log_search(query, top_k, [u.id for u, _ in hits])
        rendered = rank_for_prompt(hits)
        return rendered or "(no matching memory)"

    @mcp.tool()
    def memory_get(unit_id: str) -> dict[str, Any]:
        """Fetch a single memory unit by id (full body, not just snippet)."""
        u = store.get_memory(unit_id)
        if not u:
            return {"error": "not_found", "id": unit_id}
        return u.model_dump()

    @mcp.tool()
    def memory_log_event(
        event_type: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a raw event and immediately extract candidate memory units.

        This is the model-facing write path. The hook-driven write path lives
        in engram.hooks and bypasses the LLM entirely.
        """
        event = store.append_event(
            Event(
                project=store.project,
                session_id=session_id,
                type=event_type,
                payload=payload,
            )
        )
        units = extract_units_from_event(event)
        created = 0
        for u in units:
            _, was_new = store.upsert_memory(u)
            created += int(was_new)
        if event.id:
            store.mark_processed([event.id])
        return {"event_id": event.id, "memory_units_created": created}

    @mcp.tool()
    def memory_flush(session_id: str | None = None) -> dict[str, Any]:
        """Force-drain the queue and process pending events into memory units.

        Called by hooks at PostCompact / SessionEnd, but also exposed so the
        model can request consolidation explicitly.
        """
        drain_stats = drain_queue(store, queue)
        events = store.unprocessed_events(limit=500)
        ids: list[str] = []
        created = drain_stats["memory_units_created"]
        for e in events:
            for u in extract_units_from_event(e):
                _, was_new = store.upsert_memory(u)
                created += int(was_new)
            if e.id:
                ids.append(e.id)
        store.mark_processed(ids)
        return {
            "events_processed": drain_stats["events_processed"] + len(ids),
            "memory_units_created": created,
            "session_id": session_id,
        }

    @mcp.tool()
    def memory_list(type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List active memory units, optionally filtered by type."""
        _drain()
        types = [MemoryType(type)] if type else None
        return [u.model_dump() for u in store.list_memory(types=types, limit=limit)]

    @mcp.tool()
    def memory_related(
        unit_id: str, depth: int = 1, limit: int = 10
    ) -> dict[str, Any]:
        """Return memory units related to a given unit via the graph.

        Follows relation edges outward from *unit_id* up to *depth* hops.
        Useful when you have a hit from memory_search and want to pull its
        neighbours without crafting a new query.
        """
        _drain()
        unit = store.get_memory(unit_id)
        if unit is None:
            return {"error": "not_found", "id": unit_id}
        from ..retrieval.graph import expand_with_graph

        neighbors = expand_with_graph(store, [unit_id], depth=depth, max_extra=limit)
        relations = store.get_relations(unit_id)
        return {
            "unit": unit.model_dump(),
            "neighbors": [
                {"unit": u.model_dump(), "score": round(s, 4)} for u, s in neighbors
            ],
            "edges": relations,
        }

    # ----------------------------- resources -----------------------------
    #
    # Resources are stable references Claude Code users can pin via @-mention,
    # e.g. `@engram:digest://project/current`. They are intentionally
    # higher-level than `memory_search` so the model can pull a focused slice
    # without crafting a query.

    @mcp.resource("digest://project/current")
    def project_digest() -> str:
        """The current bootstrap capsule for the active project."""
        _drain()
        return build_bootstrap_capsule(store)

    @mcp.resource("decisions://recent")
    def recent_decisions() -> str:
        _drain()
        units = store.list_memory(types=[MemoryType.decision], limit=20)
        if not units:
            return "(no decisions recorded yet)"
        lines = ["# Recent decisions"]
        for u in units:
            lines.append(f"- {u.title}: {u.body[:200]}")
        return "\n".join(lines)

    @mcp.resource("incidents://recent")
    def recent_incidents() -> str:
        _drain()
        units = store.list_memory(types=[MemoryType.incident], limit=20)
        if not units:
            return "(no incidents recorded yet)"
        lines = ["# Recent incidents"]
        for u in units:
            lines.append(f"- {u.title}: {u.body[:200]}")
        return "\n".join(lines)

    @mcp.resource("open-questions://current")
    def open_questions() -> str:
        _drain()
        units = store.list_memory(types=[MemoryType.open_question], limit=20)
        if not units:
            return "(no open questions)"
        lines = ["# Open questions"]
        for u in units:
            lines.append(f"- {u.title}: {u.body[:200]}")
        return "\n".join(lines)

    @mcp.resource("memory://unit/{unit_id}")
    def memory_unit_resource(unit_id: str) -> str:
        u = store.get_memory(unit_id)
        if not u:
            return json.dumps({"error": "not_found", "id": unit_id})
        return json.dumps(u.model_dump(), indent=2)

    @mcp.resource("relations://unit/{unit_id}")
    def unit_relations_resource(unit_id: str) -> str:
        """A unit and its first-degree neighbours as JSON."""
        u = store.get_memory(unit_id)
        if not u:
            return json.dumps({"error": "not_found", "id": unit_id})
        from ..retrieval.graph import expand_with_graph

        neighbors = expand_with_graph(store, [unit_id], depth=1, max_extra=10)
        return json.dumps(
            {
                "unit": u.model_dump(),
                "edges": store.get_relations(unit_id),
                "neighbors": [
                    {"unit": n.model_dump(), "score": round(s, 4)}
                    for n, s in neighbors
                ],
            },
            indent=2,
        )

    return mcp


def run_stdio() -> None:
    server = build_server()
    server.run()  # FastMCP defaults to stdio transport
