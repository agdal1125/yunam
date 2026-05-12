"""Attachment handler — receive, batch, and process Telegram file uploads.

Handles the full attachment lifecycle:
  1. Receive individual photos/docs/videos/voice/audio/animations
  2. Buffer media-group (album) uploads with a settle timer
  3. Route to save or orchestrator based on caption content
  4. Build multimodal user_content blocks for inline vision

The media-group batching logic (AttachmentBatch + settle timer) is the most
complex part of the gateway — it exists because Telegram delivers album
photos as separate updates with a shared `media_group_id`, and we need to
wait for all of them before processing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from yunam.auth import (
    is_authorized_chat,
    log_unauthorized_chat,
    resolve_principal,
)
from yunam.config import Config, Principal
from yunam.orchestrator import Orchestrator
from yunam.sessions import SessionStore
from yunam.tools.attachments import AttachmentTools
from yunam.vision import image_content_block, is_inline_image

from ._helpers import TELEGRAM_MSG_LIMIT, send_reply, strip_command_prefix
from .text import should_engage_in_group

logger = logging.getLogger("yunam.gateway")

MEDIA_GROUP_SETTLE_SECONDS = 1.25
MAX_INLINE_ATTACHMENT_IMAGES = 10


@dataclass
class AttachmentBatch:
    chat_id: int
    media_group_id: str | None
    pending_ids: list[int] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    caption: str | None = None
    reply_to_message_id: int | None = None
    task: asyncio.Task[None] | None = None
    # First principal in the album wins — Telegram media groups can only have
    # one author anyway. Stored here so when the settle timer fires and we
    # process the batch, we still know who to pass to the orchestrator.
    principal: Principal | None = None


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
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return

    message = update.message
    if message is None:
        return

    bot_username: str | None = context.application.bot_data.get("bot_username")
    if not should_engage_in_group(update, bot_username, cfg.group_triggers):
        logger.debug(
            "ignoring group-chat attachment without mention chat_id=%s user_id=%s",
            update.effective_chat.id,
            update.effective_user.id,
        )
        return

    meta = _extract_attachment(message)
    if meta is None:
        return

    chat_id = update.effective_chat.id
    caption = message.caption
    media_group_id = getattr(message, "media_group_id", None)
    store: SessionStore = context.application.bot_data["store"]

    # Always stash in `pending_attachments` — cheap, defers download until /save.
    pending_id = await store.add_pending_attachment(
        chat_id=chat_id,
        file_id=meta["file_id"],
        file_unique_id=meta["file_unique_id"],
        media_group_id=media_group_id,
        kind=meta["kind"],
        file_name=meta["file_name"],
        mime_type=meta["mime_type"],
        file_size=meta["file_size"],
        caption=caption,
    )
    logger.info(
        "attachment received chat_id=%s kind=%s file_id=%s name=%s size=%s pending_id=%s media_group_id=%s",
        chat_id,
        meta["kind"],
        meta["file_id"][:16] + "...",
        meta["file_name"],
        meta["file_size"],
        pending_id,
        media_group_id,
    )

    # Captions are instructions. Route them once per attachment or media group.
    item = {
        **meta,
        "pending_id": pending_id,
        "caption": caption,
        "media_group_id": media_group_id,
    }
    if media_group_id:
        _queue_media_group_item(
            context,
            chat_id=chat_id,
            media_group_id=media_group_id,
            item=item,
            caption=caption,
            reply_to_message_id=message.message_id,
            principal=principal,
        )
        return

    await _process_attachment_batch(
        context.application.bot,
        context.application.bot_data,
        chat_id=chat_id,
        pending_ids=[pending_id],
        items=[item],
        caption=caption,
        media_group_id=None,
        reply_to_message_id=message.message_id,
        principal=principal,
    )
    return


# ---- media-group batching ---------------------------------------------------


def _strip_save_prefix(caption: str | None) -> str | None:
    """Return the caption with the leading `/save` command stripped, or None if empty."""
    if caption is None:
        return None
    stripped = caption.strip()
    if not stripped.lower().startswith("/save"):
        return caption
    remainder = stripped[len("/save"):].lstrip()
    return remainder or None


def _queue_media_group_item(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    media_group_id: str,
    item: dict[str, Any],
    caption: str | None,
    reply_to_message_id: int,
    principal: Principal | None = None,
) -> None:
    batches: dict[str, AttachmentBatch] = context.application.bot_data.setdefault(
        "media_group_batches", {}
    )
    key = f"{chat_id}:{media_group_id}"
    batch = batches.get(key)
    if batch is None:
        batch = AttachmentBatch(
            chat_id=chat_id, media_group_id=media_group_id, principal=principal
        )
        batches[key] = batch
    elif batch.principal is None and principal is not None:
        batch.principal = principal
    batch.pending_ids.append(int(item["pending_id"]))
    batch.items.append(item)
    if caption and caption.strip():
        batch.caption = caption
        batch.reply_to_message_id = reply_to_message_id
    elif batch.reply_to_message_id is None:
        batch.reply_to_message_id = reply_to_message_id

    if batch.task is not None and not batch.task.done():
        batch.task.cancel()
    batch.task = asyncio.create_task(
        _flush_media_group_batch(context.application, key),
        name=f"yunam-media-group-{key}",
    )


async def _flush_media_group_batch(application, key: str) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_SETTLE_SECONDS)
    except asyncio.CancelledError:
        return
    batches: dict[str, AttachmentBatch] = application.bot_data.setdefault(
        "media_group_batches", {}
    )
    batch = batches.pop(key, None)
    if batch is None:
        return
    try:
        await _process_attachment_batch(
            application.bot,
            application.bot_data,
            chat_id=batch.chat_id,
            pending_ids=batch.pending_ids,
            items=batch.items,
            caption=batch.caption,
            media_group_id=batch.media_group_id,
            reply_to_message_id=batch.reply_to_message_id,
            principal=batch.principal,
        )
    except Exception:
        logger.exception("media group batch processing failed key=%s", key)


# ---- attachment processing ---------------------------------------------------


async def _process_attachment_batch(
    bot,
    bot_data: dict[str, Any],
    *,
    chat_id: int,
    pending_ids: list[int],
    items: list[dict[str, Any]],
    caption: str | None,
    media_group_id: str | None,
    reply_to_message_id: int | None,
    principal: Principal | None = None,
) -> None:
    instruction = (caption or "").strip()
    if instruction.lower().startswith("/save"):
        await _commit_pending_ids_and_send(
            bot,
            bot_data,
            chat_id=chat_id,
            pending_ids=pending_ids,
            media_group_id=media_group_id,
            caption_override=_strip_save_prefix(instruction),
            reply_to_message_id=reply_to_message_id,
        )
        return

    if not instruction:
        await _commit_pending_ids_and_send(
            bot,
            bot_data,
            chat_id=chat_id,
            pending_ids=pending_ids,
            media_group_id=media_group_id,
            caption_override=None,
            reply_to_message_id=reply_to_message_id,
        )
        return

    orch_key = "orch"
    if instruction.lower().startswith("/think"):
        orch_key = "deep_orch"
        instruction = strip_command_prefix(instruction, "/think")
        if not instruction:
            await send_reply(
                bot,
                chat_id,
                "Usage: /think <your question>",
                reply_to_message_id=reply_to_message_id,
            )
            return

    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        logger.debug("send_chat_action failed", exc_info=True)

    user_text = _build_attachment_user_text(
        instruction,
        pending_ids=pending_ids,
        items=items,
        media_group_id=media_group_id,
    )
    user_content = await _build_attachment_user_content(
        bot,
        instruction,
        pending_ids=pending_ids,
        items=items,
        media_group_id=media_group_id,
    )
    orch: Orchestrator = bot_data[orch_key]
    try:
        response = await orch.handle_turn(
            chat_id,
            user_text,
            user_content=user_content,
            principal=principal,
        )
    except Exception:
        logger.exception("attachment orchestrator failure chat_id=%s", chat_id)
        response = "처리 중 문제가 생겼어. 로그를 확인해줘."
    await send_reply(
        bot,
        chat_id,
        response[:TELEGRAM_MSG_LIMIT],
        reply_to_message_id=reply_to_message_id,
    )


async def _commit_pending_ids_and_send(
    bot,
    bot_data: dict[str, Any],
    *,
    chat_id: int,
    pending_ids: list[int],
    media_group_id: str | None,
    caption_override: str | None,
    reply_to_message_id: int | None,
) -> None:
    attachments: AttachmentTools | None = bot_data.get("attachments")
    if attachments is None:
        await send_reply(
            bot,
            chat_id,
            "attachments not configured on this server.",
            reply_to_message_id=reply_to_message_id,
        )
        return
    try:
        saved = await attachments.commit_pending_attachments(
            chat_id=chat_id,
            pending_ids=pending_ids,
            media_group_id=media_group_id,
            caption_override=caption_override,
        )
    except Exception:
        logger.exception("commit_pending_attachments failed chat_id=%s", chat_id)
        await send_reply(
            bot,
            chat_id,
            "저장에 실패했어. 로그를 확인해줘.",
            reply_to_message_id=reply_to_message_id,
        )
        return
    if not saved:
        await send_reply(
            bot,
            chat_id,
            "저장할 최근 첨부를 찾지 못했어. 파일을 다시 보내줘.",
            reply_to_message_id=reply_to_message_id,
        )
        return
    paths = ", ".join(sf.relpath for sf in saved[:5])
    more = f" 외 {len(saved) - 5}개" if len(saved) > 5 else ""
    await send_reply(
        bot,
        chat_id,
        f"{len(saved)}개 저장했어: {paths}{more}",
        reply_to_message_id=reply_to_message_id,
    )


# ---- user_content builders --------------------------------------------------


def _build_attachment_user_text(
    instruction: str,
    *,
    pending_ids: list[int],
    items: list[dict[str, Any]],
    media_group_id: str | None,
) -> str:
    lines = [
        "The user sent Telegram attachment(s) with this instruction:",
        instruction,
        "",
        f"Pending attachment ids for this turn: {', '.join(str(pid) for pid in pending_ids)}",
    ]
    if media_group_id:
        lines.append(f"Telegram media_group_id for this turn: {media_group_id}")
    lines.append("Attachment metadata:")
    for idx, item in enumerate(items, start=1):
        lines.append(
            f"{idx}. pending_id={item['pending_id']} kind={item['kind']} "
            f"mime={item.get('mime_type') or 'unknown'} "
            f"name={item.get('file_name') or '(none)'}"
        )
    lines.append(
        "When saving this upload, use save_attachments with the pending ids above. "
        "When extracting text from images, inspect the attached image blocks or use "
        "extract_attachment_text with the same pending ids."
    )
    return "\n".join(lines)


async def _build_attachment_user_content(
    bot,
    instruction: str,
    *,
    pending_ids: list[int],
    items: list[dict[str, Any]],
    media_group_id: str | None,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": _build_attachment_user_text(
                instruction,
                pending_ids=pending_ids,
                items=items,
                media_group_id=media_group_id,
            ),
        }
    ]
    inline_count = 0
    for idx, item in enumerate(items, start=1):
        if inline_count >= MAX_INLINE_ATTACHMENT_IMAGES:
            content.append(
                {
                    "type": "text",
                    "text": "Additional images omitted from inline vision context; use extract_attachment_text for remaining pending ids.",
                }
            )
            break
        if not is_inline_image(item.get("kind"), item.get("mime_type")):
            continue
        label = (
            f"Image {idx}: pending_id={item['pending_id']} "
            f"kind={item['kind']} mime={item.get('mime_type') or 'unknown'}"
        )
        try:
            tg_file = await bot.get_file(item["file_id"])
            data = bytes(await tg_file.download_as_bytearray())
            content.append({"type": "text", "text": label})
            content.append(image_content_block(data, item.get("mime_type")))
            inline_count += 1
        except Exception:
            logger.exception("inline image download failed pending_id=%s", item["pending_id"])
            content.append({"type": "text", "text": f"{label}: inline download failed"})
    return content
