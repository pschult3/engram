"""Tests for bootstrap capsule token budget enforcement."""

from __future__ import annotations

import uuid

from engram.digest.bootstrap import build_bootstrap_capsule, _estimate_tokens, _DEFAULT_MAX_TOKENS
from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


def _add_unit(
    store: Store,
    type_: MemoryType,
    title: str,
    body: str = "",
) -> None:
    body = body or ("x " * 80)  # ~160 chars, ~40 tokens per bullet
    u = MemoryUnit(
        id=uuid.uuid4().hex[:12],
        project=store.project,
        type=type_,
        title=title,
        body=body,
    )
    store.upsert_memory(u)


# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------


def test_estimate_tokens_basic() -> None:
    assert _estimate_tokens("hello world") == 2  # 11 chars // 4


def test_estimate_tokens_empty() -> None:
    assert _estimate_tokens("") == 1  # clamped to 1


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_capsule_respects_explicit_max_tokens(store: Store) -> None:
    """With a tight budget, the rendered capsule must stay within it."""
    for i in range(20):
        _add_unit(store, MemoryType.decision, f"decision {i}", "a" * 200)

    capsule = build_bootstrap_capsule(store, max_tokens=100)
    estimated = _estimate_tokens(capsule)
    assert estimated <= 100


def test_capsule_respects_store_setting(store: Store) -> None:
    store.set_setting("bootstrap_max_tokens", "80")
    for i in range(10):
        _add_unit(store, MemoryType.decision, f"dec {i}", "b" * 200)

    capsule = build_bootstrap_capsule(store)  # reads from store setting
    estimated = _estimate_tokens(capsule)
    assert estimated <= 80


def test_capsule_uses_default_when_no_setting(store: Store) -> None:
    """With no setting and no override, the cap is _DEFAULT_MAX_TOKENS."""
    for i in range(30):
        _add_unit(store, MemoryType.decision, f"decision {i}", "c" * 300)

    capsule = build_bootstrap_capsule(store)
    estimated = _estimate_tokens(capsule)
    assert estimated <= _DEFAULT_MAX_TOKENS


def test_capsule_empty_store_returns_placeholder(store: Store) -> None:
    capsule = build_bootstrap_capsule(store)
    assert "no durable memory" in capsule


def test_capsule_includes_multiple_types(store: Store) -> None:
    _add_unit(store, MemoryType.decision, "use postgres")
    _add_unit(store, MemoryType.preference, "prefer snake_case")
    _add_unit(store, MemoryType.open_question, "what about redis?")

    capsule = build_bootstrap_capsule(store)
    assert "Decision" in capsule
    assert "Preference" in capsule
    assert "Open Question" in capsule


def test_capsule_sections_stop_at_budget(store: Store) -> None:
    """If budget is exhausted after decisions, later types must be omitted."""
    # Fill decisions to near-cap.
    for i in range(10):
        _add_unit(store, MemoryType.decision, f"long decision {i}", "d" * 300)
    _add_unit(store, MemoryType.preference, "a preference that should not appear")

    capsule = build_bootstrap_capsule(store, max_tokens=60)
    # With 60 tokens, there's barely room for the header + a couple of
    # decisions — no room for preferences.
    assert "Preference" not in capsule


def test_invalid_store_setting_falls_back_to_default(store: Store) -> None:
    store.set_setting("bootstrap_max_tokens", "notanumber")
    for i in range(5):
        _add_unit(store, MemoryType.decision, f"d {i}")

    # Must not raise; must stay within the default cap.
    capsule = build_bootstrap_capsule(store)
    assert _estimate_tokens(capsule) <= _DEFAULT_MAX_TOKENS
