"""File-backed JSONL event queue.

The PostToolUse hook is on the hot path of every Claude Code tool call.
Opening a SQLite connection there costs 80–150 ms cold-start on macOS,
which adds up over a session. Instead, the hook just appends a single
JSON line to a queue file (sub-millisecond) and returns. The queue is
drained later by SessionStart, UserPromptSubmit, PostCompact, SessionEnd,
or an explicit `engram drain` call.

Concurrency:
  - Append uses POSIX O_APPEND, which is atomic for writes < PIPE_BUF.
  - Drain renames the queue file before reading. New appenders create a
    fresh file. Any writer mid-append still writes to its own fd, which
    points at the renamed (draining) file — those events are picked up
    in the same drain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


class EventQueue:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        # Open with O_APPEND so concurrent writers don't clobber each other.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

    def drain(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        tmp = self.path.with_suffix(self.path.suffix + ".draining")
        try:
            os.replace(self.path, tmp)
        except FileNotFoundError:
            return
        try:
            with open(tmp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # Corrupt line — skip rather than crash the drain.
                        continue
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def pending_count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as f:
            return sum(1 for _ in f)
