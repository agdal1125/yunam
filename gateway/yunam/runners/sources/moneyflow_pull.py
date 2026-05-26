"""Moneyflow pull — invoke Stock-Agent MCP to surface today's supply/demand hot tickers.

The Stock MCP is already wired into Yunam's tool surface for in-conversation
queries. This source *reuses* the same client to produce curated items at
tick time, so the agent doesn't have to call analyze_supply itself every
turn just to know "anything notable in flows today".

The MCP returns a free-text analysis (FastMCP serializes pandas tables as
text). We treat that text as the `raw_excerpt`; the summarizer / scorer
handle it like any other source. `external_id` is `moneyflow:<YYYYMMDD>` so
re-invoking the source on the same day is a dedup no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .base import CuratedCandidate, FeedSource

logger = logging.getLogger("yunam.runners.sources.moneyflow")


class _StockClientLike:
    """Protocol-shape for the bits of StockMCPClient we use here."""

    tools: tuple[dict[str, Any], ...]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        ...


class MoneyflowSource:
    name = "moneyflow"

    def __init__(
        self,
        stock_client: _StockClientLike | None,
        *,
        timezone_name: str = "Asia/Seoul",
        enabled: bool = True,
    ):
        self._client = stock_client
        self._tz = ZoneInfo(timezone_name)
        self._enabled = enabled

    async def fetch(self) -> list[CuratedCandidate]:
        if not self._enabled or self._client is None:
            return []
        # Prefer `analyze_supply` (today's snapshot). If the MCP server exposes
        # something different, this guard means we just no-op rather than
        # crash the tick.
        tool_names = {t.get("name") for t in self._client.tools}
        if "analyze_supply" not in tool_names:
            logger.info(
                "moneyflow source: 'analyze_supply' not exposed by stock MCP "
                "(available: %s); skipping",
                sorted(tool_names),
            )
            return []
        try:
            # Default top_n=100 + sync KRX calls @ 0.8s sleep each makes
            # analyze_supply long enough to starve the FastMCP event loop and
            # drop the SSE stream. 30 keeps the snapshot useful for curation
            # while letting the call complete inside the keep-alive window.
            text = await self._client.call_tool(
                "analyze_supply", {"top_n": 30}
            )
        except Exception:
            logger.exception("moneyflow source: analyze_supply call failed")
            return []
        if not text or not text.strip():
            return []

        today_local = datetime.now(self._tz).strftime("%Y%m%d")
        external_id = f"moneyflow:{today_local}"
        return [
            CuratedCandidate(
                source=self.name,
                external_id=external_id,
                url=f"yunam://stock/moneyflow/{today_local}",
                title=f"Moneyflow snapshot — {today_local}",
                raw_excerpt=text.strip(),
            )
        ]
