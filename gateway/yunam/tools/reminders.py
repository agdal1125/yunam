"""ReminderTools — primitives for scheduling and cancelling proactive nudges.

Schedules live in `scheduled_nudges` in the session DB; a sweeper coroutine
(see `scheduler.run_nudge_sweeper`) polls for due rows and dispatches them
as Telegram messages. This module is the tool surface the model uses to
*create* those rows, and nothing more — dispatching is out of scope here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..sessions import SessionStore
from ..tools.vault import VaultError

logger = logging.getLogger("yunam.tools.reminders")

# Accept absolute timestamps in either "YYYY-MM-DD HH:MM" or ISO 8601.
# Ambiguous formats (just "tomorrow") are the model's responsibility to
# resolve using the [meta: now is ...] tag — we don't parse relative words.
_ABSOLUTE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

# Safety caps on scheduling — a missed cap means the tool call fails cleanly
# rather than silently swallowing a malformed timestamp.
MIN_HORIZON = timedelta(minutes=1)
MAX_HORIZON = timedelta(days=365)
MAX_MESSAGE_LEN = 1000


class ReminderTools:
    def __init__(self, store: SessionStore, timezone_name: str):
        self._store = store
        self._tz = ZoneInfo(timezone_name)

    async def schedule_reminder(
        self, chat_id: int, fire_at: str, message: str
    ) -> str:
        """Schedule a reminder. `fire_at` is parsed as local time in self._tz.

        Returns a short confirmation string the model can surface to jaekeun.
        Raises `VaultError` on bad input so the orchestrator renders it as a
        tool error rather than a 500.
        """
        if not isinstance(fire_at, str) or not fire_at.strip():
            raise VaultError("fire_at must be a non-empty string")
        if not isinstance(message, str) or not message.strip():
            raise VaultError("message must be a non-empty string")
        if len(message) > MAX_MESSAGE_LEN:
            raise VaultError(
                f"message too long ({len(message)} chars; max {MAX_MESSAGE_LEN})"
            )

        fire_at_local = self._parse_local(fire_at.strip())
        fire_at_utc = fire_at_local.astimezone(timezone.utc)

        horizon = fire_at_utc - datetime.now(timezone.utc)
        if horizon < MIN_HORIZON:
            raise VaultError(
                "fire_at must be at least 1 minute in the future"
            )
        if horizon > MAX_HORIZON:
            raise VaultError("fire_at is more than a year out — sanity-check the date")

        nudge_id = await self._store.add_nudge(
            chat_id=chat_id,
            fire_at_iso_utc=fire_at_utc.isoformat(),
            message=message.strip(),
        )
        local_display = fire_at_local.strftime("%Y-%m-%d %H:%M %Z")
        logger.info(
            "nudge scheduled id=%s chat_id=%s fire_at=%s msg_len=%d",
            nudge_id, chat_id, fire_at_utc.isoformat(), len(message),
        )
        return f"reminder scheduled (id {nudge_id}) for {local_display}"

    async def list_reminders(self, chat_id: int) -> str:
        """Return pending reminders for this chat as plain text (one per line)."""
        nudges = await self._store.list_pending_nudges(chat_id)
        if not nudges:
            return "no pending reminders."
        lines = []
        for n in nudges:
            local = self._to_local_display(n.fire_at)
            preview = n.message if len(n.message) <= 80 else n.message[:77] + "..."
            lines.append(f"{n.id}: {local} — {preview}")
        return "\n".join(lines)

    async def cancel_reminder(self, chat_id: int, nudge_id: int) -> str:
        if not isinstance(nudge_id, int) or nudge_id <= 0:
            raise VaultError("nudge_id must be a positive integer")
        cancelled = await self._store.cancel_nudge(nudge_id, chat_id)
        if cancelled:
            return f"cancelled reminder {nudge_id}."
        return (
            f"no pending reminder with id {nudge_id} "
            "(already fired, cancelled, or doesn't exist)."
        )

    def _parse_local(self, value: str) -> datetime:
        # ISO with timezone offset wins — respect whatever the model specified.
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
            return parsed.replace(tzinfo=self._tz)
        except ValueError:
            pass
        for fmt in _ABSOLUTE_FORMATS:
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=self._tz)
            except ValueError:
                continue
        raise VaultError(
            f"could not parse fire_at={value!r}. "
            "Use 'YYYY-MM-DD HH:MM' (local time) or ISO 8601."
        )

    def _to_local_display(self, iso_utc: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_utc)
        except ValueError:
            return iso_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._tz).strftime("%Y-%m-%d %H:%M %Z")
