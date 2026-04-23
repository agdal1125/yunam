"""Yunam gateway — entrypoint.

Wires the Telegram long-polling gateway to the Yunam orchestrator. The allowlist
on TELEGRAM_ALLOWED_USER_ID is the gate; unauthorized users are silently ignored
and logged at WARNING.

Uses the manual PTB lifecycle (initialize/start/start_polling/stop/shutdown)
rather than `app.run_polling()` so aiosqlite opens/closes in the same asyncio
event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import anthropic

from yunam.config import Config, configure_logging, load_config
from yunam.embeddings import VoyageEmbedder
from yunam.orchestrator import Orchestrator
from yunam.prompts import DAILY_PROMPT_TEMPLATE
from yunam.scheduler import (
    now_utc_iso,
    run_daily_scheduler,
    run_nudge_sweeper,
)
from yunam.sender import PTBSender
from yunam.sessions import SessionStore
from yunam.mcp import GCalMCPClient, build_gcal_mcp_skill
from yunam.skills import (
    Skill,
    SkillRegistry,
    build_airquality_skill,
    build_files_skill,
    build_memory_skill,
    build_obsidian_graph_skill,
    build_obsidian_skill,
    build_parcel_skill,
    build_reminders_skill,
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
from yunam.tools.web import WebTools

load_dotenv()
configure_logging()
logger = logging.getLogger("yunam.gateway")

TELEGRAM_MSG_LIMIT = 4096


def _is_authorized(update: Update, allowed_user_id: int) -> bool:
    user = update.effective_user
    if user is None:
        logger.warning("update with no effective_user: %s", update.update_id)
        return False
    if user.id != allowed_user_id:
        logger.warning(
            "unauthorized access: user_id=%s username=%s",
            user.id,
            user.username,
        )
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    if not _is_authorized(update, cfg.allowed_user_id):
        return
    logger.info("/start from user_id=%s", update.effective_user.id)
    await update.message.reply_text(
        "Yunam online. I have access to your Obsidian vault — ask me anything."
    )


def _extract_attachment(message) -> dict | None:
    """Return a kind/file_id/metadata dict for the attachment on `message`, or None.

    Photos are delivered as an array of sizes; we use the largest. Everything
    else has a single nested object. Order reflects Telegram's priorities.
    """
    if message.photo:
        largest = message.photo[-1]
        return {
            "kind": "photo",
            "file_id": largest.file_id,
            "file_unique_id": largest.file_unique_id,
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": largest.file_size,
        }
    if message.document:
        d = message.document
        return {
            "kind": "document",
            "file_id": d.file_id,
            "file_unique_id": d.file_unique_id,
            "file_name": d.file_name,
            "mime_type": d.mime_type,
            "file_size": d.file_size,
        }
    if message.video:
        v = message.video
        return {
            "kind": "video",
            "file_id": v.file_id,
            "file_unique_id": v.file_unique_id,
            "file_name": v.file_name,
            "mime_type": v.mime_type,
            "file_size": v.file_size,
        }
    if message.animation:
        a = message.animation
        return {
            "kind": "animation",
            "file_id": a.file_id,
            "file_unique_id": a.file_unique_id,
            "file_name": a.file_name,
            "mime_type": a.mime_type,
            "file_size": a.file_size,
        }
    if message.voice:
        v = message.voice
        return {
            "kind": "voice",
            "file_id": v.file_id,
            "file_unique_id": v.file_unique_id,
            "file_name": None,
            "mime_type": v.mime_type or "audio/ogg",
            "file_size": v.file_size,
        }
    if message.audio:
        a = message.audio
        return {
            "kind": "audio",
            "file_id": a.file_id,
            "file_unique_id": a.file_unique_id,
            "file_name": a.file_name,
            "mime_type": a.mime_type,
            "file_size": a.file_size,
        }
    return None


async def on_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record an incoming attachment. Saves immediately if caption starts with /save."""
    cfg: Config = context.application.bot_data["cfg"]
    if not _is_authorized(update, cfg.allowed_user_id):
        return

    message = update.message
    if message is None:
        return
    meta = _extract_attachment(message)
    if meta is None:
        return

    chat_id = update.effective_chat.id
    caption = message.caption
    store: SessionStore = context.application.bot_data["store"]

    # Always stash in `pending_attachments` — cheap, defers download until /save.
    pending_id = await store.add_pending_attachment(
        chat_id=chat_id,
        file_id=meta["file_id"],
        file_unique_id=meta["file_unique_id"],
        kind=meta["kind"],
        file_name=meta["file_name"],
        mime_type=meta["mime_type"],
        file_size=meta["file_size"],
        caption=caption,
    )
    logger.info(
        "attachment received chat_id=%s kind=%s file_id=%s name=%s size=%s pending_id=%s",
        chat_id,
        meta["kind"],
        meta["file_id"][:16] + "...",
        meta["file_name"],
        meta["file_size"],
        pending_id,
    )

    # Fast path: `/save` in caption → commit immediately without going through the agent.
    inline_save = bool(caption) and caption.strip().lower().startswith("/save")
    if inline_save:
        await _commit_and_reply(update, context, caption_override=_strip_save_prefix(caption))
        return

    await message.reply_text(
        "📎 got it. Send /save to keep this, or describe what to do with it."
    )


