"""Telegram command handlers — /start, /help, /save, /think, /diary, /newsletter, /chatid.

Each handler follows the same pattern: resolve principal → check auth →
extract args → dispatch → reply. Dependencies come from `bot_data`.

`/help` is intentionally zero-token: it returns the module-level `HELP_TEXT`
string directly through PTB, never touching the orchestrator. Updating it
is a one-line code edit; we keep it separate from each skill's
`SYSTEM_PROMPT_FRAGMENT` because the audiences differ (agent vs user).
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


# Static — zero Anthropic tokens to render. Update when adding a slash command
# or a new skill. Kept terse; the model's SYSTEM_PROMPT_FRAGMENT per skill
# remains the source of truth for "when do I call this tool".
HELP_TEXT = """\
유남 사용 안내

명령어:
  /start — 인사 + 권한 확인
  /help — 이 안내
  /save [메모] — 직전에 보낸 첨부파일 저장 + 인덱싱
  /think <질문> — Opus 4.7로 깊게 생각해서 답변 (느리고 비쌈)
  /diary [내용] — 일기 / 하루 정리. 내용 없이 보내면 프롬프트만 전송
  /newsletter [hours] — 큐레이션 디지스트 즉시 발송 (기본 룩백 24h)
  /chatid — 현재 채팅 ID 표시

기능 (그냥 말로 요청하면 알아서 호출):
  obsidian 볼트 — 읽기/쓰기/검색 + 그래프 (백링크, 태그, outgoing)
  파일 — 사진/문서 저장, 캡션·설명·내용 의미 검색, 다시 전송
  웹 — 검색 + 페이지 fetch
  한국 — 대기질, 택배 추적
  리마인더 — "내일 8시 약 먹어라" 같이 자연어로 예약/조회/취소
  메모리 — 과거 대화 의미 검색 ("지난번에 뭐 얘기했지")
  캘린더 — 일정 조회/추가 (MCP, 설정 시)
  종목 수급 — 한국 주식 수급 분석 (MCP, 설정 시)
  큐레이션 — 자동 뉴스 수집 + 21시 KST 뉴스레터, 관심사 편집 가능
  API 비용 — usage_summary, usage_breakdown, cost_alert_status

빠른 팁:
  - 첨부 보낼 때 "/save 캡션" 한 번에 또는 캡션 없이 /save
  - 비밀 얘기는 "비밀이야" 한 마디로 자동 마킹 (yoolim에게 숨김)
  - 답변이 얕다 싶으면 같은 질문에 /think 붙여서 다시
"""


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the static help text. Zero Anthropic tokens — no orchestrator turn."""
    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return
    await update.message.reply_text(HELP_TEXT)


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


async def on_newsletter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/newsletter [hours]` — build + send the curation newsletter now.

    Without args, looks back 24 hours (same as the 21:00 cron). Pass an
    integer to override the window: `/newsletter 6` for the last 6 hours.
    Useful for testing without waiting until 21:00.
    """
    from yunam.runners.digester import Digester
    from yunam.runners.pusher import CurationPusher

    cfg: Config = context.application.bot_data["cfg"]
    principal = resolve_principal(update, cfg)
    if principal is None:
        return
    if not is_authorized_chat(update, cfg):
        log_unauthorized_chat(update, principal)
        return

    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    try:
        lookback_hours = int(parts[1].strip()) if len(parts) > 1 else 24
    except ValueError:
        await update.message.reply_text("Usage: /newsletter [hours]  (default 24)")
        return
    lookback_hours = max(1, min(168, lookback_hours))

    store: SessionStore = context.application.bot_data["store"]
    chat_id = update.effective_chat.id
    digester = Digester(store)
    body, item_ids = await digester.build_newsletter(lookback_hours=lookback_hours)
    if not body:
        await update.message.reply_text(
            f"디지스트 큐가 비어있어요 (지난 {lookback_hours}시간 기준). "
            "큐레이터가 한 번도 안 돌았거나, 임계치 위로 올라온 항목이 없네요."
        )
        return

    pusher = CurationPusher(context.bot, store, owner_chat_id=chat_id)
    ok = await pusher.push_newsletter(body, item_ids)
    logger.info(
        "/newsletter principal=%s chat_id=%s lookback=%dh items=%d ok=%s",
        principal.name, chat_id, lookback_hours, len(item_ids), ok,
    )
    if not ok:
        await update.message.reply_text("뉴스레터 발송에 실패했어. 로그 확인 필요.")


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
