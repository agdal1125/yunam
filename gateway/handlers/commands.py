"""Telegram command handlers — /start, /save, /think, /diary, /chatid.

Each handler follows the same pattern: resolve principal → check auth →
extract args → dispatch → reply. Dependencies come from `bot_data`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from yunam.auth import (
    is_authorized_chat,
    log_unauthorized_chat,
    resolve_principal,
)
from yunam.config import Config
from yunam.orchestrator import Orchestrator
from yunam.prompts import DAILY_PROMPT_TEMPLATE
from yunam.sessions import SessionStore
from yunam.tools.attachments import AttachmentTools

from ._helpers import TELEGRAM_MSG_LIMIT

logger = logging.getLogger("yunam.gateway")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return
    logger.info(
        "/start from principal=%s user_id=%s chat_id=%s",
        principal.name,
        update.effective_user.id,
        getattr(update.effective_chat, "id", "?"),
    )
    await update.message.reply_text(
        f"Yunam online, {principal.name}. I have access to your Obsidian vault — ask me anything."
    )


async def on_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/save` typed as its own message (no attachment in this message)."""
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return
    # If /save was attached to a file, `on_attachment` already handled it — this
    # path is only for "/save" typed on its own or with text args.
    text = (update.message.text or "").strip()
    remainder = text[len("/save"):].lstrip() if text.lower().startswith("/save") else None
    await _commit_and_reply(update, context, caption_override=remainder or None)


async def on_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the current chat_id back to the requester.

    Principal-gated (only known users get an answer) but intentionally
    bypasses `is_authorized_chat` — the whole point of the command is to
    discover the id of a chat that ISN'T yet on the allowlist so jaekeun
    can add it. After echoing, suggests the .env edit. The reply is plain
    text and harmless to leak (chat_id is visible to every chat member
    anyway via Telegram clients), so disclosure isn't a concern.
    """
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    chat_type = chat.type
    title = getattr(chat, "title", None) or getattr(chat, "username", None) or "DM"
    already_allowed = chat_type == "private" or chat_id in cfg.allowed_chats
    status = "already enabled" if already_allowed else "not in YUNAM_ALLOWED_CHATS"
    body = (
        f"chat_id: {chat_id}\n"
        f"type: {chat_type}\n"
        f"title: {title}\n"
        f"status: {status}"
    )
    if not already_allowed:
        body += (
            f"\n\nTo enable, append {chat_id} to YUNAM_ALLOWED_CHATS in .env "
            f"and restart gateway."
        )
    logger.info(
        "/chatid principal=%s chat_id=%s type=%s allowed=%s",
        principal.name, chat_id, chat_type, already_allowed,
    )
    await update.message.reply_text(body)


async def on_think(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/think <query>` — route to the Opus deep-think orchestrator."""
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
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
    logger.info(
        "/think start principal=%s chat_id=%s len=%d", principal.name, chat_id, len(query)
    )

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        logger.debug("send_chat_action failed", exc_info=True)

    deep_orch: Orchestrator = context.application.bot_data["deep_orch"]
    try:
        response = await deep_orch.handle_turn(chat_id, query, principal=principal)
    except Exception:
        logger.exception("/think orchestrator failure chat_id=%s", chat_id)
        response = "Sorry — deep-think failed. Check the logs."

    await update.message.reply_text(response[:TELEGRAM_MSG_LIMIT])


async def on_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/diary [content]` — manual daily reflection trigger."""
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return

    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    content = parts[1].strip() if len(parts) > 1 else ""

    chat_id = update.effective_chat.id

    tz = ZoneInfo(cfg.timezone)
    date_str = datetime.now(tz).strftime("%Y-%m-%d")

    if not content:
        prompt_text = DAILY_PROMPT_TEMPLATE.format(date=date_str)
        await update.message.reply_text(prompt_text)
        store: SessionStore = context.application.bot_data["store"]
        await store.record_proactive_message(
            chat_id, prompt_text, target_user_id=cfg.owner.user_id
        )
        return

    logger.info(
        "/diary start principal=%s chat_id=%s len=%d", principal.name, chat_id, len(content)
    )

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        logger.debug("send_chat_action failed", exc_info=True)

    orch: Orchestrator = context.application.bot_data["orch"]
    user_text = f"오늘({date_str})의 일기/하루 정리 내용이야: {content}"
    try:
        response = await orch.handle_turn(chat_id, user_text, principal=principal)
    except Exception:
        logger.exception("/diary orchestrator failure chat_id=%s", chat_id)
        response = "일기 저장 중 문제가 발생했어. 로그를 확인해줘."

    await update.message.reply_text(response[:TELEGRAM_MSG_LIMIT])


# ---- internal helpers -------------------------------------------------------


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