def _strip_save_prefix(caption: str | None) -> str | None:
    """Return the caption with the leading `/save` command stripped, or None if empty."""
    if caption is None:
        return None
    stripped = caption.strip()
    if not stripped.lower().startswith("/save"):
        return caption
    remainder = stripped[len("/save"):].lstrip()
    return remainder or None


async def _commit_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    caption_override: str | None = None,
) -> None:
    chat_id = update.effective_chat.id
    attachments: AttachmentTools | None = context.application.bot_data.get("attachments")
    if attachments is None:
        await update.message.reply_text("attachments not configured on this server.")
        return
    try:
        saved = await attachments.commit_pending(
            chat_id=chat_id, caption_override=caption_override
        )
    except Exception:
        logger.exception("commit_pending failed chat_id=%s", chat_id)
        await update.message.reply_text("save failed — check the logs.")
        return
    if saved is None:
        await update.message.reply_text(
            "no recent attachment to save. Send a file first, then /save."
        )
        return
    await update.message.reply_text(
        f"✅ saved: {saved.relpath} ({saved.file_size or 0} bytes). indexed."
    )


async def on_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/save` typed as its own message (no attachment in this message)."""
    cfg: Config = context.application.bot_data["cfg"]
    if not _is_authorized(update, cfg.allowed_user_id):
        return
    # If /save was attached to a file, `on_attachment` already handled it — this
    # path is only for "/save" typed on its own or with text args.
    text = (update.message.text or "").strip()
    remainder = text[len("/save"):].lstrip() if text.lower().startswith("/save") else None
    await _commit_and_reply(update, context, caption_override=remainder or None)


async def on_think(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/think <query>` — route to the Opus deep-think orchestrator."""
    cfg: Config = context.application.bot_data["cfg"]
    if not _is_authorized(update, cfg.allowed_user_id):
        return

    text = (update.message.text or "").strip()
    # `/think` with optional args — split on first whitespace.
    parts = text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        await update.message.reply_text(
            "Usage: /think <your question>\n"
            "Routes to Opus 4.7 with adaptive thinking. Costs more — use for "
            "problems where Sonnet's default reply feels shallow."
        )
        return

    chat_id = update.effective_chat.id
    logger.info("/think start chat_id=%s len=%d", chat_id, len(query))

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        logger.debug("send_chat_action failed", exc_info=True)

    deep_orch: Orchestrator = context.application.bot_data["deep_orch"]
    try:
        response = await deep_orch.handle_turn(chat_id, query)
    except Exception:
        logger.exception("/think orchestrator failure chat_id=%s", chat_id)
        response = "Sorry — deep-think failed. Check the logs."

    await update.message.reply_text(response[:TELEGRAM_MSG_LIMIT])


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    if not _is_authorized(update, cfg.allowed_user_id):
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text or ""
    logger.info("turn start chat_id=%s len=%d", chat_id, len(user_text))

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        # A typing indicator failure should not abort the turn.
        logger.debug("send_chat_action failed", exc_info=True)

    orch: Orchestrator = context.application.bot_data["orch"]
    try:
        response = await orch.handle_turn(chat_id, user_text)
    except Exception:
        logger.exception("orchestrator failure chat_id=%s", chat_id)
        response = "Sorry — something went wrong on my end. Check the logs."

    await update.message.reply_text(response[:TELEGRAM_MSG_LIMIT])


async def _run() -> None:
    cfg = load_config()
    logger.info(
        "gateway starting; allowlist user_id=%s vault=%s filevault=%s db=%s",
        cfg.allowed_user_id,
        cfg.vault_path,
        cfg.filevault_path,
        cfg.db_path,
    )

    store = await SessionStore.open(cfg.db_path)
    tools = ObsidianTools(cfg.vault_path)
    claude_client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
    embedder = VoyageEmbedder(api_key=cfg.voyage_api_key)

    app = Application.builder().token(cfg.telegram_token).build()
    sender = PTBSender(app.bot)
    attachments = AttachmentTools(
        store=store,
        filevault_root=cfg.filevault_path,
        obsidian_root=cfg.vault_path,
        sender=sender,
        embedder=embedder,
        timezone=cfg.timezone,
    )
    web_tools = WebTools(jina_api_key=cfg.jina_api_key)
    airquality_tools = AirQualityTools()
    parcel_tools = ParcelTools(api_key=cfg.sweettracker_api_key)
    reminder_tools = ReminderTools(store=store, timezone_name=cfg.timezone)
    memory_tools = MemoryTools(store=store, embedder=embedder, timezone_name=cfg.timezone)
    graph_tools = ObsidianGraphTools(vault_root=cfg.vault_path)

    # Optional: connect to the Google Calendar MCP sibling container. If the
    # URL is unset we skip the skill entirely (dev-time or pre-OAuth state).
    # If the URL is set but unreachable, fail fast — silent skipping there
    # would hide a misconfiguration.
    gcal_client: GCalMCPClient | None = None
    gcal_skill: Skill | None = None
    if cfg.gcal_mcp_url:
        logger.info("gcal MCP configured at %s — connecting", cfg.gcal_mcp_url)
        gcal_client = GCalMCPClient(cfg.gcal_mcp_url)
        await gcal_client.connect()
        gcal_skill = build_gcal_mcp_skill(gcal_client)
    else:
        logger.info("YUNAM_GCAL_MCP_URL unset — gcal skill disabled")

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
    skills.append(build_reminders_skill(reminder_tools))
    skills.append(build_memory_skill(memory_tools))
    skills.append(build_obsidian_graph_skill(graph_tools))
    registry = SkillRegistry(skills)
    orch = Orchestrator(
        claude_client, store, registry,
        timezone=cfg.timezone,
        vault_path=cfg.vault_path,
        embedder=embedder,
    )
    # Deep-think path (Opus 4.7 + adaptive / high effort) — only invoked via
    # the /think command, never by the main agent autonomously.
    deep_orch = build_deep_think_orchestrator(
        claude_client, store, registry,
        timezone=cfg.timezone,
        vault_path=cfg.vault_path,
        embedder=embedder,
    )

    app.bot_data["cfg"] = cfg
    app.bot_data["orch"] = orch
    app.bot_data["deep_orch"] = deep_orch
    app.bot_data["store"] = store
    app.bot_data["attachments"] = attachments

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("save", on_save))
    app.add_handler(CommandHandler("think", on_think))
    # Attachment handlers — order doesn't matter, filters are disjoint.
    attachment_filter = (
        filters.PHOTO
        | filters.Document.ALL
        | filters.VIDEO
        | filters.VOICE
        | filters.AUDIO
        | filters.ANIMATION
    )
    app.add_handler(MessageHandler(attachment_filter, on_attachment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    stop_event = asyncio.Event()

    async def _send_daily_prompt(chat_id: int, date_str: str) -> None:
        text = DAILY_PROMPT_TEMPLATE.format(date=date_str)
        await app.bot.send_message(chat_id=chat_id, text=text)
        # Record so the user's reply loads this prompt as prior assistant context.
        await store.record_proactive_message(chat_id, text)

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
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("gateway running")

        if cfg.schedule_enabled:
            scheduler_tasks.append(asyncio.create_task(
                run_daily_scheduler(
                    chat_id=cfg.allowed_user_id,
                    hour=cfg.daily_reflection_hour,
                    minute=cfg.daily_reflection_minute,
                    tz_name=cfg.timezone,
                    on_fire=_send_daily_prompt,
                    stop_event=stop_event,
                ),
                name="yunam-daily-scheduler",
            ))
        else:
            logger.info("daily retrospective disabled (YUNAM_SCHEDULE_ENABLED is not set)")

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
        await store.close()
        logger.info("gateway stopped cleanly")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
