"""Tests for transcript-based session summary extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engram.ingest.transcript import summary_from_transcript


def _write_transcript(path: Path, lines: list[dict]) -> Path:
    """Helper: write a list of dicts as JSONL."""
    with path.open("w") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    return path


def _user_msg(text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_msg(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def _system_msg(subtype: str = "compact_boundary") -> dict:
    return {"type": "system", "subtype": subtype, "content": "Conversation compacted"}


def _queue_op() -> dict:
    return {"type": "queue-operation", "operation": "enqueue"}


# ---------- Missing / empty files ----------


def test_missing_file(tmp_path: Path):
    assert summary_from_transcript(tmp_path / "nope.jsonl") is None


def test_empty_file(tmp_path: Path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert summary_from_transcript(p) is None


def test_no_user_messages(tmp_path: Path):
    p = _write_transcript(
        tmp_path / "t.jsonl",
        [_queue_op(), _assistant_msg("hello"), _system_msg()],
    )
    assert summary_from_transcript(p) is None


# ---------- Single user message ----------


def test_single_user_message(tmp_path: Path):
    p = _write_transcript(
        tmp_path / "t.jsonl",
        [_user_msg("Analyse der Codenummern in VBAP")],
    )
    result = summary_from_transcript(p)
    assert result is not None
    assert "Session topic:" in result
    assert "Codenummern" in result
    # Only 1 message → no "Recent" section
    assert "Recent" not in result


# ---------- Multiple user messages ----------


def test_multiple_user_messages(tmp_path: Path):
    p = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _user_msg("Analyse der Codenummern in VBAP"),
            _assistant_msg("OK, ich schaue mir das an."),
            _user_msg("Bitte auch die Marge berechnen"),
            _assistant_msg("Ergebnis: ..."),
            _user_msg("Danke, jetzt als Dashboard strukturieren"),
        ],
    )
    result = summary_from_transcript(p)
    assert result is not None
    assert "Session topic:" in result
    assert "Codenummern" in result
    assert "Recent[1]:" in result
    assert "Marge" in result
    assert "Recent[2]:" in result
    assert "Dashboard" in result


# ---------- Mixed line types ----------


def test_ignores_non_user_lines(tmp_path: Path):
    p = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _queue_op(),
            _user_msg("Erster Prompt"),
            _assistant_msg("Antwort"),
            _system_msg(),
            _user_msg("Zweiter Prompt"),
            {"type": "attachment", "data": {}},
            _user_msg("Dritter Prompt"),
        ],
    )
    result = summary_from_transcript(p)
    assert result is not None
    assert "Erster Prompt" in result
    # Recent should have the last 2 user messages
    assert "Zweiter Prompt" in result
    assert "Dritter Prompt" in result


# ---------- Truncation ----------


def test_long_message_truncated(tmp_path: Path):
    long_text = "A" * 1000
    p = _write_transcript(tmp_path / "t.jsonl", [_user_msg(long_text)])
    result = summary_from_transcript(p)
    assert result is not None
    assert len(result) < 500  # single msg, truncated to 300 chars


def test_max_body_respected(tmp_path: Path):
    msgs = [_user_msg(f"Message number {i} with some text") for i in range(50)]
    p = _write_transcript(tmp_path / "t.jsonl", msgs)
    result = summary_from_transcript(p, max_body=200)
    assert result is not None
    assert len(result) <= 200


# ---------- Malformed input ----------


def test_malformed_json_lines_skipped(tmp_path: Path):
    p = tmp_path / "t.jsonl"
    with p.open("w") as fh:
        fh.write("not json\n")
        fh.write(json.dumps(_user_msg("Valid message")) + "\n")
        fh.write("{broken\n")
    result = summary_from_transcript(p)
    assert result is not None
    assert "Valid message" in result


# ---------- Content as plain string ----------


def test_content_as_string(tmp_path: Path):
    p = _write_transcript(
        tmp_path / "t.jsonl",
        [{"type": "user", "message": {"role": "user", "content": "plain string"}}],
    )
    result = summary_from_transcript(p)
    assert result is not None
    assert "plain string" in result


# ---------- Integration: summarize_session uses transcript fallback ----------


def test_summarize_session_with_transcript_body(tmp_path: Path):
    """Verify that the transcript body integrates with summarize_session."""
    from engram.ingest.extractor import summarize_session

    p = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _user_msg("Setup engram hooks in settings.json"),
            _assistant_msg("Done."),
            _user_msg("Test the hooks"),
        ],
    )
    body = summary_from_transcript(p)
    unit = summarize_session("test-project", "sess-abc", [], compact_summary=body)
    assert unit is not None
    assert unit.type.value == "session_summary"
    assert "engram hooks" in unit.body
    assert "Test the hooks" in unit.body
