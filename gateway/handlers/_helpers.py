"""Shared constants and utilities for Telegram handlers.

Small helpers that multiple handler modules need. Kept thin — if a helper
is only used by one handler module, it belongs in that module, not here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("yunam.gateway")

TELEGRAM_MSG_LIMIT = 4096


def strip_command_prefix(text: str, command: str) -> str:
    """Strip a leading `/command` from text, returning the remainder."""
    stripped = text.strip()
    if stripped.lower().startswith(command.lower()):
        return stripped[len(command):].lstrip()
    return stripped


async def send_reply(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
) -> None:
    del reply_to_message_id  # Keep callers simple; plain chat replies are most compatible.
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
    await bot.send_message(**kwargs)
