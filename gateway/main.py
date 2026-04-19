"""Yunam gateway — Telegram long-polling echo bot (Phase 0-7).

Only replies to messages from TELEGRAM_ALLOWED_USER_ID. Every other
sender is silently ignored (logged at WARNING).
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Quiet down python-telegram-bot's noisy HTTP layer
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("yunam.gateway")

# Fail fast on missing config — KeyError beats a bot that silently answers nobody.
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])


def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        logger.warning("update with no effective_user: %s", update.update_id)
        return False
    if user.id != ALLOWED_USER_ID:
        logger.warning(
            "unauthorized access: user_id=%s username=%s",
            user.id,
            user.username,
        )
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    logger.info("/start from user_id=%s", update.effective_user.id)
    await update.message.reply_text("Yunam online")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    text = update.message.text or ""
    logger.info("echo to user_id=%s: %r", update.effective_user.id, text)
    await update.message.reply_text(text)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    logger.info("gateway starting; allowlist user_id=%s", ALLOWED_USER_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
