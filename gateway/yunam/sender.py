"""Abstract channel for Yunam to download and send files through Telegram.

The orchestrator should not import `telegram` directly — it uses this narrow
Protocol so tool code stays testable (a FakeSender can satisfy it in the REPL).
`PTBSender` is the production implementation backed by python-telegram-bot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("yunam.sender")


class AttachmentSender(Protocol):
    """Narrow channel for file I/O with Telegram."""

    async def download_to(self, file_id: str, dest: Path) -> int: ...
    async def send_document(
        self, chat_id: int, path: Path, caption: str | None = None
    ) -> None: ...


class PTBSender:
    """`python-telegram-bot` implementation of AttachmentSender.

    Wraps the PTB `Bot` instance that `main.py` creates via `Application.builder()`.
    """

    def __init__(self, bot):
        self._bot = bot

    async def download_to(self, file_id: str, dest: Path) -> int:
        """Fetch a Telegram file by file_id and write it to `dest`. Returns byte count."""
        tg_file = await self._bot.get_file(file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await tg_file.download_to_drive(custom_path=str(dest))
        return dest.stat().st_size

    async def send_document(
        self, chat_id: int, path: Path, caption: str | None = None
    ) -> None:
        """Send `path` back to the user as a Telegram document.

        `send_document` preserves the original bytes (unlike `send_photo`, which
        re-encodes). Good enough as a single-mode return channel for v1.
        """
        with path.open("rb") as f:
            await self._bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=path.name,
                caption=caption,
            )
