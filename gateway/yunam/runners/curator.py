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
import re
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..sessions import CuratedItem, SessionStore
from ..usage import reset_skill_context, set_skill_context
from .digester import Digester
from .pusher import CurationPusher
from .router import DIGEST, DROP, URGENT, route
from .scorer import Scorer
from .sources.base import CuratedCandidate, FeedSource
from .summarizer import Summarizer

logger = logging.getLogger("yunam.runners.curator")

# Hard caps so the urgent channel stays "phone-vibrates-worth-it" signal.
# Anything past these caps gets demoted to the daily digest.
URGENT_PUSHES_PER_TICK = 1
URGENT_PUSHES_PER_DAY = 5

# Two-layer dedup keeps the same story from filling the digest:
#   1. Within a tick we drop candidates whose normalized title is an exact
#      duplicate of one we've already seen in this tick (e.g. an RSS feed
#      and a Naver search both return the same wire headline).
#   2. Across ticks we use the curated_item_vectors KNN index to detect
#      paraphrases — same event, different wording, different source —
#      within DEDUPE_LOOKBACK_HOURS. Threshold is L2 distance on unit-
#      normalized Voyage embeddings; ~0.45 ≈ cosine 0.90.
DEDUPE_LOOKBACK_HOURS = 12
DEDUPE_MAX_DISTANCE = 0.45

_TITLE_NORMALIZE_STRIP = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_TITLE_NORMALIZE_NONWORD = re.compile(r"[\W_]+", re.UNICODE)


def _normalize_title(title: str) -> str:
    """Cheap normalization for within-tick exact-dup detection.

    Strips wrapper brackets ('[연준]', '(시사)'), then drops every non-word
    character. Returns lowercase. Two titles colliding here means the wire
    headline is byte-identical modulo formatting — a reliable dup signal.
    """
    if not title:
        return ""
    t = _TITLE_NORMALIZE_STRIP.sub("", title)
    t = _TITLE_NORMALIZE_NONWORD.sub("", t)
    return t.lower()


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
    routed_counts = {
        "urgent": 0, "digest": 0, "drop": 0, "dedup": 0,
        "title_dedup": 0, "semantic_dedup": 0, "error": 0,
    }

    # Within-tick title dedup — collapse exact-normalized title collisions
    # before we spend tokens on summarize/score. Order-preserving so the
    # first source that surfaced the story wins (typically the more
    # authoritative wire).
    deduped: list[CuratedCandidate] = []
    seen_titles: set[str] = set()
    for candidate in candidates:
        key = _normalize_title(candidate.title)
        if key and key in seen_titles:
            routed_counts["title_dedup"] += 1
            continue
        if key:
            seen_titles.add(key)
        deduped.append(candidate)

    # Collect urgent-routed items here instead of pushing inline. After the
    # whole tick is scored we take the top-N by score (subject to the daily
    # cap) and demote the rest to digest — keeps the urgent channel scarce
    # even when a news cycle floods us with EXTREME-rated items.
    urgent_candidates: list[tuple[int, float]] = []

    dedupe_since = _hours_ago_iso(DEDUPE_LOOKBACK_HOURS)

    for candidate in deduped:
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
                store=store,
                urgent_threshold=urgent_threshold,
                digest_threshold=digest_threshold,
                routed_counts=routed_counts,
                urgent_candidates=urgent_candidates,
                dedupe_since=dedupe_since,
            )
        except Exception:
            routed_counts["error"] += 1
            logger.exception(
                "curation: per-item processing failed item_id=%s", item_id
            )

    pushed, demoted = await _dispatch_urgents(
        urgent_candidates=urgent_candidates,
        store=store,
        pusher=pusher,
    )
    routed_counts["urgent_pushed"] = pushed
    routed_counts["urgent_demoted"] = demoted

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
    store: SessionStore,
    urgent_threshold: float,
    digest_threshold: float,
    routed_counts: dict[str, int],
    urgent_candidates: list[tuple[int, float]],
    dedupe_since: str,
) -> None:
    summary = await summarizer.summarize(
        title=candidate.title, raw_excerpt=candidate.raw_excerpt
    )
    score_result = await scorer.score(
        title=candidate.title,
        summary_or_excerpt=summary or candidate.raw_excerpt,
    )

    # Semantic dedup — if a near-identical story landed in the last
    # DEDUPE_LOOKBACK_HOURS, drop this one instead of routing it. We still
    # record the summary/score (it's audit) and the embedding (so later
    # ticks can detect chains of paraphrases), we just downgrade the route.
    decision = route(
        score=score_result.score,
        urgent_threshold=urgent_threshold,
        digest_threshold=digest_threshold,
    )
    dupe = None
    if (
        score_result.embedding is not None
        and decision in (URGENT, DIGEST)
    ):
        dupe = await store.find_recent_curated_dupe(
            score_result.embedding,
            since_iso_utc=dedupe_since,
            max_distance=DEDUPE_MAX_DISTANCE,
            exclude_id=int(item_id),
        )
    if dupe is not None:
        dup_id, dup_title, distance = dupe
        logger.info(
            "semantic dedup: item_id=%s '%.60s' ≈ id=%s '%.60s' (d=%.3f) → drop",
            item_id, candidate.title or "", dup_id, dup_title, distance,
        )
        decision = DROP
        routed_counts["semantic_dedup"] += 1

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
        urgent_candidates.append((int(item_id), float(score_result.score)))


