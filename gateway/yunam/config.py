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
    timezone: str
    schedule_enabled: bool
    daily_reflection_hour: int
    daily_reflection_minute: int


def _parse_hhmm(value: str) -> tuple[int, int]:
    # "HH:MM" → (hour, minute). Fail-fast on malformed input (matches the rest of config).
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid HH:MM time: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"HH:MM out of range: {value!r}")
    return hour, minute


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    vault_path = Path(os.environ.get("YUNAM_VAULT_PATH", "/data/obsidian")).resolve()
    db_path = Path(os.environ.get("YUNAM_DB_PATH", "/data/yunam/yunam.db")).resolve()
    hour, minute = _parse_hhmm(os.environ.get("YUNAM_DAILY_REFLECTION_TIME", "22:30"))

    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        allowed_user_id=int(os.environ["TELEGRAM_ALLOWED_USER_ID"]),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        vault_path=vault_path,
        db_path=db_path,
        timezone=os.environ.get("YUNAM_TIMEZONE", "Asia/Seoul"),
        schedule_enabled=_parse_bool(os.environ.get("YUNAM_SCHEDULE_ENABLED", "false")),
        daily_reflection_hour=hour,
        daily_reflection_minute=minute,
    )


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet the HTTP layers used by python-telegram-bot and anthropic.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
