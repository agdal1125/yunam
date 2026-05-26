"""Naver Search OpenAPI — news endpoint.

Documented at https://developers.naver.com/docs/serviceapi/search/news/news.md.
Requires CLIENT_ID + CLIENT_SECRET (free, generous quota). One query per call,
sorted by `date` (recent first). We fan out across configured queries and
union the results; dedup happens at the curator's `insert_curated_item` step.

Why not use the web skill: this is a structured JSON API the curator
specifically depends on (titles, links, descriptions, pubDate), and we want
explicit per-request usage tracking attributed to the curator's skill.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from typing import Final

import httpx

from ...usage import UsageRecorder
from .base import CuratedCandidate, FeedSource

logger = logging.getLogger("yunam.runners.sources.naver")

ENDPOINT: Final = "https://openapi.naver.com/v1/search/news.json"
DEFAULT_TIMEOUT_S: Final = 10.0
DEFAULT_DISPLAY: Final = 10  # max items per query call (API allows up to 100)


def _strip_html(text: str) -> str:
    """Naver returns titles/descriptions with <b> tags + HTML entities."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class NaverNewsSource:
    name = "naver"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        queries: tuple[str, ...],
        usage_recorder: UsageRecorder | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._queries = queries
        self._usage = usage_recorder
        self._timeout_s = timeout_s

    async def fetch(self) -> list[CuratedCandidate]:
        if not self._queries:
            return []
        candidates: list[CuratedCandidate] = []
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for query in self._queries:
                try:
                    candidates.extend(await self._fetch_one(client, query))
                except Exception:
                    logger.exception("naver fetch failed for query=%r", query)
        return candidates

    async def _fetch_one(
        self, client: httpx.AsyncClient, query: str
    ) -> list[CuratedCandidate]:
        headers = {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
        }
        params = {"query": query, "display": DEFAULT_DISPLAY, "sort": "date"}
        t0 = time.monotonic()
        status = "ok"
        try:
            r = await client.get(ENDPOINT, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception:
            status = "error"
            raise
        finally:
            if self._usage is not None:
                self._usage.record_rest(
                    provider="naver",
                    endpoint="search/news",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    status=status,
                )

        items = data.get("items") or []
        out: list[CuratedCandidate] = []
        for it in items:
            title = _strip_html(it.get("title", ""))
            desc = _strip_html(it.get("description", ""))
            url = (it.get("link") or it.get("originallink") or "").strip()
            if not url or not title:
                continue
            # Prefer original publisher URL (originallink) when present; both
            # land under naver:<hash> for dedup.
            external_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            out.append(
                CuratedCandidate(
                    source=self.name,
                    external_id=external_id,
                    url=url,
                    title=title,
                    raw_excerpt=desc or None,
                )
            )
        return out
