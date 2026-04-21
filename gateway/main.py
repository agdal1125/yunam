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
from yunam.orchestrator import Orchestrator
from yunam.prompts import DAILY_PROMPT_TEMPLATE
from yunam.scheduler import run_daily_scheduler
from yunam.sessions import SessionStore
from yunam.tools.obsidian import ObsidianTools

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
        "gateway starting; allowlist user_id=%s vault=%s db=%s",
        cfg.allowed_user_id,
        cfg.vault_path,
        cfg.db_path,
    )

    store = await SessionStore.open(cfg.db_path)
    tools = ObsidianTools(cfg.vault_path)
    claude_client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
    orch = Orchestrator(claude_client, store, tools, timezone=cfg.timezone)

    app = Application.builder().token(cfg.telegram_token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["orch"] = orch
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    stop_event = asyncio.Event()

    async def _send_daily_prompt(chat_id: int, date_str: str) -> None:
        text = DAILY_PROMPT_TEMPLATE.format(date=date_str)
        await app.bot.send_message(chat_id=chat_id, text=text)
        # Record so the user's reply loads this prompt as prior assistant context.
        await store.record_proactive_message(chat_id, text)

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

    scheduler_task: asyncio.Task[None] | None = None
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("gateway running")

        if cfg.schedule_enabled:
            scheduler_task = asyncio.create_task(
                run_daily_scheduler(
                    chat_id=cfg.allowed_user_id,
                    hour=cfg.daily_reflection_hour,
                    minute=cfg.daily_reflection_minute,
                    tz_name=cfg.timezone,
                    on_fire=_send_daily_prompt,
                    stop_event=stop_event,
                ),
                name="yunam-daily-scheduler",
            )
        else:
            logger.info("scheduler disabled (YUNAM_SCHEDULE_ENABLED is not set)")

        await stop_event.wait()
    finally:
        logger.info("gateway stopping")
        if scheduler_task is not None:
            try:
                await asyncio.wait_for(scheduler_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("scheduler did not exit within 5s; cancelling")
                scheduler_task.cancel()
            except Exception:
                logger.exception("scheduler task raised on shutdown")
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
        await store.close()
        logger.info("gateway stopped cleanly")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
