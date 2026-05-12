"""Authorization helpers — principal + chat allowlists + group triggers.

Lives outside `main.py` so unit tests can import these without pulling in
python-telegram-bot. Functions here read attributes off Telegram Update
objects but never construct them or call PTB APIs, so the module imports
cleanly on a stock Python install (PTB only present in the gateway container).
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config, Principal

logger = logging.getLogger("yunam.auth")


# Default vocative-style triggers. Acts as an in-process alternative to
# requiring jaekeun to BotFather-rename the bot to a shorter handle. A group
# message starting with one of these (followed by space, punctuation, or end
# of message) is treated as if `@<bot_username>` had been typed at the front.
# Comparison is case-insensitive on the ASCII forms; Hangul forms are matched
# exactly. Override via `YUNAM_GROUP_TRIGGERS` env var (CSV).
DEFAULT_GROUP_TRIGGERS: tuple[str, ...] = (
    "yunam",
    "Yunam",
    "유남",
    "유남아",
    "유남이",
)

# Punctuation that may follow a trigger before the actual request body.
# Halfwidth + fullwidth (Korean keyboards emit fullwidth in some flows).
_TRIGGER_TAIL_PUNCT: frozenset[str] = frozenset(",.!?~-,。!?…")


def match_group_trigger(text: str, triggers: tuple[str, ...]) -> str | None:
    """Return the matched trigger if `text` starts with one, else None.

    A match requires the trigger to sit at the very start of `text` and to
    be followed by either end-of-message OR a whitespace/punctuation char.
    This rules out false positives like `yunamcorp` matching trigger `yunam`.

    Comparison is case-insensitive — both the candidate text and each trigger
    are lowered before comparison. (Hangul lowercasing is a no-op, so 유남아
    matches 유남아 only.)
    """
    if not text or not triggers:
        return None
    lowered = text.lower()
    for trigger in triggers:
        tlow = trigger.lower()
        if not tlow:
            continue
        if not lowered.startswith(tlow):
            continue
        tail_idx = len(tlow)
        if tail_idx == len(text):
            return trigger
        next_ch = text[tail_idx]
        if next_ch.isspace() or next_ch in _TRIGGER_TAIL_PUNCT:
            return trigger
    return None


def strip_group_trigger(text: str, triggers: tuple[str, ...]) -> str:
    """Strip a leading trigger word + adjacent punctuation/whitespace.

    `유남아 일정 알려줘` → `일정 알려줘`
    `yunam, 잘 지냈어?` → `잘 지냈어?`
    No-match → returned as-is. Used so the model doesn't see the trigger word
    in the user message (which would confuse it about who's being addressed).
    """
    matched = match_group_trigger(text, triggers)
    if matched is None:
        return text
    rest = text[len(matched):]
    # Strip leading punctuation+whitespace from the remainder.
    return rest.lstrip("".join(_TRIGGER_TAIL_PUNCT) + " \t\n\r")


def resolve_principal(update: Any, cfg: Config) -> Principal | None:
    """Return the Principal for `update`'s sender, or None to ignore the message.

    Acceptance rule: the sender's user_id must be in `cfg.principals`. This
    works identically for 1:1 DMs (chat_id == user_id) and group chats
    (chat_id != user_id, sender's user_id still maps to a Principal).

    Group-chat mention gating happens in `_should_engage_in_group`, separate
    from authorization — a known principal can still send a non-mention
    message in a group, but yunam ignores it. Unknown-user attempts log at
    WARNING; chat-allowlist misses use a different log line.
    """
    user = getattr(update, "effective_user", None)
    if user is None:
        logger.warning("update with no effective_user: %s", getattr(update, "update_id", "?"))
        return None
    principal = cfg.principals_by_id.get(user.id)
    if principal is None:
        logger.warning(
            "unauthorized access: user_id=%s username=%s chat_id=%s",
            user.id,
            getattr(user, "username", None),
            getattr(getattr(update, "effective_chat", None), "id", "?"),
        )
        return None
    return principal


def is_authorized_chat(update: Any, cfg: Config) -> bool:
    """Return True iff yunam should accept work in this chat.

    Two-tier policy that complements the principal allowlist:
      - Private DMs: always allowed (chat_id == user_id; principal check
        already vetted the sender).
      - Groups / supergroups / channels: must appear in `cfg.allowed_chats`.
        An empty `allowed_chats` (default) means group chats are disabled
        entirely — the safest default while jaekeun is still cataloguing
        which group rooms yunam belongs in.
    """
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return False
    if chat.type == "private":
        return True
    if chat.id in cfg.allowed_chats:
        return True
    return False


def log_unauthorized_chat(update: Any, principal: Principal) -> None:
    """Emit one WARN line per unauthorized-chat attempt for chat_id discovery.

    Kept separate from `is_authorized_chat` so handlers can decide which
    paths warrant the noise — `/chatid` shouldn't log this since it's the
    user's tool for resolving the very situation."""
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return
    title = getattr(chat, "title", None) or getattr(chat, "username", None) or "?"
    logger.warning(
        "untrusted chat: principal=%s chat_id=%s type=%s title=%r — "
        "add to YUNAM_ALLOWED_CHATS to enable",
        principal.name,
        chat.id,
        chat.type,
        title,
    )
