"""The per-prompt recall injects at most 3 hits and 600 chars (commit 17c5230)."""

from __future__ import annotations

import io
import json
import uuid

import pytest

from engram.hooks.handlers import handle_user_prompt_submit
from engram.storage import Store
from engram.storage.models import MemoryType, MemoryUnit


# Short units: six would fit in 600 chars, so only top_k holds the count.
# Long titles: three lines exceed 600 chars, so only max_chars holds the size.
@pytest.mark.parametrize("pad", [0, 60])
def test_user_prompt_recall_caps_hits_and_chars(store: Store, monkeypatch, pad: int) -> None:
    for i in range(10):
        store.upsert_memory(MemoryUnit(
            id=uuid.uuid4().hex[:12], project=store.project, type=MemoryType.fact,
            title=f"vtweg mapping rule {i} " + "long " * pad, body=f"vtweg {i} maps to channel {i}.",
        ))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "which vtweg mapping rule applies"})))
    out = handle_user_prompt_submit()
    hits = [line for line in out.splitlines() if line.startswith("- [")]
    assert 1 <= len(hits) <= 3
    assert len(out) <= 600
