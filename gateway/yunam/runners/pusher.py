"""Proactive Telegram push for URGENT curated items and the daily newsletter.

The pusher wraps PTBSender (already wired into main.py) plus the session
store, so a successful send is followed by an audit write (`pushed_at` or
`digested_at`) and a `record_proactive_message` so the next conversation turn
sees the push in history.

Failures inside `send_message` are logged and swallowed — a failed urgent
push should NOT mark the row pushed, so the next tick can retry (idempotent
because the `pushed_at IS NULL` guard in `mark_curated_pushed`).
"""

from __future__ import annotations

import logging

from ..sessions import CuratedItem, SessionStore

logger = logging.getLogger("yunam.runners.pusher")

TELEGRAM_MSG_LIMIT = 4096


class CurationPusher:
    def __init__(self, bot, store: SessionStore, *, owner_chat_id: int):
        self._bot = bot
        self._store = store
        self._chat_id = owner_chat_id

    async def push_urgent(self, item: CuratedItem) -> bool:
        """Send a single urgent item. Returns True on success."""
        text = _format_urgent(item)
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text[:TELEGRAM_MSG_LIMIT]
            )
        except Exception:
            logger.exception(
                "urgent push failed item_id=%s url=%s", item.id, item.url
            )
            return False
        try:
            await self._store.record_proactive_message(self._chat_id, text)
            await self._store.mark_curated_pushed(item.id)
        except Exception:
            logger.exception(
                "urgent push: bookkeeping failed item_id=%s", item.id
            )
        return True

    async def push_newsletter(self, text: str, item_ids: list[int]) -> bool:
        if not text or not item_ids:
            return False
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text[:TELEGRAM_MSG_LIMIT]
            )
        except Exception:
            logger.exception("newsletter push failed (%d items)", len(item_ids))
            return False
        try:
            await self._store.record_proactive_message(self._chat_id, text)
            await self._store.mark_curated_digested(item_ids)
        except Exception:
            logger.exception("newsletter bookkeeping failed")
        return True


URGENT_SUMMARY_CAP = 120


def _format_urgent(item: CuratedItem) -> str:
    label = item.matched_interest or "긴급"
    summary = (item.summary or "").strip().replace("\n", " ")
    if len(summary) > URGENT_SUMMARY_CAP:
        summary = summary[: URGENT_SUMMARY_CAP - 1] + "…"
    head = f"🚨 [{label}] {item.title}"
    if summary:
        head += f" — {summary}"
    return f"{head}\n\n{item.url}"
