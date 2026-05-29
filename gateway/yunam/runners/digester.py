"""Daily newsletter builder.

Reads every digest-routed item fetched since the previous newsletter, picks
the top-N by score within each category, and lays them out in a compact
Telegram-friendly format.

Design rules (don't drift from these without a reason):
  - One line per item: "• title (url)". Telegram will auto-unfurl ONLY the
    first URL in the message, so we don't get N preview cards. The title
    is what the user reads; the URL is the click target.
  - No per-item summary line. The Haiku summary is duplicative once you've
    got the article title + Telegram preview card. Skipping it cuts the
    message length ~3x and removes the "..." truncations the user
    complained about.
  - No separate "🔗 링크" block at the bottom. URLs are inline; the block
    just bloated the message.
  - Cap at TOTAL_CAP items overall, PER_SECTION_CAP per category. Items
    are sorted by score within a section so the strongest signals win the
    finite slot budget.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..sessions import CuratedItem, SessionStore

logger = logging.getLogger("yunam.runners.digester")

PER_SECTION_CAP = 4
TOTAL_CAP = 15
TITLE_CAP = 120


class Digester:
    def __init__(self, store: SessionStore):
        self._store = store

    async def build_newsletter(
        self, *, lookback_hours: int = 24
    ) -> tuple[str, list[int]]:
        """Render the newsletter and return (text, item_ids_consumed)."""
        since = (
            datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        ).isoformat()
        items = await self._store.list_pending_digest_items(since)
        if not items:
            return "", []

        # list_pending_digest_items already orders by score DESC. Bucket by
        # category, keep the top PER_SECTION_CAP within each, then take the
        # leading TOTAL_CAP across sections.
        grouped: dict[str, list[CuratedItem]] = defaultdict(list)
        for item in items:
            label = (item.matched_interest or "기타").strip()
            if len(grouped[label]) < PER_SECTION_CAP:
                grouped[label].append(item)

        selected: list[CuratedItem] = []
        for label in sorted(grouped.keys()):
            for it in grouped[label]:
                selected.append(it)
                if len(selected) >= TOTAL_CAP:
                    break
            if len(selected) >= TOTAL_CAP:
                break

        # Re-bucket the kept items so section ordering matches selection.
        kept: dict[str, list[CuratedItem]] = defaultdict(list)
        for it in selected:
            kept[(it.matched_interest or "기타").strip()].append(it)

        date_str = datetime.now(timezone.utc).strftime("%m/%d")
        header = f"📰 {date_str} 뉴스 다이제스트 ({len(selected)}건)"

        sections: list[str] = []
        consumed: list[int] = []
        for label in sorted(kept.keys()):
            lines = [f"<{label}>"]
            for item in kept[label]:
                lines.append(_format_bullet(item))
                consumed.append(int(item.id))
            sections.append("\n".join(lines))

        body = "\n\n".join(sections)
        text = f"{header}\n\n{body}"
        return text, consumed


def _format_bullet(item: CuratedItem) -> str:
    """One-liner per item: `• title  url`.

    Telegram-friendly: single line so the message stays scannable; URL
    is the only click target. Title is hard-trimmed to TITLE_CAP so a
    single ranty headline can't push everything else off-screen.
    """
    title = (item.title or "").strip().replace("\n", " ")
    if len(title) > TITLE_CAP:
        title = title[: TITLE_CAP - 1] + "…"
    return f"• {title}\n  {item.url}"
