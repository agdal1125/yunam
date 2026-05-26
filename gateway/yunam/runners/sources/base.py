"""Shared types for source adapters.

`CuratedCandidate` is what every source emits — the curator turns these into
`curated_items` rows after dedup. `FeedSource` is the Protocol every adapter
implements; the curator only depends on this surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CuratedCandidate:
    """One candidate item from a source.

    `external_id` should be stable across re-fetches of the same item from the
    same source (URL hash, news_id, status_id, …) — it pairs with `source` for
    the dedup UNIQUE constraint on `curated_items`.

    `raw_excerpt` is the source-provided snippet (Naver `description`, RSS
    `summary`, Toss `body`, etc.). The summarizer reads this; the scorer reads
    `title + raw_excerpt` to embed.
    """

    source: str
    external_id: str
    url: str
    title: str
    raw_excerpt: str | None = None


class FeedSource(Protocol):
    """Async fetcher. Implementations should be tolerant of empty responses."""

    name: str

    async def fetch(self) -> list[CuratedCandidate]:
        ...
