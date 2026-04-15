"""Tiny SessionStart capsule (target: 150–400 tokens).

We pull the most useful active memory units of a few high-signal types and
render them as a short bulleted block. No global wiki, no full project dump —
that is exactly the token trap we are avoiding.

Token budget is stored per-project in the meta table (key: bootstrap_max_tokens).
Default is 400. Set via: engram config set bootstrap_max_tokens 300
"""

from __future__ import annotations

from ..storage import Store
from ..storage.models import MemoryType

_CAPSULE_TYPES = [
    MemoryType.fact,
    MemoryType.decision,
    MemoryType.open_question,
    MemoryType.preference,
    MemoryType.incident,
    MemoryType.lesson,
]

_DEFAULT_MAX_TOKENS = 400


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 characters ≈ 1 token (GPT-ish ratio)."""
    return max(1, len(text) // 4)


def build_bootstrap_capsule(store: Store, max_tokens: int | None = None) -> str:
    if max_tokens is None:
        raw = store.get_setting("bootstrap_max_tokens", str(_DEFAULT_MAX_TOKENS))
        try:
            max_tokens = int(raw)
        except ValueError:
            max_tokens = _DEFAULT_MAX_TOKENS

    header = f"# Project memory: {store.project}"
    tokens_used = _estimate_tokens(header) + 1

    sections: list[str] = [header]

    for mt in _CAPSULE_TYPES:
        units = store.list_memory(types=[mt], limit=20)
        if not units:
            continue

        section_header = f"\n## {mt.value.replace('_', ' ').title()}"
        tokens_used += _estimate_tokens(section_header)
        if tokens_used >= max_tokens:
            break

        type_lines: list[str] = [section_header]
        for u in units:
            snippet = u.body.strip().replace("\n", " ")
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            line = f"- {u.title}: {snippet}"
            line_tokens = _estimate_tokens(line)
            if tokens_used + line_tokens > max_tokens:
                break
            type_lines.append(line)
            tokens_used += line_tokens

        # Only add the section if it has at least one bullet.
        if len(type_lines) > 1:
            sections.extend(type_lines)

    if len(sections) == 1:
        sections.append("\n_(no durable memory yet — run a few sessions)_")

    return "\n".join(sections)
