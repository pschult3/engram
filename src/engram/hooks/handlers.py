"""Claude Code hook handlers.

These are invoked by the CLI (`engram hook ...`). Each Claude Code hook
sends a JSON payload on stdin and consumes either stdout text (which becomes
additional context) or a structured JSON response.

Write/read split:
  - PostToolUse runs on the hot path of *every* tool call. It MUST stay
    fast, so it only appends to a JSONL queue and returns. No SQLite.
  - SessionStart, UserPromptSubmit, PostCompact and SessionEnd drain the
    queue first, then do their actual work. They run rarely enough that
    a SQLite open is fine.

Hook reference (Claude Code):
  - SessionStart        — drain queue, inject bootstrap capsule
  - UserPromptSubmit    — drain queue, inject query-specific retrieval hits
  - PostToolUse         — append tool event to queue (no SQLite)
  - PostCompact         — drain queue, persist Claude's compact_summary
  - SessionEnd          — drain queue, finalize the session
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from ..config import load_config
from ..digest import build_bootstrap_capsule
from ..ingest import drain_queue, events_from_tool_call, summarize_session
from ..retrieval import rank_for_prompt, search_memory
from ..storage import EventQueue, Store, open_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Surface to stderr so `claude --debug` users can see why a hook is
        # silently no-opping. Returning {} causes the handler to skip cleanly.
        sys.stderr.write(f"engram: hook payload not valid JSON ({e}); skipping\n")
        return {}


def _open_store() -> tuple[Store, EventQueue]:
    cfg = load_config()
    store = open_store(cfg.db_path, cfg.project)
    queue = EventQueue(cfg.queue_path)
    return store, queue


# --------------------------- SessionStart ---------------------------

def handle_session_start() -> str:
    payload = _read_payload()
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"
    store, queue = _open_store()
    try:
        drain_queue(store, queue)
        store.start_session(session_id, _now())
        return build_bootstrap_capsule(store)
    finally:
        store.close()


# --------------------------- UserPromptSubmit ---------------------------

def handle_user_prompt_submit() -> str:
    payload = _read_payload()
    prompt = (
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("message")
        or ""
    )
    if len(prompt.strip()) < 8:
        # Skip retrieval for trivially short prompts ("yes", "ok", "next")
        # to avoid latency on the first token.
        return ""
    store, queue = _open_store()
    try:
        drain_queue(store, queue)
        hits = search_memory(store, prompt, top_k=6)
        store.log_search(prompt, 6, [u.id for u, _ in hits])
        return rank_for_prompt(hits)
    finally:
        store.close()


# --------------------------- PostToolUse (HOT PATH) ---------------------------

def handle_post_tool_use() -> dict[str, Any]:
    """Append tool events to the queue. No SQLite, no extraction.

    Must finish in < 5 ms to not slow down Claude Code.
    """
    payload = _read_payload()
    tool_name = payload.get("tool_name") or payload.get("toolName") or "unknown"
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_response = payload.get("tool_response") or payload.get("toolResponse") or {}
    session_id = payload.get("session_id") or payload.get("sessionId")

    events = events_from_tool_call(tool_name, tool_input, tool_response)
    if not events:
        return {"ok": True, "events_queued": 0}

    cfg = load_config()
    queue = EventQueue(cfg.queue_path)
    now = _now()
    for ev in events:
        queue.append(
            {
                "project": cfg.project,
                "session_id": session_id,
                "type": ev["type"],
                "payload": ev["payload"],
                "created_at": now,
            }
        )
    return {"ok": True, "events_queued": len(events)}


# --------------------------- PostCompact ---------------------------

def handle_post_compact() -> dict[str, Any]:
    payload = _read_payload()
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"
    summary = payload.get("compact_summary") or payload.get("summary")
    store, queue = _open_store()
    try:
        drain_stats = drain_queue(store, queue)
        events = store.unprocessed_events(limit=1000)
        unit = summarize_session(store.project, session_id, events, compact_summary=summary)
        created = drain_stats["memory_units_created"]
        if unit:
            _, was_new = store.upsert_memory(unit)
            created += int(was_new)
        return {"ok": True, "memory_units_created": created}
    finally:
        store.close()


# --------------------------- SessionEnd ---------------------------

def handle_session_end() -> dict[str, Any]:
    payload = _read_payload()
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"
    summary = payload.get("summary")
    store, queue = _open_store()
    try:
        drain_stats = drain_queue(store, queue)
        events = store.unprocessed_events(limit=1000)
        unit = summarize_session(store.project, session_id, events, compact_summary=summary)
        created = drain_stats["memory_units_created"]
        if unit:
            _, was_new = store.upsert_memory(unit)
            created += int(was_new)
        store.end_session(session_id, _now(), summary)
        return {"ok": True, "memory_units_created": created}
    finally:
        store.close()
