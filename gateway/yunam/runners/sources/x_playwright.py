"""X (Twitter) source — logged-in scraping via playwright (STUB).

The plan (per milestone.md §1 + user's 2026-05-23 decision):
  - dedicated burner X account, NOT the user's primary
  - shared Chromium sidecar container (also used by Toss playwright fallback)
  - saved-cookie auth (re-login only when cookie expires)
  - circuit-breaker so X breakage doesn't take down the curator

None of the infra is up yet. This module is the *interface* — it satisfies the
`FeedSource` Protocol so the curator's fan-out doesn't need to special-case it,
and it returns [] until you wire the sidecar.

To enable later:
  1. Add a `chromium` sibling service to docker-compose.yml
  2. Implement the playwright session: load cookies → navigate to
     https://x.com/home (or /<handle>) → scroll a fixed number of times →
     scrape tweet cards → return as CuratedCandidate (source='x:<handle>')
  3. Add `YUNAM_X_COOKIE_PATH` and document cookie export in .env.example
  4. Flip `YUNAM_X_ENABLED=true`
"""

from __future__ import annotations

import logging

from .base import CuratedCandidate, FeedSource

logger = logging.getLogger("yunam.runners.sources.x")

_WARNED_ONCE = False


class XPlaywrightSource:
    name = "x"

    def __init__(self, *, handles: tuple[str, ...] = (), enabled: bool = False):
        self._handles = handles
        self._enabled = enabled

    async def fetch(self) -> list[CuratedCandidate]:
        global _WARNED_ONCE
        if not self._enabled:
            return []
        if not _WARNED_ONCE:
            logger.warning(
                "X source enabled (handles=%s) but playwright sidecar isn't "
                "implemented yet — set up Chromium + saved-cookie auth before "
                "expecting items. Returning 0 candidates for now.",
                list(self._handles) or "(home timeline)",
            )
            _WARNED_ONCE = True
        return []
