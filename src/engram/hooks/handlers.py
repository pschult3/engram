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
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from ..config import load_config
from ..digest import build_bootstrap_capsule
from ..ingest import drain_queue, events_from_tool_call, summary_from_transcript, summarize_session
from ..retrieval import rank_for_prompt, search_memory
from ..storage import EventQueue, Store, open_store
from ..storage.models import MemoryType, MemoryUnit


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


# --------------------------- PreCompact ---------------------------

_COMPACT_INSTRUCTIONS = """\
When writing this summary, follow these rules for engram (the project memory system):

1. Clearly separate what was IMPLEMENTED (files created/modified, tests run, \
commands executed) from what was only DISCUSSED (ideas, plans, hypotheticals).
2. Use markers: [DONE] for completed work, [DISCUSSED] for ideas/plans not yet built.
3. For [DONE] items include: file paths, what changed, test results if any.
4. Keep the summary under 800 words.
5. Do NOT wrap the summary in <analysis> or <summary> XML tags — plain text only.
6. Start with a one-line session topic, then list items.
7. If memories retrieved earlier in this session were misleading, too verbose, \
or missing critical info, you MAY append a [FEEDBACK] section (1-3 bullets) \
describing what future summaries should do differently. Only add feedback when \
there is a genuine quality issue — do NOT force it.
8. If the session produced VERIFIED facts, decisions, incidents, or preferences, \
append structured blocks AFTER the main summary. Only include information backed \
by: user confirmation, BQ query result, Looker metadata, or external docs. \
Skip types where nothing verified happened. One block per paragraph, blank line \
between blocks. Exact format:

[FACT] title: <short name>
body: <2-3 sentences ending with "Verified: [source]">
tags: tag1, tag2

[DECISION] title: <decision name>
body: <2-3 sentences ending with "Verified: [source]">
tags: tag1, tag2

Same format for [INCIDENT] and [PREFERENCE]. If nothing verified → omit entirely.\
"""

_FEEDBACK_TAG = "memory_feedback"


def handle_pre_compact() -> str:
    """Inject compact formatting instructions + past feedback.

    Returns plain text on stdout which Claude Code injects as
    additionalContext into the compact prompt.
    """
    _read_payload()  # consume stdin even if we don't use it

    instructions = _COMPACT_INSTRUCTIONS

    # Append recent feedback from prior sessions, if any.
    try:
        store, _ = _open_store()
        try:
            feedback_units = store.find_by_tag(_FEEDBACK_TAG, limit=3)
        finally:
            store.close()
    except Exception:
        feedback_units = []

    if feedback_units:
        instructions += "\n\nPast feedback on summary quality (apply these):\n"
        for u in feedback_units:
            body = u.body.strip()
            # Avoid double-bullet when feedback body already has bullet points.
            if body.startswith("- "):
                instructions += body + "\n"
            else:
                instructions += f"- {body}\n"

    return instructions


# --------------------------- PostCompact ---------------------------

