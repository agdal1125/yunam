"""Generic RSS / Atom adapter — fan-out across configured feeds.

`feedparser` is permissive about format quirks; we trust it to handle both
RSS 2.0 and Atom. Each entry contributes one `CuratedCandidate`. The `source`
label is `rss:<host>` so the curator's audit/UI can tell feeds apart.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from typing import Final
from urllib.parse import urlparse

import httpx

from ...usage import UsageRecorder
from .base import CuratedCandidate, FeedSource

logger = logging.getLogger("yunam.runners.sources.rss")

DEFAULT_TIMEOUT_S: Final = 10.0
USER_AGENT: Final = (
    "Mozilla/5.0 (compatible; yunam-curation/0.1; +https://github.com/agdal1125/yunam)"
)
MAX_ENTRIES_PER_FEED: Final = 20


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _host_label(url: str) -> str:
    host = urlparse(url).hostname or "unknown"
    return host.lower().lstrip("www.")


class RssGenericSource:
    """Generic RSS / Atom source with optional tier-divisor skipping.

    `tier_divisor=1` fetches every tick. `tier_divisor=2` fetches every 2nd
    tick (cadence offset to tick 1). `tier_divisor=4` fetches every 4th. The
    counter is per-instance state, so creating three RssGenericSource
    instances with divisors 1/2/4 gives you a natural tiered cadence without
    needing the curator to know about tiers.

    `tick 1` always fetches (so first-tick coverage isn't gated by tier).
    """

    def __init__(
        self,
        *,
        feeds: tuple[str, ...],
        usage_recorder: UsageRecorder | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        tier_divisor: int = 1,
        tier_label: str = "rss",
    ):
        self._feeds = feeds
        self._usage = usage_recorder
        self._timeout_s = timeout_s
        self._tier_divisor = max(1, int(tier_divisor))
        self._tick_counter = 0
        # Each instance shows up under its own label in audit logs / dedup —
        # 'rss-high', 'rss-mid', 'rss-low' all coexist as distinct sources.
        self.name = tier_label

    async def fetch(self) -> list[CuratedCandidate]:
        if not self._feeds:
            return []
        self._tick_counter += 1
        if (self._tick_counter - 1) % self._tier_divisor != 0:
            logger.debug(
                "%s: skipping tick %d (divisor=%d)",
                self.name,
                self._tick_counter,
                self._tier_divisor,
            )
            return []
        out: list[CuratedCandidate] = []
        async with httpx.AsyncClient(
            timeout=self._timeout_s,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
        ) as client:
            for feed_url in self._feeds:
                try:
                    out.extend(await self._fetch_one(client, feed_url))
                except Exception:
                    logger.exception("rss fetch failed url=%s", feed_url)
        return out

    async def _fetch_one(
        self, client: httpx.AsyncClient, feed_url: str
    ) -> list[CuratedCandidate]:
        host = _host_label(feed_url)
        t0 = time.monotonic()
        status = "ok"
        try:
            r = await client.get(feed_url)
            r.raise_for_status()
            body = r.text
        except Exception:
            status = "error"
            if self._usage is not None:
                self._usage.record_rest(
                    provider=f"rss:{host}",
                    endpoint="feed",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    status=status,
                )
            raise
        if self._usage is not None:
            self._usage.record_rest(
                provider=f"rss:{host}",
                endpoint="feed",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                status=status,
            )

        # feedparser is sync + does its own I/O if given a URL; we hand it the
        # body we already fetched (lets us tracker per-call latency cleanly).
        parsed = await asyncio.to_thread(_parse_feed_body, body)
        entries = (parsed.get("entries") or [])[:MAX_ENTRIES_PER_FEED]
        out: list[CuratedCandidate] = []
        for entry in entries:
            url = (entry.get("link") or "").strip()
            title = _strip_html(entry.get("title", ""))
            if not url or not title:
                continue
            summary = _strip_html(
                entry.get("summary", "") or entry.get("description", "")
            ) or None
            # Prefer guid; fall back to URL hash if absent. Keep within
            # external_id reasonable length.
            guid = (entry.get("id") or entry.get("guid") or url)
            external_id = hashlib.sha256(
                f"{host}|{guid}".encode("utf-8")
            ).hexdigest()[:24]
            out.append(
                CuratedCandidate(
                    source=f"rss:{host}",
                    external_id=external_id,
                    url=url,
                    title=title,
                    raw_excerpt=summary,
                )
            )
        return out


def _parse_feed_body(body: str) -> dict:
    """Thread-only wrapper around feedparser.parse for one feed body."""
    import feedparser  # lazy import: feedparser is curation-only

    return feedparser.parse(body)
