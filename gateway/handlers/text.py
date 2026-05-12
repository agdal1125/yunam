"""Free-text message handler + group-chat engagement logic.

`on_text` is the catch-all for non-command, non-attachment messages. The
group-chat gating functions (`should_engage_in_group`, `strip_bot_mention`)
live here because `on_text` is their primary consumer; `attachments.py`
imports `should_engage_in_group` from here.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from yunam.auth import (
    is_authorized_chat,
    log_unauthorized_chat,
    match_group_trigger,
    resolve_principal,
    strip_group_trigger,
)
from yunam.config import Config
from yunam.orchestrator import Orchestrator

from ._helpers import TELEGRAM_MSG_LIMIT

logger = logging.getLogger("yunam.gateway")


def should_engage_in_group(
    update: Update,
    bot_username: str | None,
    group_triggers: tuple[str, ...] = (),
) -> bool:
    """Return True iff yunam should reply to a group-chat message.

    1:1 DMs always engage. In groups, we engage only when explicitly invited:
      - Telegram 'mention' or 'text_mention' entity matching @bot_username
      - The message is a reply to a previous yunam message
      - The message is a /command (PTB's CommandHandler dispatches these
        regardless; we still need to gate on /command in on_text-style flows)
      - The message starts with one of `group_triggers` — vocative-style
        in-process aliases (e.g. `유남아 ...`) so jaekeun doesn't have to
        type the full @AgentYunamBot handle each time.

    This matches the user's choice (Q1=b): privacy-mode-on style behavior
    even if @BotFather privacy mode is later flipped off — we behave the
    same, only engaging on explicit invitation. Cheaper, less surprising.
    """
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return True
    message = update.message
    if message is None:
        return False
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        return True
    # Trigger-word alias check — cheap substring at message start. Does not
    # require Telegram entities, so it works regardless of bot_username
    # availability or privacy-mode state.
    if group_triggers and match_group_trigger(text, group_triggers) is not None:
        return True
    # Reply to one of yunam's prior messages — implicit re-invocation.
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and getattr(reply.from_user, "is_bot", False):
        if bot_username and reply.from_user.username == bot_username:
            return True
    # Mention entity scanning. Telegram surfaces @bot mentions as 'mention'
    # entities; explicit user mentions of the bot (set up via t.me/<bot>)
    # come as 'text_mention' with `user.is_bot=True`.
    entities = (message.entities or []) + (message.caption_entities or [])
    if not entities:
        return False
    for entity in entities:
        if entity.type == "mention" and bot_username:
            mention_text = text[entity.offset : entity.offset + entity.length]
            if mention_text.lower() == f"@{bot_username.lower()}":
                return True
        if entity.type == "text_mention":
            mentioned = getattr(entity, "user", None)
            if mentioned is not None and getattr(mentioned, "is_bot", False):
                if bot_username and mentioned.username == bot_username:
                    return True
    return False


def strip_bot_mention(
    text: str,
    bot_username: str | None,
    group_triggers: tuple[str, ...] = (),
) -> str:
    """Remove a leading `@bot_username` mention OR trigger-word alias.

    Without this, the model sees its own handle/alias in every group message
    and may try to address itself. We try `@<bot>` first (canonical) then
    fall back to the trigger words. Inline mentions (mid-sentence) are left
    as-is — rare and not worth over-engineering string surgery for.
    """
    if not text:
        return text
    stripped = text.strip()
    if bot_username:
        handle = f"@{bot_username}"
        if stripped.lower().startswith(handle.lower()):
            return stripped[len(handle):].lstrip()
    if group_triggers:
        return strip_group_trigger(stripped, group_triggers)
    return text


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return

    bot_username: str | None = context.application.bot_data.get("bot_username")
    if not should_engage_in_group(update, bot_username, cfg.group_triggers):
        logger.debug(
            "ignoring group-chat text without mention chat_id=%s user_id=%s",
            update.effective_chat.id,
            update.effective_user.id,
        )
        return

    chat_id = update.effective_chat.id
    raw_text = update.message.text or ""
    user_text = strip_bot_mention(raw_text, bot_username, cfg.group_triggers)
    logger.info(
        "turn start principal=%s chat_id=%s chat_type=%s len=%d",
        principal.name,
        chat_id,
        update.effective_chat.type if update.effective_chat else "?",
        len(user_text),
    )

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        # A typing indicator failure should not abort the turn.
        logger.debug("send_chat_action failed", exc_info=True)

    orch: Orchestrator = context.application.bot_data["orch"]
    try:
        response = await orch.handle_turn(chat_id, user_text, principal=principal)
    except Exception:
        logger.exception("orchestrator failure chat_id=%s", chat_id)
        response = "Sorry — something went wrong on my end. Check the logs."

    await update.message.reply_text(response[:TELEGRAM_MSG_LIMIT])
