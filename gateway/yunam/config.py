"""Environment and path configuration for Yunam.

All required env vars are read here with `os.environ[...]` (KeyError if missing) —
no silent `.get()` fallbacks. Optional settings use env lookups with explicit defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_id: int
    anthropic_api_key: str
    vault_path: Path
    db_path: Path


def load_config() -> Config:
    vault_path = Path(os.environ.get("YUNAM_VAULT_PATH", "/data/obsidian")).resolve()
    db_path = Path(os.environ.get("YUNAM_DB_PATH", "/data/yunam/yunam.db")).resolve()

    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        allowed_user_id=int(os.environ["TELEGRAM_ALLOWED_USER_ID"]),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        vault_path=vault_path,
        db_path=db_path,
    )


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet the HTTP layers used by python-telegram-bot and anthropic.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
