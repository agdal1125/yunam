"""Handler registration — single entry point for main.py to wire all handlers.

Keeps handler registration order in one place and out of the composition root.
Order within PTB matters only for overlapping filters; our filters are disjoint
(commands vs. attachments vs. plain text), so the order here is organizational.
"""

from __future__ import annotations

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from .attachments import on_attachment
from .commands import on_chatid, on_diary, on_save, on_think, start
from .text import on_text


def register_handlers(app: Application) -> None:
    """Register all Telegram handlers in deterministic order."""
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("save", on_save))
    app.add_handler(CommandHandler("think", on_think))
    app.add_handler(CommandHandler("chatid", on_chatid))
    app.add_handler(CommandHandler("diary", on_diary))
    # Attachment handler — matches any file-bearing message type.
    attachment_filter = (
        filters.PHOTO
        | filters.Document.ALL
        | filters.VIDEO
        | filters.VOICE
        | filters.AUDIO
        | filters.ANIMATION
    )
    app.add_handler(MessageHandler(attachment_filter, on_attachment))
    # Catch-all for plain text (non-command, non-attachment).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