async def _dispatch_urgents(
    *,
    urgent_candidates: list[tuple[int, float]],
    store: SessionStore,
    pusher: CurationPusher,
) -> tuple[int, int]:
    """Send up to N urgent pushes per tick, capped daily; demote the rest.

    Returns (pushed_count, demoted_count). The demoted items keep their
    summary/score but get re-routed to 'digest' so the daily newsletter
    picks them up.
    """
    if not urgent_candidates:
        return 0, 0
    # Highest-score first — if a single tick has multiple EXTREME items we
    # want the most impactful one to be the one that fires.
    urgent_candidates.sort(key=lambda t: t[1], reverse=True)

    day_start = _utc_day_start_iso()
    pushed_today = await store.count_urgent_pushes_since(day_start)
    remaining_today = max(0, URGENT_PUSHES_PER_DAY - pushed_today)
    slots = min(URGENT_PUSHES_PER_TICK, remaining_today)

    pushed = 0
    demoted: list[int] = []
    one_hour_ago = _one_hour_ago_iso()
    for item_id, _score in urgent_candidates:
        if slots <= 0:
            demoted.append(item_id)
            continue
        # Refetch so the push body has the populated summary/score/category.
        recent = await store.list_recent_curated_items(
            since_iso_utc=one_hour_ago, routed_as=URGENT, limit=50
        )
        match = next((it for it in recent if int(it.id) == int(item_id)), None)
        if match is None or match.pushed_at is not None:
            continue
        ok = await pusher.push_urgent(match)
        if ok:
            pushed += 1
            slots -= 1
        else:
            # Treat send failure as a demote so the item still shows up in
            # the digest rather than getting silently dropped.
            demoted.append(item_id)

    for item_id in demoted:
        try:
            await store.set_curated_routed_as(item_id, DIGEST)
        except Exception:
            logger.exception("urgent-demote failed item_id=%s", item_id)
    if demoted:
        logger.info(
            "urgent cap: demoted %d items to digest (pushed=%d, daily_pushed=%d/%d)",
            len(demoted), pushed, pushed_today + pushed, URGENT_PUSHES_PER_DAY,
        )
    return pushed, len(demoted)


def _one_hour_ago_iso() -> str:
    return (datetime.utcnow() - timedelta(hours=1)).isoformat() + "+00:00"


def _hours_ago_iso(hours: int) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "+00:00"


def _utc_day_start_iso() -> str:
    now = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.isoformat() + "+00:00"


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
