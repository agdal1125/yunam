"""Toss Invest news feed — internal JSON API first, playwright fallback.

The public page at https://www.tossinvest.com/feed/news is a Next.js client app
backed by Toss's own JSON API. The default `YUNAM_TOSS_NEWS_URL` points at the
common pattern (`wts-info-api.tossinvest.com/api/v2/feed/news`) — if Toss
changes the path you can override via env without code change.

The expected response shape is an envelope with a list of news objects under
`result.body` or `data`. We try a few common keys before giving up — when none
match, the source logs a noisy WARNING and returns [] so curator runs continue.

Playwright fallback (`mode='playwright'`) is intentionally stubbed: spinning
a Chromium sidecar is real infra on a 2GB VPS. Set up that container, then
flip `YUNAM_TOSS_FETCH_MODE=playwright` and implement `_fetch_playwright`.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Final

import httpx

from ...usage import UsageRecorder
from .base import CuratedCandidate, FeedSource

logger = logging.getLogger("yunam.runners.sources.toss")

DEFAULT_TIMEOUT_S: Final = 10.0
USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ITEM_KEYS = ("body", "items", "data", "list", "newsList")


class TossInvestSource:
    name = "toss"

    def __init__(
        self,
        *,
        mode: str = "api",
        api_url: str = "https://wts-info-api.tossinvest.com/api/v2/feed/news",
        usage_recorder: UsageRecorder | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._mode = mode.lower()
        self._url = api_url
        self._usage = usage_recorder
        self._timeout_s = timeout_s

    async def fetch(self) -> list[CuratedCandidate]:
        if self._mode == "disabled":
            return []
        if self._mode == "playwright":
            return await self._fetch_playwright()
        return await self._fetch_api()

    async def _fetch_api(self) -> list[CuratedCandidate]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.tossinvest.com/feed/news",
            "Accept": "application/json",
        }
        t0 = time.monotonic()
        status = "ok"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, follow_redirects=True
            ) as client:
                r = await client.get(self._url, headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception:
            status = "error"
            if self._usage is not None:
                self._usage.record_rest(
                    provider="toss",
                    endpoint="feed/news",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    status=status,
                )
            raise
        if self._usage is not None:
            self._usage.record_rest(
                provider="toss",
                endpoint="feed/news",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                status=status,
            )

        items = _extract_items(data)
        if not items:
            logger.warning(
                "toss fetch: no items found at any of %s (keys present: %s)",
                ITEM_KEYS,
                list(data.keys()) if isinstance(data, dict) else "(not a dict)",
            )
            return []

        out: list[CuratedCandidate] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            url = _pick_url(it)
            title = _pick_title(it)
            if not url or not title:
                continue
            excerpt = _pick_excerpt(it)
            external_id = _pick_id(it, url)
            out.append(
                CuratedCandidate(
                    source=self.name,
                    external_id=external_id,
                    url=url,
                    title=title,
                    raw_excerpt=excerpt,
                )
            )
        return out

    async def _fetch_playwright(self) -> list[CuratedCandidate]:
        # Stub — requires a Chromium sidecar. See `docs/curation-setup.md`
        # (to be added when the sidecar lands). Returning [] keeps the curator
        # ticking without warning spam.
        logger.warning(
            "toss source: playwright mode requested but not implemented yet "
            "— wire a Chromium sidecar before enabling. Returning 0 items."
        )
        return []


def _extract_items(data: Any) -> list[Any]:
    """Best-effort traversal of Toss-style envelopes.

    Real responses we've seen:
      {'result': {'body': [...]}}        ← common WTS shape
      {'data': {'list': [...]}}          ← alternate
      {'items': [...]}                   ← if the endpoint goes flat
    """
    if not isinstance(data, dict):
        return []
    # Strip wrappers: `result`, `data`, then the actual key
    for wrapper in (None, "result", "data"):
        bucket: Any = data
        if wrapper is not None:
            bucket = data.get(wrapper)
            if not isinstance(bucket, dict):
                continue
        for key in ITEM_KEYS:
            value = bucket.get(key)
            if isinstance(value, list) and value:
                return value
    return []


def _pick_url(item: dict[str, Any]) -> str:
    for key in ("url", "newsUrl", "linkUrl", "link"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value.strip()
    # Toss internal ids sometimes get rendered as relative paths — promote
    nid = item.get("id") or item.get("newsId") or item.get("articleId")
    if nid is not None:
        return f"https://www.tossinvest.com/feed/news/{nid}"
    return ""


def _pick_title(item: dict[str, Any]) -> str:
    for key in ("title", "newsTitle", "headline"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_excerpt(item: dict[str, Any]) -> str | None:
    for key in ("summary", "description", "body", "content", "newsContent"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_id(item: dict[str, Any], url: str) -> str:
    nid = item.get("id") or item.get("newsId") or item.get("articleId")
    if nid is not None:
        return f"t{nid}"
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
