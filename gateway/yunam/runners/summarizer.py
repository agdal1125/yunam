"""Haiku-backed summarizer for curated items.

One Anthropic call per item — cheap, parallelizable. The summarizer is best-
effort: a failure returns `None` so the curator can still route the item by
title+excerpt alone. Cost is recorded through UsageRecorder so the curation
spend shows up under skill_id='curation' in `usage_breakdown`.

Why not batch all items into one call: latency vs. cache hit rate. Each item
is small, and a per-item call keeps a single failed item from poisoning the
whole batch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..usage import UsageRecorder

logger = logging.getLogger("yunam.runners.summarizer")

SUMMARIZER_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 200
DEFAULT_MAX_PARALLEL = 4

SUMMARIZER_SYSTEM = (
    "You are a terse Korean-news summarizer for an investor's daily briefing. "
    "Reply with 2-3 plain prose sentences in Korean (no markdown, no bullets) "
    "covering: what happened, the key number or actor, and any forward "
    "implication. Skip filler ('이는~ 평가된다', '주목된다'). If the source "
    "snippet is too thin to summarize honestly, return exactly: SKIP."
)


class Summarizer:
    def __init__(
        self,
        client: Any,
        *,
        usage_recorder: UsageRecorder | None = None,
        model: str = SUMMARIZER_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        skill_id: str = "curation",
    ):
        self._client = client
        self._usage = usage_recorder
        self._model = model
        self._max_tokens = max_tokens
        self._sem = asyncio.Semaphore(max(1, max_parallel))
        self._skill_id = skill_id

    async def summarize(self, *, title: str, raw_excerpt: str | None) -> str | None:
        """Return a 2-3 line summary or None when the model decides to skip."""
        body = (raw_excerpt or "").strip()
        if not title and not body:
            return None
        user_text = f"제목: {title}\n\n본문:\n{body[:2000] if body else '(본문 없음)'}"
        async with self._sem:
            t0 = time.monotonic()
            status = "ok"
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=SUMMARIZER_SYSTEM,
                    messages=[{"role": "user", "content": user_text}],
                )
            except Exception:
                status = "error"
                if self._usage is not None:
                    self._usage.record_anthropic(
                        model=self._model,
                        usage=None,
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                        status=status,
                        skill_id=self._skill_id,
                    )
                logger.warning("summarizer failed for %r", title[:60], exc_info=True)
                return None
            usage = getattr(response, "usage", None)
            if self._usage is not None:
                self._usage.record_anthropic(
                    model=self._model,
                    usage=usage,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    status=status,
                    skill_id=self._skill_id,
                )

        text = _extract_text(response)
        if not text:
            return None
        if text.strip().upper().startswith("SKIP"):
            return None
        # Defensive: cap length to keep the digest budget tight.
        return text.strip()[:600] or None


def _extract_text(response: Any) -> str:
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
    return "".join(parts).strip()
