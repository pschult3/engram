"""Map raw Claude Code tool-call payloads into engram event dicts.

This is the noise filter. The goal is to capture the *signal* of a coding
session — file edits, test failures, meaningful shell commands — and drop
the rest. Without this filter, every `ls` and `cat` would land in the store
and dilute retrieval.

Returned events are plain dicts (not Event models) because they go straight
into the JSONL queue.
"""

from __future__ import annotations

from typing import Any

from .redact import is_sensitive_path, redact_payload, redact_string

# Shell commands we never want to log: navigation, inspection, trivial output.
_NOISE_COMMANDS = {
    "ls", "cd", "pwd", "cat", "echo", "which", "type", "whoami",
    "head", "tail", "wc", "stat", "file", "true", "false",
}

# Commands that look like a test invocation. Used to upgrade a generic
# "command" event to a "test_failure" / "test_pass" event.
_TEST_MARKERS = (
    "pytest", "vitest", "jest", "mocha", "go test", "cargo test",
    "npm test", "npm run test", "yarn test", "pnpm test",
    "rspec", "phpunit", "ctest", "tox", "nox",
)

# Substrings in stderr/stdout that strongly suggest the command failed.
_FAILURE_MARKERS = (
    "FAILED", "FAIL:", "error:", "Error:", "ERROR:", "Traceback",
    "AssertionError", "panic:", "fatal:",
)


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 3] + "..."


def events_from_tool_call(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    tool_response: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tool_input = tool_input or {}
    tool_response = tool_response or {}
    out: list[dict[str, Any]] = []

    # ---- file edits ----
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not path:
            return out
        if is_sensitive_path(path):
            return out
        old = tool_input.get("old_string", "") or ""
        new = tool_input.get("new_string", "") or tool_input.get("content", "") or ""
        # cheap diff size as a stand-in for "how big was this change"
        delta = abs(len(new) - len(old))
        out.append(
            {
                "type": "file_edit",
                "payload": {
                    "tool": tool_name,
                    "path": path,
                    "delta_chars": delta,
                    "summary": f"{tool_name} on {path}",
                },
            }
        )
        return out

    # ---- bash commands ----
    if tool_name == "Bash":
        cmd = (tool_input.get("command") or "").strip()
        if not cmd:
            return out
        first = cmd.split()[0] if cmd.split() else ""
        # Strip leading sudo / env-var prefixes for noise detection
        if first in _NOISE_COMMANDS:
            return out

        stdout = tool_response.get("stdout", "") if isinstance(tool_response, dict) else ""
        stderr = tool_response.get("stderr", "") if isinstance(tool_response, dict) else ""
        interrupted = (
            tool_response.get("interrupted", False)
            if isinstance(tool_response, dict)
            else False
        )

        is_test = any(marker in cmd for marker in _TEST_MARKERS)
        combined = (stderr or "") + "\n" + (stdout or "")
        failed = interrupted or any(m in combined for m in _FAILURE_MARKERS)

        cmd = redact_string(cmd)

        if is_test:
            if failed:
                err_line = ""
                for line in combined.splitlines():
                    if any(m in line for m in _FAILURE_MARKERS):
                        err_line = line.strip()
                        break
                out.append(
                    {
                        "type": "test_failure",
                        "payload": redact_payload({
                            "command": _truncate(cmd, 200),
                            "name": _truncate(cmd.split()[0], 80),
                            "message": _truncate(err_line, 240),
                        }),
                    }
                )
            # We deliberately do NOT log test passes — they bloat the store.
            return out

        # Generic command: only keep failures and "interesting" verbs.
        interesting = first in {
            "git", "npm", "pnpm", "yarn", "uv", "pip", "poetry",
            "cargo", "go", "make", "docker", "kubectl", "terraform",
        }
        if not (failed or interesting):
            return out

        out.append(
            {
                "type": "command",
                "payload": redact_payload({
                    "command": _truncate(cmd, 200),
                    "status": "fail" if failed else "ok",
                }),
            }
        )
        return out

    # Everything else (Read, Grep, Glob, Task, MCP tools, ...) is dropped.
    return out
