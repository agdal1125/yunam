"""Scheduler coroutines for Yunam's proactive jobs.

Intentionally dependency-free (no APScheduler): each job is one coroutine
that sleeps until the next fire time, calls its callback, and repeats.
Simpler to reason about than a timer subsystem for the handful of jobs we
run (nudge sweeper for reminders).

All loops shut down promptly when their shared `stop_event` is set
(e.g. SIGTERM in main.py). Callback failures are logged and swallowed — a
network blip during one dispatch should never kill the loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

logger = logging.getLogger("yunam.scheduler")


OnSweep = Callable[[], Awaitable[None]]


async def run_nudge_sweeper(
    on_sweep: OnSweep,
    stop_event: asyncio.Event,
    interval_seconds: float = 60.0,
) -> None:
    """Poll for due reminders every `interval_seconds` and fire them.

    The caller's `on_sweep` is responsible for querying the store, sending
    messages, and marking rows sent. We just drive the cadence. Sweep errors
    are logged and swallowed — one failed tick should not kill the loop.
    Resolution is ±interval seconds, which is fine for user-facing reminders.
    """
    logger.info("nudge sweeper starting interval=%.0fs", interval_seconds)
    while not stop_event.is_set():
        try:
            await on_sweep()
        except Exception:
            logger.exception("nudge sweeper: on_sweep raised; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return
        except asyncio.TimeoutError:
            pass


def now_utc_iso() -> str:
    """Helper used by sweeper consumers to query `list_due_nudges`."""
    return datetime.now(timezone.utc).isoformat()