_FEEDBACK_RE = re.compile(
    r"\[FEEDBACK\]\s*\n?(.*?)"
    r"(?=\n\s*##|\n\s*\[(?:FACT|DECISION|INCIDENT|PREFERENCE)\]|\n\s*</\s*summary\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)

_FEEDBACK_STRIP_RE = re.compile(
    r"\[FEEDBACK\]\s*\n?.*?"
    r"(?=\n\s*##|\n\s*\[(?:FACT|DECISION|INCIDENT|PREFERENCE)\]|\n\s*</\s*summary\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _extract_feedback(text: str | None) -> str | None:
    """Extract an optional [FEEDBACK] section from a compact summary.

    Bounded by the next section marker (## header, structured block,
    </summary>, or end-of-text). Without a boundary the regex greedy-eats
    trailing content like file lists into the lesson body.
    """
    if not text:
        return None
    m = _FEEDBACK_RE.search(text)
    if not m:
        return None
    body = m.group(1).strip()
    body = re.sub(r"</?\s*summary[^>]*>\s*$", "", body, flags=re.IGNORECASE).strip()
    return body if body else None


def _strip_feedback(text: str | None) -> str | None:
    """Return the summary without the [FEEDBACK] section."""
    if not text:
        return text
    return _FEEDBACK_STRIP_RE.sub("", text).strip()


_STRUCTURED_TYPES = {
    "FACT": MemoryType.fact,
    "DECISION": MemoryType.decision,
    "INCIDENT": MemoryType.incident,
    "PREFERENCE": MemoryType.preference,
}

_STRUCTURED_BLOCK_RE = re.compile(
    r"^\s*\[(FACT|DECISION|INCIDENT|PREFERENCE)\]\s*"
    r"title:\s*([^\n]+)\s*\n"
    r"body:\s*(.+?)"
    r"(?:\s*\ntags:\s*(.+?))?"
    r"\s*$",
    re.DOTALL | re.IGNORECASE,
)

_STRUCTURED_MARKER_RE = re.compile(
    r"^\s*\[(?:FACT|DECISION|INCIDENT|PREFERENCE)\]",
    re.IGNORECASE,
)


def _extract_structured_units(
    text: str | None,
    project: str,
    session_id: str,
) -> list[MemoryUnit]:
    """Extract [FACT]/[DECISION]/[INCIDENT]/[PREFERENCE] blocks as units."""
    if not text:
        return []
    units: list[MemoryUnit] = []
    for para in re.split(r"\n\s*\n", text):
        m = _STRUCTURED_BLOCK_RE.match(para)
        if not m:
            continue
        type_str = m.group(1).upper()
        title = m.group(2).strip()
        body = m.group(3).strip()
        tags_str = (m.group(4) or "").strip().strip("[]")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        memory_type = _STRUCTURED_TYPES[type_str]
        if memory_type.value not in tags:
            tags.insert(0, memory_type.value)
        units.append(
            MemoryUnit(
                id=uuid.uuid4().hex[:12],
                project=project,
                type=memory_type,
                title=title[:120],
                body=body,
                tags=tags,
                source_refs=[f"session:{session_id}"],
                confidence=0.8,
            )
        )
    return units


def _strip_structured_units(text: str | None) -> str | None:
    """Return the summary without [FACT]/[DECISION]/[INCIDENT]/[PREFERENCE] blocks."""
    if not text:
        return text
    kept = [p for p in re.split(r"\n\s*\n", text) if not _STRUCTURED_MARKER_RE.match(p)]
    return "\n\n".join(kept).strip()


_SIGNAL_MARKER_RE = re.compile(
    r"\[(?:DONE|DISCUSSED|FACT|DECISION|INCIDENT|PREFERENCE)\]",
    re.IGNORECASE,
)
_MIN_SESSION_SUMMARY_CHARS = 120


def _is_low_signal_summary(body: str | None) -> bool:
    """True if the session_summary body is garbage / prompt-echo only.

    Drops two noise classes:
      1. Too-short bodies (less than _MIN_SESSION_SUMMARY_CHARS chars),
         e.g. "Session topic: eit".
      2. Transcript-fallback echoes: contain "Recent[" (user prompts) but
         no [DONE]/[DISCUSSED]/[FACT]/[DECISION] marker, meaning the session
         ended without compaction and we are about to persist just the
         user's questions. Those hurt retrieval more than they help.
    """
    if not body:
        return True
    b = body.strip()
    if len(b) < _MIN_SESSION_SUMMARY_CHARS:
        return True
    if "Recent[" in b and not _SIGNAL_MARKER_RE.search(b):
        return True
    return False


def handle_post_compact() -> dict[str, Any]:
    payload = _read_payload()
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"
    summary = payload.get("compact_summary") or payload.get("summary")
    store, queue = _open_store()
    try:
        drain_stats = drain_queue(store, queue)
        events = store.unprocessed_events(limit=1000)

        # Extract structured units, feedback, and clean summary.
        structured = _extract_structured_units(summary, store.project, session_id)
        summary_no_structured = _strip_structured_units(summary)
        feedback = _extract_feedback(summary_no_structured)
        clean_summary = _strip_feedback(summary_no_structured)

        unit = summarize_session(store.project, session_id, events, compact_summary=clean_summary)
        created = drain_stats["memory_units_created"]
        if unit and not _is_low_signal_summary(unit.body):
            _, was_new = store.upsert_memory(unit)
            created += int(was_new)

        for su in structured:
            _, was_new = store.upsert_memory(su)
            created += int(was_new)

        if feedback:
            fb_unit = MemoryUnit(
                id=uuid.uuid4().hex[:12],
                project=store.project,
                type=MemoryType.lesson,
                title="compact feedback",
                body=feedback,
                tags=[_FEEDBACK_TAG],
                source_refs=[f"session:{session_id}"],
                confidence=0.6,
            )
            store.upsert_memory(fb_unit)
            created += 1

        return {"ok": True, "memory_units_created": created}
    finally:
        store.close()


# --------------------------- SessionEnd ---------------------------

def handle_session_end() -> dict[str, Any]:
    payload = _read_payload()
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"
    summary = payload.get("summary")
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    store, queue = _open_store()
    try:
        drain_stats = drain_queue(store, queue)
        events = store.unprocessed_events(limit=1000)

        # If no explicit summary and no queued events, try the transcript.
        if not summary and not events and transcript_path:
            summary = summary_from_transcript(transcript_path)

        # Extract structured units, feedback, and clean summary.
        structured = _extract_structured_units(summary, store.project, session_id)
        summary_no_structured = _strip_structured_units(summary)
        feedback = _extract_feedback(summary_no_structured)
        clean_summary = _strip_feedback(summary_no_structured)

        unit = summarize_session(store.project, session_id, events, compact_summary=clean_summary)
        created = drain_stats["memory_units_created"]
        if unit and not _is_low_signal_summary(unit.body):
            _, was_new = store.upsert_memory(unit)
            created += int(was_new)

        for su in structured:
            _, was_new = store.upsert_memory(su)
            created += int(was_new)

        if feedback:
            fb_unit = MemoryUnit(
                id=uuid.uuid4().hex[:12],
                project=store.project,
                type=MemoryType.lesson,
                title="compact feedback",
                body=feedback,
                tags=[_FEEDBACK_TAG],
                source_refs=[f"session:{session_id}"],
                confidence=0.6,
            )
            store.upsert_memory(fb_unit)
            created += 1

        store.end_session(session_id, _now(), clean_summary)
        return {"ok": True, "memory_units_created": created}
    finally:
        store.close()
