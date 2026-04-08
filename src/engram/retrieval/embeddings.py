"""Embedding backend for vector search.

Two modes:
  prod  — fastembed (BAAI/bge-small-en-v1.5, 384 dim, offline, ~30 MB)
  stub  — deterministic hash-based (for testing / CI, no ML library needed)

Set ENGRAM_EMBEDDER=stub to force the stub.
Set ENGRAM_EMBEDDER=<huggingface-model-id> to use a different fastembed model
(you are responsible for the correct dimension matching the vec table DDL).

The singleton pattern keeps the model in memory across calls; use
reset_embedder() in tests to clear state between cases.
"""

from __future__ import annotations

import hashlib
import os
import struct

EMBED_DIM = 384
_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder:
    """Wraps fastembed or the deterministic stub behind a uniform interface."""

    def __init__(self, model: object | None, *, stub: bool = False) -> None:
        self._model = model
        self._stub = stub

    @classmethod
    def create(cls) -> "Embedder | None":
        """Return an Embedder if a backend is available, else None."""
        mode = os.environ.get("ENGRAM_EMBEDDER", "").strip().lower()
        if mode == "stub":
            return cls(None, stub=True)
        if mode == "none":
            return None
        # Try fastembed.
        model_name = mode or _DEFAULT_MODEL
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]

            model = TextEmbedding(model_name)
            return cls(model)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        return "stub" if self._stub else (os.environ.get("ENGRAM_EMBEDDER") or _DEFAULT_MODEL)

    def embed_one(self, text: str) -> list[float]:
        if self._stub:
            return _stub_embed(text)
        result = list(self._model.embed([text[:1024]]))  # type: ignore[union-attr]
        return result[0].tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if self._stub:
            return [_stub_embed(t) for t in texts]
        result = list(self._model.embed([t[:1024] for t in texts]))  # type: ignore[union-attr]
        return [r.tolist() for r in result]

    @staticmethod
    def serialize(embedding: list[float]) -> bytes:
        """Serialize to little-endian float32 bytes for sqlite-vec."""
        return struct.pack(f"<{EMBED_DIM}f", *embedding)


# ---------------------------------------------------------------------------
# Stub implementation
# ---------------------------------------------------------------------------


def _stub_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic, normalized embedding from SHA-256 hash chain.

    Each SHA-256 block (32 bytes) yields 32 floats in [-1, 1] by interpreting
    each byte as a signed offset from 128.  This avoids NaN / Inf values that
    arise when raw bytes are reinterpreted directly as IEEE 754 float32.

    Not semantically meaningful — only useful for infrastructure testing.
    """
    vals: list[float] = []
    data = text.encode()
    counter = 0
    while len(vals) < dim:
        h = hashlib.sha256(data + counter.to_bytes(4, "big")).digest()
        vals.extend((b - 128) / 128.0 for b in h)
        counter += 1
    v = vals[:dim]
    mag = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / mag for x in v]


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_singleton: Embedder | None = None
_singleton_attempted: bool = False


def get_embedder() -> Embedder | None:
    """Return the process-level Embedder singleton (lazy-init on first call)."""
    global _singleton, _singleton_attempted
    if not _singleton_attempted:
        _singleton = Embedder.create()
        _singleton_attempted = True
    return _singleton


def reset_embedder() -> None:
    """Reset the singleton — for use in tests only."""
    global _singleton, _singleton_attempted
    _singleton = None
    _singleton_attempted = False
