"""Extract a deterministic summary from a Claude Code JSONL transcript.

Used as a fallback in SessionEnd when no compact_summary is available.
Reads user messages from the transcript and builds a concise summary
capturing the session's topic and recent activity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_MAX_MSG_CHARS = 300
_MAX_BODY_CHARS = 2000


def _extract_user_text(obj: dict) -> str | None:
    """Pull plain text from a user-type transcript line.

    Filters out slash commands (/exit, /model, etc.) and system XML tags
    that are not meaningful session content.
    """
    if obj.get("type") != "user":
        return None
    msg = obj.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = " ".join(parts).strip()
    else:
        return None

    if not text:
        return None

    # Skip slash commands and system artifacts
    if text.startswith("/") or text.startswith("<command-name>"):
        return None
    if "<local-command" in text or "<system-reminder>" in text:
        return None

    return text


def summary_from_transcript(path: str | Path, max_body: int = _MAX_BODY_CHARS) -> str | None:
    """Build a deterministic summary from a JSONL transcript file.

    Strategy:
      1. Collect all user messages.
      2. Use the first message as the session topic (truncated).
      3. Use the last 2 messages as recent context (truncated).
      4. Return a combined summary capped at *max_body* characters.

    Returns None if the file is missing, empty, or contains no user messages.
    """
    path = Path(path)
    if not path.exists():
        return None

    user_messages: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _extract_user_text(obj)
                if text:
                    user_messages.append(text)
    except OSError as exc:
        sys.stderr.write(f"engram: cannot read transcript {path} ({exc})\n")
        return None

    if not user_messages:
        return None

    first = user_messages[0][:_MAX_MSG_CHARS]
    parts = [f"Session topic: {first}"]

    if len(user_messages) > 1:
        recent = user_messages[-2:]
        for i, msg in enumerate(recent, 1):
            parts.append(f"Recent[{i}]: {msg[:_MAX_MSG_CHARS]}")

    body = "\n".join(parts)
    if len(body) > max_body:
        body = body[:max_body - 3] + "..."
    return body
