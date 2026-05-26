"""Daily 21:00 newsletter builder.

Reads every digest-routed item fetched since the previous newsletter, groups
them by `matched_interest`, formats a single Telegram-friendly message, marks
them digested, and returns the rendered text. The caller pushes via PTBSender.

We don't ask Claude to rewrite the bundle — the per-item Haiku summaries are
already there, and rewriting them would (a) cost more tokens and (b) lose
the source attribution we want to preserve. Just lay them out.

Format: each item is a single-line bullet (number + title + em-dash + one-line
summary). URLs are grouped at the end in a flat numbered list so the body stays
scannable.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..sessions import CuratedItem, SessionStore

logger = logging.getLogger("yunam.runners.digester")

MAX_ITEMS_PER_SECTION = 6
MAX_ITEMS_TOTAL = 25
SUMMARY_LINE_CAP = 90


class Digester:
    def __init__(self, store: SessionStore):
        self._store = store

    async def build_newsletter(
        self, *, lookback_hours: int = 24
    ) -> tuple[str, list[int]]:
        """Render the newsletter and return (text, item_ids_consumed).

        Caller sends the text via PTBSender, then calls
        `store.mark_curated_digested(item_ids)` after a successful send.
        """
        since = (
            datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        ).isoformat()
        items = await self._store.list_pending_digest_items(since)
        if not items:
            return "", []

        capped = items[:MAX_ITEMS_TOTAL]

        grouped: dict[str, list[CuratedItem]] = defaultdict(list)
        for item in capped:
            label = item.matched_interest or "기타"
            grouped[label].append(item)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        header = f"📰 오늘({date_str}) 뉴스레터 — {len(items)}건"

        sections: list[str] = []
        link_lines: list[str] = []
        consumed: list[int] = []
        counter = 0
        for label in sorted(grouped.keys()):
            section_items = grouped[label][:MAX_ITEMS_PER_SECTION]
            lines = [f"[{label}]"]
            for item in section_items:
                counter += 1
                lines.append(_format_bullet(counter, item))
                link_lines.append(f"{counter}. {item.url}")
                consumed.append(int(item.id))
            sections.append("\n".join(lines))

        body = "\n\n".join(sections)
        links_block = "🔗 링크\n" + "\n".join(link_lines)
        text = f"{header}\n\n{body}\n\n────────\n{links_block}"
        return text, consumed


def _format_bullet(index: int, item: CuratedItem) -> str:
    title = item.title.strip()
    summary = (item.summary or "").strip().replace("\n", " ")
    if len(summary) > SUMMARY_LINE_CAP:
        summary = summary[: SUMMARY_LINE_CAP - 1] + "…"
    if summary:
        return f"{index}. {title} — {summary}"
    return f"{index}. {title}"
