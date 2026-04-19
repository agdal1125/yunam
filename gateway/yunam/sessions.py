"""SQLite session store for Yunam.

Persists conversation history (plain text only — no Claude content blocks) and a
brief log of tool calls per turn. One session per Telegram chat_id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    chat_id       INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL REFERENCES sessions(chat_id),
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id, created_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL REFERENCES sessions(chat_id),
    turn_message_id INTEGER REFERENCES messages(id),
    name            TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    result_preview  TEXT,
    is_error        INTEGER NOT NULL DEFAULT 0,
    elapsed_ms      INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_chat ON tool_calls(chat_id, created_at);
"""

HISTORY_LIMIT = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    result_preview: str
    is_error: bool
    elapsed_ms: int


class SessionStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, db_path: Path) -> "SessionStore":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(db_path)
        store._db = await aiosqlite.connect(str(db_path))
        await store._db.executescript(_SCHEMA)
        await store._db.commit()
        return store

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SessionStore is not open")
        return self._db

    async def load_history(self, chat_id: int) -> list[dict[str, str]]:
        """Return last HISTORY_LIMIT messages as [{role, content}] in chronological order.

        The current turn's user message is NOT included — the orchestrator appends it.
        """
        db = self._conn
        async with db.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at, id FROM messages
                WHERE chat_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            ) ORDER BY created_at ASC, id ASC
            """,
            (chat_id, HISTORY_LIMIT),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    async def persist_turn(
        self,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        tool_calls: list[ToolCall],
    ) -> None:
        """Write one full turn (user + assistant + tool calls) as a single transaction."""
        db = self._conn
        now = _now_iso()
        await db.execute("BEGIN")
        try:
            await db.execute(
                """
                INSERT INTO sessions (chat_id, created_at, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
                """,
                (chat_id, now, now),
            )
            await db.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (chat_id, user_text, now),
            )
            async with db.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                (chat_id, assistant_text, now),
            ) as cur:
                assistant_msg_id = cur.lastrowid

            if tool_calls:
                await db.executemany(
                    """
                    INSERT INTO tool_calls
                        (chat_id, turn_message_id, name, input_json,
                         result_preview, is_error, elapsed_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chat_id,
                            assistant_msg_id,
                            tc.name,
                            json.dumps(tc.input, ensure_ascii=False),
                            tc.result_preview,
                            1 if tc.is_error else 0,
                            tc.elapsed_ms,
                            now,
                        )
                        for tc in tool_calls
                    ],
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
