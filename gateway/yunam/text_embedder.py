"""Pluggable text embedder.

Voyage is paid; Jina v3 has a free tier and 1024-dim outputs that drop into
the existing vec0 tables unchanged. This module exposes a thin Protocol so
consumers (memory, curation, scorer, orchestrator) can swap between them
without code changes.

`embed_query` and `embed_text_document` are the two methods every text path
calls. Voyage's existing `VoyageEmbedder` already provides them; the new
`JinaTextEmbedder` here implements the same surface against the Jina API.

Multimodal (file) embeddings stay on Voyage — Jina v3 doesn't accept images.
`AttachmentTools` keeps taking a Voyage embedder, which is unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

from .usage import UsageRecorder

logger = logging.getLogger("yunam.text_embedder")

# Jina v3 is multilingual (incl Korean), 1024-dim by default — same as our
# vec0 schema, so no migration needed when switching.
JINA_MODEL = "jina-embeddings-v3"
JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"
JINA_DIM = 1024
JINA_TIMEOUT_S = 15.0


class TextEmbedder(Protocol):
    """Subset of VoyageEmbedder that text-only consumers depend on."""

    async def embed_query(self, text: str) -> list[float]: ...  # noqa: E704
    async def embed_text_document(self, text: str) -> list[float]: ...  # noqa: E704


class TextEmbeddingError(Exception):
    """Raised when an embedding call fails in a way the caller should surface."""


class JinaTextEmbedder:
    """Jina v3 text embeddings — free-tier-friendly drop-in for Voyage.

    Task vocabulary:
      - `retrieval.query` for one-shot queries (memory recall, curation search).
      - `retrieval.passage` for documents we're indexing (message turns,
        curated items, interest anchors). Asymmetric task settings give Jina
        a noticeable quality bump over a single task type.
    """

    def __init__(
        self,
        *,
        api_key: str,
        usage_recorder: UsageRecorder | None = None,
        timeout_s: float = JINA_TIMEOUT_S,
    ):
        if not api_key:
            raise ValueError(
                "JinaTextEmbedder requires JINA_API_KEY — get one at https://jina.ai/embeddings"
            )
        self._api_key = api_key
        self._usage = usage_recorder
        self._timeout_s = timeout_s

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(text, task="retrieval.query")

    async def embed_text_document(self, text: str) -> list[float]:
        return await self._embed(text, task="retrieval.passage")

    async def _embed(self, text: str, *, task: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            raise TextEmbeddingError("text is required")
        payload = {
            "model": JINA_MODEL,
            "task": task,
            "dimensions": JINA_DIM,
            "input": [text],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        t0 = time.monotonic()
        status = "ok"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                r = await client.post(JINA_EMBED_URL, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            status = "error"
            self._record(t0, status, tokens=0)
            raise TextEmbeddingError(f"jina embed failed: {e}") from e

        try:
            vec = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as e:
            status = "error"
            self._record(t0, status, tokens=0)
            raise TextEmbeddingError(
                f"jina embed: unexpected response shape: {data!r}"
            ) from e

        # Record cost — Jina free tier; per-token cost is currently $0
        # in our rates table, so this lands as a units=1 audit row.
        tokens = int(data.get("usage", {}).get("total_tokens", 0) or 0)
        self._record(t0, status, tokens=tokens)

        if len(vec) != JINA_DIM:
            raise TextEmbeddingError(
                f"jina embed: expected {JINA_DIM}-dim vector, got {len(vec)}"
            )
        return [float(v) for v in vec]

    def _record(self, t0: float, status: str, *, tokens: int) -> None:
        if self._usage is None:
            return
        self._usage.record_rest(
            provider="jina",
            endpoint="embeddings",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            status=status,
            units=max(1, tokens) if tokens else 1,
        )


__all__ = [
    "TextEmbedder",
    "TextEmbeddingError",
    "JinaTextEmbedder",
    "JINA_MODEL",
    "JINA_DIM",
]
