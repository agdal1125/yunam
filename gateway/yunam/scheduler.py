"""Daily retrospective scheduler.

One `asyncio` task that sleeps until the next local fire time (HH:MM in
`timezone`), then sends a proactive Telegram prompt and records it in the
session store so the next user reply sees the prompt as prior context.

Intentionally dependency-free (no APScheduler): one coroutine, one sleep, one
`stop_event` to unblock on shutdown. Simpler to reason about for a single
cron-style job and avoids dragging in a timer subsystem for one trigger.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger("yunam.scheduler")


OnFire = Callable[[int, str], Awaitable[None]]


def _seconds_until_next_fire(now: datetime, hour: int, minute: int) -> tuple[datetime, float]:
    """Return (next_fire_local, seconds_to_sleep)."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target, (target - now).total_seconds()


async def run_daily_scheduler(
    chat_id: int,
    hour: int,
    minute: int,
    tz_name: str,
    on_fire: OnFire,
    stop_event: asyncio.Event,
) -> None:
    """Fire `on_fire(chat_id, YYYY-MM-DD)` once per day at the local wall-clock time.

    Exits promptly when `stop_event` is set (e.g. SIGTERM in main.py).
    """
    tz = ZoneInfo(tz_name)
    logger.info(
        "daily scheduler starting chat_id=%s fire_time=%02d:%02d tz=%s",
        chat_id,
        hour,
        minute,
        tz_name,
    )

    while not stop_event.is_set():
        now = datetime.now(tz)
        next_fire, sleep_s = _seconds_until_next_fire(now, hour, minute)
        logger.info(
            "daily scheduler: next fire at %s (sleeping %.0fs)",
            next_fire.isoformat(timespec="seconds"),
            sleep_s,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
            return  # stop_event set during sleep; shut down cleanly
        except asyncio.TimeoutError:
            pass  # sleep elapsed naturally → time to fire

        date_str = next_fire.strftime("%Y-%m-%d")
        try:
            await on_fire(chat_id, date_str)
            logger.info("daily scheduler: fired for %s", date_str)
        except Exception:
            # A send failure (network, Telegram outage) should not kill the loop.
            # Skip to the next day; we'll try again in ~24h.
            logger.exception("daily scheduler: on_fire failed for %s", date_str)
