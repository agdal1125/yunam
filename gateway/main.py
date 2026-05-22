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
from yunam.mcp import (
    GCalMCPClient,
    StockMCPClient,
    build_gcal_mcp_skill,
    build_stock_mcp_skill,
)
from yunam.skills import (
    Skill,
    SkillRegistry,
    build_airquality_skill,
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
    embedder = VoyageEmbedder(
        api_key=cfg.voyage_api_key, usage_recorder=usage_recorder
    )

    app = Application.builder().token(cfg.telegram_token).build()
    sender = PTBSender(app.bot)
    attachments = AttachmentTools(
        store=store,
        filevault_root=cfg.filevault_path,
        obsidian_root=cfg.vault_path,
        sender=sender,
        embedder=embedder,
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
    # Usage skill last — appending after `privacy` preserves the existing
    # prompt-cache prefix and only adds its own fragment + tools at the tail.
    skills.append(build_usage_skill(usage_tools))
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
