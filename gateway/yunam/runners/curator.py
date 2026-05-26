"""Main curation runner — orchestrates the fetch/summarize/score/route loop.

Two concurrent coroutines share a stop_event:
  - `_tick_loop`: every N minutes, fan out to sources, summarize+score+route
                  new items, push URGENT directly.
  - `_newsletter_loop`: sleeps until the configured local time each day,
                  builds + pushes the digest, marks rows digested.

Both are tolerant of failures inside their inner steps so one broken source
doesn't tank the whole run. The curator does NOT extend the SkillRegistry —
the in-conversation read/admin surface lives in `skills/curation.py`.

ContextVars: each tick / newsletter binds `skill_id='curation'` so any
Anthropic / Voyage / REST call made downstream lands under that skill in
`api_usage`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..sessions import SessionStore
from ..usage import reset_skill_context, set_skill_context
from .digester import Digester
from .pusher import CurationPusher
from .router import DIGEST, DROP, URGENT, route
from .scorer import Scorer
from .sources.base import CuratedCandidate, FeedSource
from .summarizer import Summarizer

logger = logging.getLogger("yunam.runners.curator")


async def run_curation_loop(
    *,
    cfg: Config,
    store: SessionStore,
    sources: list[FeedSource],
    summarizer: Summarizer,
    scorer: Scorer,
    pusher: CurationPusher,
    digester: Digester,
    stop_event: asyncio.Event,
) -> None:
    """Drive both the per-tick loop and the daily newsletter loop until stop.

    Called by main.py as `asyncio.create_task(run_curation_loop(...))`.
    """
    interval_s = max(60.0, float(cfg.curation_interval_minutes) * 60.0)
    tz = ZoneInfo(cfg.timezone)

    tick_task = asyncio.create_task(
        _tick_loop(
            sources=sources,
            summarizer=summarizer,
            scorer=scorer,
            pusher=pusher,
            store=store,
            urgent_threshold=cfg.curation_urgent_threshold,
            digest_threshold=cfg.curation_digest_threshold,
            interval_seconds=interval_s,
            stop_event=stop_event,
        ),
        name="yunam-curation-tick",
    )
    newsletter_task = asyncio.create_task(
        _newsletter_loop(
            digester=digester,
            pusher=pusher,
            newsletter_time_hhmm=cfg.curation_newsletter_time,
            tz=tz,
            stop_event=stop_event,
        ),
        name="yunam-curation-newsletter",
    )

    try:
        await stop_event.wait()
    finally:
        for task in (tick_task, newsletter_task):
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "%s did not exit within 5s; cancelling", task.get_name()
                )
                task.cancel()
            except Exception:
                logger.exception("%s raised on shutdown", task.get_name())


# ---- tick loop ------------------------------------------------------------


async def _tick_loop(
    *,
    sources: list[FeedSource],
    summarizer: Summarizer,
    scorer: Scorer,
    pusher: CurationPusher,
    store: SessionStore,
    urgent_threshold: float,
    digest_threshold: float,
    interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    logger.info(
        "curation tick loop starting interval=%.0fs sources=%s",
        interval_seconds,
        [s.name for s in sources],
    )
    while not stop_event.is_set():
        token = set_skill_context("curation")
        try:
            await _run_one_tick(
                sources=sources,
                summarizer=summarizer,
                scorer=scorer,
                pusher=pusher,
                store=store,
                urgent_threshold=urgent_threshold,
                digest_threshold=digest_threshold,
            )
        except Exception:
            logger.exception("curation tick raised; continuing")
        finally:
            reset_skill_context(token)
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=interval_seconds
            )
            return
        except asyncio.TimeoutError:
            pass


async def _run_one_tick(
    *,
    sources: list[FeedSource],
    summarizer: Summarizer,
    scorer: Scorer,
    pusher: CurationPusher,
    store: SessionStore,
    urgent_threshold: float,
    digest_threshold: float,
) -> None:
    t0 = time.monotonic()
    fetched_per_source: dict[str, int] = {}
    results = await asyncio.gather(
        *(_fetch_one_source(s) for s in sources), return_exceptions=False
    )
    candidates: list[CuratedCandidate] = []
    for src, items in zip(sources, results):
        fetched_per_source[src.name] = len(items)
        candidates.extend(items)
    if not candidates:
        logger.info(
            "curation tick: 0 candidates fetched (per-source: %s)",
            fetched_per_source,
        )
        return

    now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "+00:00"
    routed_counts = {"urgent": 0, "digest": 0, "drop": 0, "dedup": 0, "error": 0}

    for candidate in candidates:
        item_id = await store.insert_curated_item(
            source=candidate.source,
            external_id=candidate.external_id,
            url=candidate.url,
            title=candidate.title,
            raw_excerpt=candidate.raw_excerpt,
            fetched_at=now_iso,
        )
        if item_id is None:
            routed_counts["dedup"] += 1
            continue
        try:
            await _process_one(
                item_id=item_id,
                candidate=candidate,
                summarizer=summarizer,
                scorer=scorer,
                pusher=pusher,
                store=store,
                urgent_threshold=urgent_threshold,
                digest_threshold=digest_threshold,
                routed_counts=routed_counts,
            )
        except Exception:
            routed_counts["error"] += 1
            logger.exception(
                "curation: per-item processing failed item_id=%s", item_id
            )

    logger.info(
        "curation tick: fetched_per_source=%s routed=%s elapsed=%.1fs",
        fetched_per_source,
        routed_counts,
        time.monotonic() - t0,
    )


async def _fetch_one_source(source: FeedSource) -> list[CuratedCandidate]:
    try:
        return await source.fetch()
    except Exception:
        logger.exception("curation: source %s.fetch() raised", source.name)
        return []


async def _process_one(
    *,
    item_id: int,
    candidate: CuratedCandidate,
    summarizer: Summarizer,
    scorer: Scorer,
    pusher: CurationPusher,
    store: SessionStore,
    urgent_threshold: float,
    digest_threshold: float,
    routed_counts: dict[str, int],
) -> None:
    summary = await summarizer.summarize(
        title=candidate.title, raw_excerpt=candidate.raw_excerpt
    )
    score_result = await scorer.score(
        title=candidate.title,
        summary_or_excerpt=summary or candidate.raw_excerpt,
    )
    decision = route(
        score=score_result.score,
        urgent_threshold=urgent_threshold,
        digest_threshold=digest_threshold,
    )
    await store.update_curated_summary_and_score(
        item_id,
        summary=summary,
        score=score_result.score,
        matched_interest=score_result.matched_interest,
        routed_as=decision,
    )
    if score_result.embedding is not None:
        await store.record_curated_item_embedding(item_id, score_result.embedding)
    routed_counts[decision] += 1
    if decision == URGENT:
        # Refetch the row to get the populated summary/score for the push body.
        items = await store.list_recent_curated_items(
            since_iso_utc=_one_hour_ago_iso(), routed_as=URGENT, limit=20
        )
        # Find our just-routed row by id.
        match = next((it for it in items if int(it.id) == int(item_id)), None)
        if match is not None and match.pushed_at is None:
            await pusher.push_urgent(match)


def _one_hour_ago_iso() -> str:
    return (datetime.utcnow() - timedelta(hours=1)).isoformat() + "+00:00"


# ---- newsletter loop ------------------------------------------------------


async def _newsletter_loop(
    *,
    digester: Digester,
    pusher: CurationPusher,
    newsletter_time_hhmm: str,
    tz: ZoneInfo,
    stop_event: asyncio.Event,
) -> None:
    hh, mm = _parse_hhmm(newsletter_time_hhmm)
    logger.info(
        "curation newsletter loop starting tz=%s fires at %02d:%02d",
        tz.key,
        hh,
        mm,
    )
    while not stop_event.is_set():
        wait_seconds = _seconds_until_next(hh, mm, tz)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            return
        except asyncio.TimeoutError:
            pass
        token = set_skill_context("curation")
        try:
            text, item_ids = await digester.build_newsletter(lookback_hours=24)
            if not text:
                logger.info("curation newsletter: nothing to send today")
            else:
                await pusher.push_newsletter(text, item_ids)
        except Exception:
            logger.exception("curation newsletter: build/push raised")
        finally:
            reset_skill_context(token)


def _parse_hhmm(value: str) -> tuple[int, int]:
    """`'21:00'` → (21, 0). Falls back to (21, 0) on bad input."""
    try:
        hh_s, mm_s = value.strip().split(":", 1)
        hh = max(0, min(23, int(hh_s)))
        mm = max(0, min(59, int(mm_s)))
        return hh, mm
    except Exception:
        logger.warning("invalid newsletter time %r; falling back to 21:00", value)
        return 21, 0


def _seconds_until_next(hh: int, mm: int, tz: ZoneInfo) -> float:
    now = datetime.now(tz)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    delta = (target - now).total_seconds()
    return max(1.0, delta)
