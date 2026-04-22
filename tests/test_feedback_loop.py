"""Tests for the self-optimizing feedback loop in compact handlers."""

from __future__ import annotations

import json

import pytest

from engram.hooks.handlers import _extract_feedback, _strip_feedback, _FEEDBACK_TAG
from engram.storage import Store


# ---------- _extract_feedback ----------


def test_extract_feedback_present():
    text = "Session topic: engram hooks\n[DONE] Added hooks\n\n[FEEDBACK]\n- Summaries were too verbose\n- Missing file paths"
    fb = _extract_feedback(text)
    assert fb is not None
    assert "too verbose" in fb
    assert "Missing file paths" in fb


def test_extract_feedback_absent():
    text = "Session topic: engram hooks\n[DONE] Added hooks"
    assert _extract_feedback(text) is None


def test_extract_feedback_none():
    assert _extract_feedback(None) is None


def test_extract_feedback_empty_section():
    text = "Session topic: test\n[FEEDBACK]\n   "
    assert _extract_feedback(text) is None


def test_extract_feedback_case_insensitive():
    text = "Session topic: test\n[feedback]\n- shorter summaries please"
    fb = _extract_feedback(text)
    assert fb is not None
    assert "shorter" in fb


def test_extract_feedback_inline_ignored():
    """[FEEDBACK] inside backtick prose (describing past work) must not match."""
    text = (
        "[DONE] E2E test: rich payload (`[DONE]` + `[FEEDBACK]` + `## Dateien`"
        " + `</summary>`) produced session_summary (clean, no XML, no feedback"
        " inline, file list preserved) + feedback lesson."
    )
    assert _extract_feedback(text) is None


def test_extract_feedback_no_bullets_rejected():
    """[FEEDBACK] at line-start but body is prose without bullets → None."""
    text = "Session topic: test\n[FEEDBACK]\nThis was a good session overall."
    assert _extract_feedback(text) is None


# ---------- _strip_feedback ----------


def test_strip_removes_feedback():
    text = "Session topic: hooks\n[DONE] Added file\n\n[FEEDBACK]\n- Be shorter"
    clean = _strip_feedback(text)
    assert "[FEEDBACK]" not in clean
    assert "[DONE] Added file" in clean
    assert "Session topic: hooks" in clean


def test_strip_no_feedback():
    text = "Session topic: hooks\n[DONE] Added file"
    assert _strip_feedback(text) == text


def test_strip_none():
    assert _strip_feedback(None) is None


# ---------- find_by_tag ----------


def test_find_by_tag(store: Store):
    from engram.storage.models import MemoryType, MemoryUnit

    u = MemoryUnit(
        id="fb001",
        project="test",
        type=MemoryType.lesson,
        title="compact feedback",
        body="Summaries should include file paths",
        tags=[_FEEDBACK_TAG],
        source_refs=["session:test-1"],
        confidence=0.6,
    )
    store.upsert_memory(u)

    results = store.find_by_tag(_FEEDBACK_TAG, limit=5)
    assert len(results) == 1
    assert results[0].id == "fb001"
    assert results[0].body == "Summaries should include file paths"


def test_find_by_tag_empty(store: Store):
    results = store.find_by_tag("nonexistent_tag", limit=5)
    assert results == []


# ---------- Integration: feedback round-trip ----------


def test_feedback_round_trip(store: Store):
    """Simulate: PostCompact stores feedback → PreCompact reads it."""
    from engram.storage.models import MemoryType, MemoryUnit

    # Simulate PostCompact storing feedback
    fb_unit = MemoryUnit(
        id="fb-rt-001",
        project="test",
        type=MemoryType.lesson,
        title="compact feedback",
        body="- Include test results in [DONE] items\n- Max 500 words",
        tags=[_FEEDBACK_TAG],
        source_refs=["session:sess-123"],
        confidence=0.6,
    )
    store.upsert_memory(fb_unit)

    # Simulate PreCompact reading feedback
    feedback_units = store.find_by_tag(_FEEDBACK_TAG, limit=3)
    assert len(feedback_units) == 1
    assert "test results" in feedback_units[0].body
    assert "500 words" in feedback_units[0].body
