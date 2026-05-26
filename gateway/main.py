"""Yunam gateway — entrypoint.

Wires the Telegram long-polling gateway to the Yunam orchestrator. The allowlist
on TELEGRAM_ALLOWED_USER_ID is the gate; unauthorized users are silently ignored
and logged at WARNING.

Uses the manual PTB lifecycle (initialize/start/start_polling/stop/shutdown)
rather than `app.run_polling()` so aiosqlite opens/closes in the same asyncio
event loop.

Handler definitions live in `handlers/`; this module is purely the composition
root — it builds dependencies, stuffs them into `bot_data`, registers handlers
via `handlers.register_handlers()`, and manages the application lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application

import anthropic

from handlers import register_handlers
from handlers._helpers import TELEGRAM_MSG_LIMIT
from yunam.config import configure_logging, load_config
from yunam.embeddings import VoyageEmbedder
from yunam.orchestrator import Orchestrator
from yunam.scheduler import (
    now_utc_iso,
    run_nudge_sweeper,
)
from yunam.sender import PTBSender
from yunam.sessions import SessionStore
from yunam.text_embedder import JinaTextEmbedder
from yunam.mcp import (
    GCalMCPClient,
    StockMCPClient,
    build_gcal_mcp_skill,
    build_stock_mcp_skill,
)
from yunam.runners import run_curation_loop
from yunam.runners.digester import Digester
from yunam.runners.pusher import CurationPusher
from yunam.runners.scorer import Scorer
from yunam.runners.sources import (
    FeedSource,
    MoneyflowSource,
    NaverNewsSource,
    RssGenericSource,
    TossInvestSource,
    XPlaywrightSource,
)
from yunam.runners.summarizer import Summarizer
from yunam.skills import (
    Skill,
    SkillRegistry,
    build_airquality_skill,
    build_curation_skill,
    build_files_skill,
    build_memory_skill,
    build_obsidian_graph_skill,
    build_obsidian_skill,
    build_parcel_skill,
    build_privacy_skill,
    build_reminders_skill,
    build_usage_skill,
    build_web_skill,
)
from yunam.subagents import build_deep_think_orchestrator
from yunam.tools.airquality import AirQualityTools
from yunam.tools.attachments import AttachmentTools
from yunam.tools.curation import CurationTools
from yunam.tools.memory import MemoryTools
from yunam.tools.obsidian import ObsidianTools
from yunam.tools.obsidian_graph import ObsidianGraphTools
from yunam.tools.parcel import ParcelTools
from yunam.tools.reminders import ReminderTools
from yunam.tools.usage import UsageTools
from yunam.tools.web import WebTools
from yunam.usage import UsageRecorder

load_dotenv()
configure_logging()
logger = logging.getLogger("yunam.gateway")


def _build_text_embedder(cfg, voyage_embedder, usage_recorder):
    """Pick the text embedder based on `YUNAM_TEXT_EMBEDDER`.

    `voyage` (default) returns the Voyage instance directly — full backward
    compat with deployments that never set the var. `jina` returns a fresh
    JinaTextEmbedder bound to the existing JINA_API_KEY. Anything else falls
    back to voyage with a warning so a typo doesn't silently degrade memory.
    """
    provider = (cfg.text_embedder_provider or "voyage").lower()
    if provider == "jina":
        if not cfg.jina_api_key:
            logger.warning(
                "YUNAM_TEXT_EMBEDDER=jina but JINA_API_KEY is unset — "
                "falling back to voyage. Set JINA_API_KEY and restart."
            )
            return voyage_embedder
        return JinaTextEmbedder(
            api_key=cfg.jina_api_key, usage_recorder=usage_recorder
        )
    if provider != "voyage":
        logger.warning(
            "unknown YUNAM_TEXT_EMBEDDER=%r; falling back to voyage",
            provider,
        )
    return voyage_embedder


def _build_curation_sources(
    cfg,
    stock_client,
    usage_recorder: UsageRecorder,
) -> list[FeedSource]:
    """Assemble the source list from env. Missing credentials → source skipped.

    Order is intentional and stable across restarts: Naver → RSS → Toss →
    Moneyflow → X. The curator's fan-out doesn't depend on order (results are
    unioned), but log lines + audit attribution are easier to read when it's
    consistent.
    """
    sources: list[FeedSource] = []
    if cfg.naver_client_id and cfg.naver_client_secret and cfg.naver_queries:
        sources.append(
            NaverNewsSource(
                client_id=cfg.naver_client_id,
                client_secret=cfg.naver_client_secret,
                queries=cfg.naver_queries,
                usage_recorder=usage_recorder,
            )
        )
    # Tiered RSS sources. Three independent RssGenericSource instances with
    # tier_divisor=1/2/4 produce a natural per-tick cadence: every tick fires
    # tier_high; every 2nd fires tier_mid; every 4th fires tier_low. The
    # legacy `YUNAM_CURATION_RSS_FEEDS` env var (no tier suffix) is treated as
    # tier_high so older .env files keep working unchanged.
    high_feeds = cfg.curation_rss_tier_high or cfg.curation_rss_feeds
    if high_feeds:
        sources.append(
            RssGenericSource(
                feeds=high_feeds,
                usage_recorder=usage_recorder,
                tier_divisor=1,
                tier_label="rss-high",
            )
        )
    if cfg.curation_rss_tier_mid:
        sources.append(
            RssGenericSource(
                feeds=cfg.curation_rss_tier_mid,
                usage_recorder=usage_recorder,
                tier_divisor=2,
                tier_label="rss-mid",
            )
        )
    if cfg.curation_rss_tier_low:
        sources.append(
            RssGenericSource(
                feeds=cfg.curation_rss_tier_low,
                usage_recorder=usage_recorder,
                tier_divisor=4,
                tier_label="rss-low",
            )
        )
    if cfg.toss_fetch_mode != "disabled":
        sources.append(
            TossInvestSource(
                mode=cfg.toss_fetch_mode,
                api_url=cfg.toss_news_url,
                usage_recorder=usage_recorder,
            )
        )
    if cfg.moneyflow_enabled and stock_client is not None:
        sources.append(
            MoneyflowSource(
                stock_client, timezone_name=cfg.timezone, enabled=True
            )
        )
    if cfg.x_enabled:
        sources.append(
            XPlaywrightSource(handles=cfg.x_handles, enabled=True)
        )
    return sources


async def _run() -> None:
    cfg = load_config()
    logger.info(
        "gateway starting; principals=%s owner=%s allowed_chats=%s "
        "group_triggers=%s vault=%s filevault=%s db=%s",
        ", ".join(f"{p.name}({p.user_id})" for p in cfg.principals),
        cfg.owner.name,
        list(cfg.allowed_chats) if cfg.allowed_chats else "(DM-only)",
        list(cfg.group_triggers) if cfg.group_triggers else "(@mention-only)",
        cfg.vault_path,
        cfg.filevault_path,
        cfg.db_path,
    )

    store = await SessionStore.open(cfg.db_path)
    # UsageRecorder is constructed once and threaded through every paid
    # external call (Anthropic / Voyage / Jina / Sweet Tracker / Open-Meteo /
    # MCP). Each tool that takes optional `usage_recorder=` will record per
    # request; missing it (e.g. obsidian filesystem ops) is a no-op.
    usage_recorder = UsageRecorder(store)
    tools = ObsidianTools(cfg.vault_path)
    claude_client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
    voyage_embedder = VoyageEmbedder(
        api_key=cfg.voyage_api_key, usage_recorder=usage_recorder
    )
    # `text_embedder` powers every text-only embedding path: memory recall,
    # curation scoring / search, orchestrator turn embeddings. `voyage_embedder`
    # stays available for AttachmentTools' multimodal (image) path — Jina v3
    # doesn't accept images. When you've exhausted Voyage's free tier, set
    # YUNAM_TEXT_EMBEDDER=jina and only multimodal file saves keep paying.
    text_embedder = _build_text_embedder(cfg, voyage_embedder, usage_recorder)
    logger.info(
        "text embedder = %s (multimodal path stays on voyage)",
        cfg.text_embedder_provider,
    )
    # Backwards compat alias — every consumer further down still reads
    # `embedder`. With YUNAM_TEXT_EMBEDDER=voyage (default) this is the
    # Voyage instance; with jina it's the JinaTextEmbedder.
    embedder = text_embedder

    app = Application.builder().token(cfg.telegram_token).build()
    sender = PTBSender(app.bot)
    attachments = AttachmentTools(
        store=store,
        filevault_root=cfg.filevault_path,
        obsidian_root=cfg.vault_path,
        sender=sender,
        # AttachmentTools' image path needs multimodal — always Voyage.
        embedder=voyage_embedder,
        vision_client=claude_client,
        timezone=cfg.timezone,
    )
    web_tools = WebTools(
        jina_api_key=cfg.jina_api_key, usage_recorder=usage_recorder
    )
    airquality_tools = AirQualityTools(usage_recorder=usage_recorder)
    parcel_tools = ParcelTools(
        api_key=cfg.sweettracker_api_key, usage_recorder=usage_recorder
    )
    reminder_tools = ReminderTools(store=store, timezone_name=cfg.timezone)
    memory_tools = MemoryTools(
        store=store,
        embedder=embedder,
        timezone_name=cfg.timezone,
        principals=cfg.principals,
    )
    graph_tools = ObsidianGraphTools(vault_root=cfg.vault_path)
    usage_tools = UsageTools(
        store=store,
        timezone_name=cfg.timezone,
        daily_alert_usd=cfg.cost_alert_daily_usd,
        monthly_alert_usd=cfg.cost_alert_monthly_usd,
    )
    curation_tools = CurationTools(
        store=store, embedder=embedder, timezone_name=cfg.timezone
    )

    # Optional: connect to MCP sibling containers. If a URL is unset we skip
    # the skill entirely (dev-time or pre-OAuth state). If a URL is set but
    # the container is unreachable / broken, we log loudly and skip the skill
    # rather than crash the gateway — keeping Yunam usable on the rest of its
    # surface is more valuable than fail-fast for an optional integration.
    gcal_client: GCalMCPClient | None = None
    gcal_skill: Skill | None = None
    if cfg.gcal_mcp_url:
        logger.info("gcal MCP configured at %s — connecting", cfg.gcal_mcp_url)
        try:
            gcal_client = GCalMCPClient(
                cfg.gcal_mcp_url, usage_recorder=usage_recorder
            )
            await gcal_client.connect()
            gcal_skill = build_gcal_mcp_skill(gcal_client)
        except Exception:
            logger.exception(
                "gcal MCP connect failed — skill disabled for this run "
                "(if message mentions 'already initialized', try "
                "`docker restart yunam-calendar-mcp` then restart gateway)"
            )
            gcal_client = None
            gcal_skill = None
    else:
        logger.info("YUNAM_GCAL_MCP_URL unset — gcal skill disabled")

    stock_client: StockMCPClient | None = None
    stock_skill: Skill | None = None
    if cfg.stock_mcp_url:
        logger.info("stock MCP configured at %s — connecting", cfg.stock_mcp_url)
        try:
            stock_client = StockMCPClient(
                cfg.stock_mcp_url, usage_recorder=usage_recorder
            )
            await stock_client.connect()
            stock_skill = build_stock_mcp_skill(stock_client)
        except Exception:
            logger.exception("stock MCP connect failed — skill disabled for this run")
            stock_client = None
            stock_skill = None
    else:
        logger.info("YUNAM_STOCK_MCP_URL unset — stock skill disabled")

    # Skill order is a prompt-cache-affecting invariant — the flattened tool
    # list Claude sees is [obsidian, files, web, airquality, parcel, gcal?,
    # reminders, memory, obsidian_graph], and the concatenated system prompt
    # mirrors that order. Don't reshuffle casually — new skills go at the end.
    skills: list[Skill] = [
        build_obsidian_skill(tools),
        build_files_skill(attachments),
        build_web_skill(web_tools),
        build_airquality_skill(airquality_tools),
        build_parcel_skill(parcel_tools),
    ]
    if gcal_skill is not None:
        skills.append(gcal_skill)
    if stock_skill is not None:
        skills.append(stock_skill)
    skills.append(build_reminders_skill(reminder_tools))
    skills.append(build_memory_skill(memory_tools))
    skills.append(build_obsidian_graph_skill(graph_tools))
    skills.append(build_privacy_skill())
    # Usage skill — appending after `privacy` preserves the existing
    # prompt-cache prefix and only adds its own fragment + tools at the tail.
    skills.append(build_usage_skill(usage_tools))
    # Curation skill (Phase 2.1) — last in the registry. Read/admin surface
    # only; the background runner does the real work outside the tool surface.
    skills.append(build_curation_skill(curation_tools))
    registry = SkillRegistry(skills)
    orch = Orchestrator(
        claude_client, store, registry,
        timezone=cfg.timezone,
        vault_path=cfg.vault_path,
        embedder=embedder,
        principals=cfg.principals,
        usage_recorder=usage_recorder,
    )
    # Deep-think path (Opus 4.7 + adaptive / high effort) — only invoked via
    # the /think command, never by the main agent autonomously.
    deep_orch = build_deep_think_orchestrator(
        claude_client, store, registry,
        timezone=cfg.timezone,
        vault_path=cfg.vault_path,
        embedder=embedder,
        principals=cfg.principals,
        usage_recorder=usage_recorder,
    )

    app.bot_data["cfg"] = cfg
    app.bot_data["orch"] = orch
    app.bot_data["deep_orch"] = deep_orch
    app.bot_data["store"] = store
    app.bot_data["attachments"] = attachments
    app.bot_data["media_group_batches"] = {}
    # bot_username is populated after `app.initialize()` below — used by the
    # group-chat mention-gating logic. PTB caches `app.bot.username` after the
    # first `get_me()` call inside initialize, so it's safe to read post-init.

    register_handlers(app)

    stop_event = asyncio.Event()

    async def _sweep_nudges() -> None:
        """Deliver any due reminders. Called every nudge_sweep_interval_seconds."""
        due = await store.list_due_nudges(now_utc_iso())
        if not due:
            return
        logger.info("nudge sweeper: %d due", len(due))
        for nudge in due:
            try:
                await app.bot.send_message(
                    chat_id=nudge.chat_id, text=nudge.message[:TELEGRAM_MSG_LIMIT]
                )
                # Record so history shows the proactive nudge as prior context.
                await store.record_proactive_message(nudge.chat_id, nudge.message)
                await store.mark_nudge_sent(nudge.id)
                logger.info("nudge sweeper: fired id=%s chat_id=%s", nudge.id, nudge.chat_id)
            except Exception:
                # Don't mark sent on failure — next sweep will retry.
                logger.exception("nudge sweeper: failed to dispatch id=%s", nudge.id)

    def _on_signal(sig_name: str) -> None:
        logger.info("signal %s received; shutting down", sig_name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except NotImplementedError:
            # Signal handlers aren't available on Windows; fine to skip.
            pass

    scheduler_tasks: list[asyncio.Task[None]] = []
    try:
        await app.initialize()
        # PTB has now called get_me() internally — pull the bot's username
        # for group-chat mention gating. Lower-cased once so the comparison
        # in `should_engage_in_group` doesn't have to.
        try:
            app.bot_data["bot_username"] = app.bot.username
            logger.info("bot username resolved: @%s", app.bot.username)
        except Exception:
            logger.warning("could not resolve bot username; group mention gating disabled")
            app.bot_data["bot_username"] = None
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("gateway running")

        if cfg.nudge_sweeper_enabled:
            scheduler_tasks.append(asyncio.create_task(
                run_nudge_sweeper(
                    on_sweep=_sweep_nudges,
                    stop_event=stop_event,
                    interval_seconds=cfg.nudge_sweep_interval_seconds,
                ),
                name="yunam-nudge-sweeper",
            ))
        else:
            logger.info("nudge sweeper disabled (YUNAM_NUDGE_SWEEPER_ENABLED is not set)")

        if cfg.curation_enabled:
            curation_sources = _build_curation_sources(
                cfg, stock_client, usage_recorder
            )
            if not curation_sources:
                logger.warning(
                    "curation enabled but no sources configured "
                    "(set NAVER_CLIENT_ID/SECRET, RSS feeds, or moneyflow); "
                    "runner will tick but fetch nothing"
                )
            curation_summarizer = Summarizer(
                claude_client, usage_recorder=usage_recorder
            )
            curation_scorer = Scorer(
                claude_client, embedder, usage_recorder=usage_recorder
            )
            curation_pusher = CurationPusher(
                app.bot, store, owner_chat_id=cfg.owner.user_id
            )
            curation_digester = Digester(store)
            scheduler_tasks.append(asyncio.create_task(
                run_curation_loop(
                    cfg=cfg,
                    store=store,
                    sources=curation_sources,
                    summarizer=curation_summarizer,
                    scorer=curation_scorer,
                    pusher=curation_pusher,
                    digester=curation_digester,
                    stop_event=stop_event,
                ),
                name="yunam-curation-loop",
            ))
            logger.info(
                "curation runner started interval=%dmin newsletter_time=%s sources=%s",
                cfg.curation_interval_minutes,
                cfg.curation_newsletter_time,
                [s.name for s in curation_sources],
            )
        else:
            logger.info("curation runner disabled (YUNAM_CURATION_ENABLED is not set)")

        await stop_event.wait()
    finally:
        logger.info("gateway stopping")
        for task in scheduler_tasks:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("%s did not exit within 5s; cancelling", task.get_name())
                task.cancel()
            except Exception:
                logger.exception("%s raised on shutdown", task.get_name())
        try:
            await app.updater.stop()
        except Exception:
            logger.exception("error stopping updater")
        try:
            await app.stop()
        except Exception:
            logger.exception("error stopping application")
        try:
            await app.shutdown()
        except Exception:
            logger.exception("error during shutdown")
        if gcal_client is not None:
            await gcal_client.close()
        if stock_client is not None:
            await stock_client.close()
        # Drain in-flight api_usage writes before closing the DB so a fast
        # shutdown doesn't lose the last turn's bookkeeping.
        await usage_recorder.flush()
        await store.close()
        logger.info("gateway stopped cleanly")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
