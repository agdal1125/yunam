"""Environment and path configuration for Yunam.

All required env vars are read here with `os.environ[...]` (KeyError if missing) —
no silent `.get()` fallbacks. Optional settings use env lookups with explicit defaults.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Principal:
    """One human authorized to talk to Yunam.

    Identity comes from Telegram's numeric user_id. `name` is the
    short display name (e.g. 'jaekeun', 'yoolim') that appears in
    `[from: <name>]` history markers and in private-visibility
    bookkeeping. `is_owner` flags the deployment owner — defaults to
    the daily retrospective recipient and the canonical jaekeun for
    legacy code paths until they're audited individually.
    """

    user_id: int
    name: str
    is_owner: bool = False


@dataclass(frozen=True)
class Config:
    telegram_token: str
    principals: tuple[Principal, ...]
    allowed_chats: tuple[int, ...]
    group_triggers: tuple[str, ...]
    anthropic_api_key: str
    voyage_api_key: str
    vault_path: Path
    filevault_path: Path
    db_path: Path
    timezone: str
    nudge_sweeper_enabled: bool
    nudge_sweep_interval_seconds: float
    jina_api_key: str | None
    sweettracker_api_key: str | None
    gcal_mcp_url: str | None

    @property
    def principals_by_id(self) -> dict[int, Principal]:
        return {p.user_id: p for p in self.principals}

    @property
    def owner(self) -> Principal:
        """Return the Principal flagged is_owner. Falls back to the first one
        if none is flagged — keeps legacy single-user .env files working."""
        for p in self.principals:
            if p.is_owner:
                return p
        return self.principals[0]


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_principals() -> tuple[Principal, ...]:
    """Resolve the principal allowlist from env.

    Two accepted formats, checked in order:
      1. `YUNAM_PRINCIPALS` — JSON array of objects with keys
         `user_id`/`name`/`is_owner?`. Preferred for multi-principal
         deployments.
      2. `TELEGRAM_ALLOWED_USER_ID` — single int, legacy single-user
         shape. Synthesized as one Principal named 'jaekeun' with
         `is_owner=True` so existing deployments keep working without an
         .env edit.

    Fail-fast: if neither is set, `KeyError` propagates. If both are set,
    the JSON form wins.
    """
    raw = os.environ.get("YUNAM_PRINCIPALS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"YUNAM_PRINCIPALS is not valid JSON: {e}") from e
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("YUNAM_PRINCIPALS must be a non-empty JSON array")
        out: list[Principal] = []
        seen: set[int] = set()
        for entry in parsed:
            if not isinstance(entry, dict):
                raise ValueError(f"YUNAM_PRINCIPALS entry must be an object: {entry!r}")
            try:
                user_id = int(entry["user_id"])
                name = str(entry["name"]).strip()
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"YUNAM_PRINCIPALS entry missing user_id/name: {entry!r}"
                ) from e
            if not name:
                raise ValueError(f"YUNAM_PRINCIPALS entry has empty name: {entry!r}")
            if user_id in seen:
                raise ValueError(f"YUNAM_PRINCIPALS has duplicate user_id={user_id}")
            seen.add(user_id)
            is_owner = bool(entry.get("is_owner", False))
            out.append(Principal(user_id=user_id, name=name, is_owner=is_owner))
        if not any(p.is_owner for p in out):
            # Default the first declared principal to owner so daily
            # retrospective + scheduler still have a clear target.
            out[0] = Principal(
                user_id=out[0].user_id, name=out[0].name, is_owner=True
            )
        return tuple(out)
    # Legacy single-user fallback.
    legacy = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    if not legacy:
        raise KeyError(
            "Set YUNAM_PRINCIPALS (preferred, JSON array) or "
            "TELEGRAM_ALLOWED_USER_ID (legacy single-user)"
        )
    return (Principal(user_id=int(legacy), name="jaekeun", is_owner=True),)


def _load_allowed_chats() -> tuple[int, ...]:
    """Resolve the group-chat allowlist from `YUNAM_ALLOWED_CHATS`.

    Empty / unset → empty tuple → group chats are entirely disabled. DMs are
    not affected (they're allowed implicitly when the sender's user_id is in
    `principals`); only groups/supergroups/channels need to appear here.

    Two accepted forms:
      - JSON array: `[-1001234567890, -1009876543210]`
      - CSV: `-1001234567890,-1009876543210`
    """
    raw = os.environ.get("YUNAM_ALLOWED_CHATS", "").strip()
    if not raw:
        return ()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"YUNAM_ALLOWED_CHATS is not valid JSON: {e}") from e
        if not isinstance(parsed, list):
            raise ValueError("YUNAM_ALLOWED_CHATS JSON form must be an array")
        ids = parsed
    else:
        ids = [s.strip() for s in raw.split(",") if s.strip()]
    out: list[int] = []
    seen: set[int] = set()
    for entry in ids:
        try:
            cid = int(entry)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"YUNAM_ALLOWED_CHATS entry is not an integer: {entry!r}"
            ) from e
        if cid in seen:
            raise ValueError(f"YUNAM_ALLOWED_CHATS has duplicate chat_id={cid}")
        seen.add(cid)
        out.append(cid)
    return tuple(out)


def _load_group_triggers() -> tuple[str, ...]:
    """Resolve the in-process group-chat trigger word list from env.

    Empty / unset → fall back to `DEFAULT_GROUP_TRIGGERS` (yunam / 유남 / 유남아).
    Set `YUNAM_GROUP_TRIGGERS=` (literal empty assignment) to disable triggers
    entirely and require @bot mentions like the original behavior.

    CSV form: `YUNAM_GROUP_TRIGGERS=yunam,유남,유남아,Yuni`. Whitespace around
    each entry is stripped. Order isn't significant — the matcher walks the
    list and the first match wins, which is fine because triggers are
    expected to be disjoint vocatives.
    """
    # Imported lazily so config.py doesn't depend on auth.py at module load
    # time (auth.py already imports config).
    from .auth import DEFAULT_GROUP_TRIGGERS

    raw = os.environ.get("YUNAM_GROUP_TRIGGERS")
    if raw is None:
        # Truly unset → use defaults. Note this differs from "set to empty
        # string" which the user uses to mean "disable triggers".
        return DEFAULT_GROUP_TRIGGERS
    if not raw.strip():
        return ()
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def load_config() -> Config:
    vault_path = Path(os.environ.get("YUNAM_VAULT_PATH", "/data/obsidian")).resolve()
    filevault_path = Path(os.environ.get("YUNAM_FILEVAULT_PATH", "/data/filevault")).resolve()
    db_path = Path(os.environ.get("YUNAM_DB_PATH", "/data/yunam/yunam.db")).resolve()
    nudge_interval = float(os.environ.get("YUNAM_NUDGE_SWEEP_INTERVAL", "60"))

    jina_api_key_raw = os.environ.get("JINA_API_KEY", "").strip()
    sweettracker_api_key_raw = os.environ.get("SWEETTRACKER_API_KEY", "").strip()
    gcal_mcp_url_raw = os.environ.get("YUNAM_GCAL_MCP_URL", "").strip()

    return Config(
        telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
        principals=_load_principals(),
        allowed_chats=_load_allowed_chats(),
        group_triggers=_load_group_triggers(),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        voyage_api_key=os.environ["VOYAGE_API_KEY"],
        vault_path=vault_path,
        filevault_path=filevault_path,
        db_path=db_path,
        timezone=os.environ.get("YUNAM_TIMEZONE", "Asia/Seoul"),
        nudge_sweeper_enabled=_parse_bool(
            os.environ.get("YUNAM_NUDGE_SWEEPER_ENABLED", "false")
        ),
        nudge_sweep_interval_seconds=nudge_interval,
        jina_api_key=jina_api_key_raw or None,
        sweettracker_api_key=sweettracker_api_key_raw or None,
        gcal_mcp_url=gcal_mcp_url_raw or None,
    )


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet the HTTP layers used by python-telegram-bot and anthropic.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
