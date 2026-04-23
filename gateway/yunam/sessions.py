"""SQLite session store for Yunam.

Persists conversation history (plain text only — no Claude content blocks), a
brief log of tool calls per turn, pending/saved file-attachment metadata, and a
co-located `sqlite-vec` virtual table of multimodal embeddings for semantic
file search. One SQLite database file, one connection, WAL mode.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

logger = logging.getLogger("yunam.sessions")

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

-- Attachments received via Telegram but not yet committed to the filevault.
-- We don't download until /save — `file_id` stays valid on Telegram's side.
CREATE TABLE IF NOT EXISTS pending_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    file_id         TEXT NOT NULL,
    file_unique_id  TEXT,
    kind            TEXT NOT NULL,   -- photo/document/video/voice/audio/animation
    file_name       TEXT,
    mime_type       TEXT,
    file_size       INTEGER,
    caption         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_chat_created ON pending_attachments(chat_id, created_at DESC);

-- Attachments committed to the filevault. One row per saved file.
CREATE TABLE IF NOT EXISTS saved_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    relpath         TEXT NOT NULL UNIQUE,   -- filevault-relative, e.g. "2026-04-21/photo.jpg"
    kind            TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    mime_type       TEXT,
    file_size       INTEGER,
    caption         TEXT,
    description     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_files_chat ON saved_files(chat_id, created_at DESC);

-- Proactive reminders Yunam has scheduled for itself (via the reminders skill).
-- Sweeper polls `fire_at` vs. now and dispatches due rows as Telegram messages.
CREATE TABLE IF NOT EXISTS scheduled_nudges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    fire_at     TEXT NOT NULL,          -- ISO 8601 UTC
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    sent_at     TEXT,                   -- NULL until dispatched; idempotency marker
    cancelled_at TEXT                   -- NULL unless jaekeun cancels before firing
);
CREATE INDEX IF NOT EXISTS idx_nudges_due
    ON scheduled_nudges(fire_at)
    WHERE sent_at IS NULL AND cancelled_at IS NULL;

-- Conversation-turn index. One row per user→assistant exchange, keyed by the
-- assistant message id. The combined text is stored here (duplicated from
-- `messages`) so recall is one JOIN instead of a correlated subquery walking
-- `messages` to reconstruct turn pairs. Vector lives in `message_embeddings`.
CREATE TABLE IF NOT EXISTS message_turns (
    assistant_message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    chat_id              INTEGER NOT NULL,
    user_message_id      INTEGER NOT NULL REFERENCES messages(id),
    user_text            TEXT NOT NULL,
    assistant_text       TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_turns_chat_created
    ON message_turns(chat_id, created_at DESC);
"""

# Voyage `voyage-multimodal-3` returns 1024-dim float vectors.
EMBEDDING_DIM = 1024

# sqlite-vec virtual table schema. vec0 requires its own CREATE because it's a
# virtual table module, not plain SQL DDL — must be issued after the extension
# is loaded.
_VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS file_embeddings USING vec0(
    file_id INTEGER PRIMARY KEY,
    embedding float[{EMBEDDING_DIM}]
);
CREATE VIRTUAL TABLE IF NOT EXISTS message_embeddings USING vec0(
    assistant_message_id INTEGER PRIMARY KEY,
    embedding float[{EMBEDDING_DIM}]
);
"""

HISTORY_LIMIT = 20

# Schema version — bump when a migration is added below, and gate the new step
# on a `version < N` (or column-exists) check so re-runs are no-ops. Every step
# must be idempotent.
DB_USER_VERSION = 4


async def _column_exists(
    db: aiosqlite.Connection, table: str, column: str
) -> bool:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(row[1] == column for row in rows)


async def _migrate(db: aiosqlite.Connection) -> None:
    """Bring the schema up to DB_USER_VERSION. Idempotent for any starting state.

    Fresh DBs run every step; upgraded DBs skip steps whose effect is already
    present. `PRAGMA user_version` is the authoritative marker — the
    column-exists guards exist only so a pre-versioning DB converges cleanly.
    """
    await db.executescript(_SCHEMA)

    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    version = int(row[0]) if row else 0

    # v2: tool_calls gains skill_id + scope (governance bookkeeping).
    if version < 2:
        if not await _column_exists(db, "tool_calls", "skill_id"):
            await db.execute("ALTER TABLE tool_calls ADD COLUMN skill_id TEXT")
        if not await _column_exists(db, "tool_calls", "scope"):
            await db.execute("ALTER TABLE tool_calls ADD COLUMN scope TEXT")

    if version < DB_USER_VERSION:
        await db.execute(f"PRAGMA user_version = {DB_USER_VERSION}")
        logger.info(
            "sessions db migrated from user_version=%d to %d", version, DB_USER_VERSION
        )


def _pack_embedding(vector: list[float]) -> bytes:
    """sqlite-vec expects packed little-endian float32 bytes for `float[N]` columns."""
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding dim mismatch: got {len(vector)}, expected {EMBEDDING_DIM}"
        )
    return struct.pack(f"<{EMBEDDING_DIM}f", *vector)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    result_preview: str
    is_error: bool
    elapsed_ms: int
    # Governance bookkeeping. `skill_id` is None when dispatch failed before the
    # registry could locate the owning skill (e.g. unknown tool name); `scope`
    # tracks which capability the tool claimed, as a string for DB portability.
    skill_id: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class PendingAttachment:
    id: int
    chat_id: int
    file_id: str
    file_unique_id: str | None
    kind: str
    file_name: str | None
    mime_type: str | None
    file_size: int | None
    caption: str | None


@dataclass(frozen=True)
class SavedFile:
    id: int
    chat_id: int
    relpath: str
    kind: str
    file_name: str
    mime_type: str | None
    file_size: int | None
    caption: str | None
    description: str | None
    created_at: str


@dataclass(frozen=True)
class ScheduledNudge:
    id: int
    chat_id: int
    fire_at: str          # ISO 8601 UTC
    message: str
    created_at: str


@dataclass(frozen=True)
class RecalledTurn:
    assistant_message_id: int
    chat_id: int
    user_text: str
    assistant_text: str
    created_at: str
    distance: float


class SessionStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._has_vec = False

    @classmethod
    async def open(cls, db_path: Path) -> "SessionStore":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(db_path)
        store._db = await aiosqlite.connect(str(db_path))
        await _migrate(store._db)
        # Load sqlite-vec for the semantic file-search virtual table. Requires
        # a Python build with `enable_load_extension` — standard on the Debian
        # base image we run under Docker. Graceful fallback if unavailable
        # (e.g. macOS Python without extension support) — semantic file search
        # is disabled but everything else works.
        try:
            await store._db.enable_load_extension(True)
            await store._db.load_extension(sqlite_vec.loadable_path())
            await store._db.enable_load_extension(False)
            await store._db.executescript(_VEC_SCHEMA)
            store._has_vec = True
            logger.info("sqlite-vec loaded; semantic file search enabled")
        except Exception as e:
            logger.warning(
                "sqlite-vec not loaded (%s); semantic file search disabled", e
            )
            store._has_vec = False
        await store._db.commit()
        return store

    @property
    def has_vec(self) -> bool:
        return self._has_vec

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

    async def record_proactive_message(self, chat_id: int, text: str) -> None:
        """Record an assistant-initiated message (e.g. nightly retrospective prompt).

        Writes the session row (upsert) and an `assistant` message, so the next
        user reply's `load_history` sees Yunam's prompt as prior context.
        """
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
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                (chat_id, text, now),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def persist_turn(
        self,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        tool_calls: list[ToolCall],
    ) -> tuple[int, int]:
        """Write one full turn (user + assistant + tool calls + turn index) atomically.

        Returns `(user_message_id, assistant_message_id)` so the caller can
        schedule a background embedding task keyed to the assistant message.
        Does not embed — that's the orchestrator's job.
        """
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
            async with db.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (chat_id, user_text, now),
            ) as cur:
                user_msg_id = cur.lastrowid
            async with db.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                (chat_id, assistant_text, now),
            ) as cur:
                assistant_msg_id = cur.lastrowid
            await db.execute(
                """
                INSERT INTO message_turns
                    (assistant_message_id, chat_id, user_message_id,
                     user_text, assistant_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (assistant_msg_id, chat_id, user_msg_id,
                 user_text, assistant_text, now),
            )

            if tool_calls:
                await db.executemany(
                    """
                    INSERT INTO tool_calls
                        (chat_id, turn_message_id, name, input_json,
                         result_preview, is_error, elapsed_ms, created_at,
                         skill_id, scope)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            tc.skill_id,
                            tc.scope,
                        )
                        for tc in tool_calls
                    ],
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return user_msg_id, assistant_msg_id

    # ---- pending attachments ---------------------------------------------

    async def add_pending_attachment(
        self,
        *,
        chat_id: int,
        file_id: str,
        file_unique_id: str | None,
        kind: str,
        file_name: str | None,
        mime_type: str | None,
        file_size: int | None,
        caption: str | None,
    ) -> int:
        db = self._conn
        now = _now_iso()
        async with db.execute(
            """
            INSERT INTO pending_attachments
                (chat_id, file_id, file_unique_id, kind, file_name,
                 mime_type, file_size, caption, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                file_id,
                file_unique_id,
                kind,
                file_name,
                mime_type,
                file_size,
                caption,
                now,
            ),
        ) as cur:
            new_id = cur.lastrowid
        await db.commit()
        return new_id

    async def latest_pending_attachment(self, chat_id: int) -> PendingAttachment | None:
        db = self._conn
        async with db.execute(
            """
            SELECT id, chat_id, file_id, file_unique_id, kind, file_name,
                   mime_type, file_size, caption
            FROM pending_attachments
            WHERE chat_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return PendingAttachment(*row)

    async def delete_pending_attachment(self, pending_id: int) -> None:
        db = self._conn
        await db.execute("DELETE FROM pending_attachments WHERE id = ?", (pending_id,))
        await db.commit()

    # ---- saved files + embeddings ----------------------------------------

    async def record_saved_file(
        self,
        *,
        chat_id: int,
        relpath: str,
        kind: str,
        file_name: str,
        mime_type: str | None,
        file_size: int | None,
        caption: str | None,
        description: str | None,
        embedding: list[float] | None,
    ) -> int:
        """Insert a `saved_files` row and (if provided) its embedding, atomically."""
        db = self._conn
        now = _now_iso()
        await db.execute("BEGIN")
        try:
            async with db.execute(
                """
                INSERT INTO saved_files
                    (chat_id, relpath, kind, file_name, mime_type,
                     file_size, caption, description, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, relpath, kind, file_name, mime_type,
                 file_size, caption, description, now),
            ) as cur:
                saved_id = cur.lastrowid
            if embedding is not None and self._has_vec:
                await db.execute(
                    "INSERT INTO file_embeddings (file_id, embedding) VALUES (?, ?)",
                    (saved_id, _pack_embedding(embedding)),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return saved_id

    async def search_files_semantic(
        self, query_embedding: list[float], limit: int = 5
    ) -> list[tuple[SavedFile, float]]:
        """KNN search: return (SavedFile, distance) pairs ordered by distance ascending."""
        if not self._has_vec:
            return []
        db = self._conn
        packed = _pack_embedding(query_embedding)
        async with db.execute(
            f"""
            SELECT s.id, s.chat_id, s.relpath, s.kind, s.file_name, s.mime_type,
                   s.file_size, s.caption, s.description, s.created_at, fe.distance
            FROM file_embeddings fe
            JOIN saved_files s ON s.id = fe.file_id
            WHERE fe.embedding MATCH ? AND k = {int(limit)}
            ORDER BY fe.distance
            """,
            (packed,),
        ) as cur:
            rows = await cur.fetchall()
        out: list[tuple[SavedFile, float]] = []
        for row in rows:
            sf = SavedFile(
                id=row[0], chat_id=row[1], relpath=row[2], kind=row[3],
                file_name=row[4], mime_type=row[5], file_size=row[6],
                caption=row[7], description=row[8], created_at=row[9],
            )
            out.append((sf, float(row[10])))
        return out

    # ---- scheduled nudges ------------------------------------------------

    async def add_nudge(
        self, *, chat_id: int, fire_at_iso_utc: str, message: str
    ) -> int:
        """Insert a nudge row and return its id. `fire_at_iso_utc` must be UTC ISO 8601."""
        db = self._conn
        now = _now_iso()
        async with db.execute(
            """
            INSERT INTO scheduled_nudges (chat_id, fire_at, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, fire_at_iso_utc, message, now),
        ) as cur:
            new_id = cur.lastrowid
        await db.commit()
        return new_id

    async def list_due_nudges(self, now_iso_utc: str) -> list[ScheduledNudge]:
        """Return all un-sent, un-cancelled nudges with fire_at <= now."""
        db = self._conn
        async with db.execute(
            """
            SELECT id, chat_id, fire_at, message, created_at
            FROM scheduled_nudges
            WHERE sent_at IS NULL AND cancelled_at IS NULL AND fire_at <= ?
            ORDER BY fire_at ASC
            """,
            (now_iso_utc,),
        ) as cur:
            rows = await cur.fetchall()
        return [ScheduledNudge(*row) for row in rows]

    async def list_pending_nudges(self, chat_id: int) -> list[ScheduledNudge]:
        """Return upcoming (not-yet-fired, not-cancelled) nudges for a chat."""
        db = self._conn
        async with db.execute(
            """
            SELECT id, chat_id, fire_at, message, created_at
            FROM scheduled_nudges
            WHERE chat_id = ? AND sent_at IS NULL AND cancelled_at IS NULL
            ORDER BY fire_at ASC
            """,
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [ScheduledNudge(*row) for row in rows]

    async def mark_nudge_sent(self, nudge_id: int) -> None:
        db = self._conn
        await db.execute(
            "UPDATE scheduled_nudges SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
            (_now_iso(), nudge_id),
        )
        await db.commit()

    # ---- conversation-memory embeddings ----------------------------------

    async def record_message_embedding(
        self, assistant_message_id: int, embedding: list[float]
    ) -> None:
        """Store the embedding for an already-persisted turn.

        No-op if sqlite-vec isn't loaded (dev environments without the
        extension). On failure, logs and swallows — memory recall is a
        nice-to-have, not load-bearing.
        """
        if not self._has_vec:
            return
        try:
            await self._conn.execute(
                "INSERT INTO message_embeddings (assistant_message_id, embedding) VALUES (?, ?)",
                (assistant_message_id, _pack_embedding(embedding)),
            )
            await self._conn.commit()
        except Exception:
            logger.exception(
                "failed to store message embedding for assistant_msg_id=%s",
                assistant_message_id,
            )

    async def search_messages_semantic(
        self,
        chat_id: int,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[RecalledTurn]:
        """KNN over embedded turns, scoped to a single chat.

        Returns `RecalledTurn` ordered by ascending distance. Empty list if
        sqlite-vec isn't loaded or the chat has no embedded turns yet.
        """
        if not self._has_vec:
            return []
        db = self._conn
        packed = _pack_embedding(query_embedding)
        # Over-fetch from vec0 then filter by chat_id — vec0 doesn't natively
        # support WHERE predicates beyond MATCH + k, and our scale is tiny.
        overfetch_k = max(limit * 4, 20)
        async with db.execute(
            f"""
            SELECT t.assistant_message_id, t.chat_id, t.user_text, t.assistant_text,
                   t.created_at, me.distance
            FROM message_embeddings me
            JOIN message_turns t ON t.assistant_message_id = me.assistant_message_id
            WHERE me.embedding MATCH ? AND k = {int(overfetch_k)}
              AND t.chat_id = ?
            ORDER BY me.distance
            LIMIT ?
            """,
            (packed, chat_id, int(limit)),
        ) as cur:
            rows = await cur.fetchall()
        return [
            RecalledTurn(
                assistant_message_id=row[0],
                chat_id=row[1],
                user_text=row[2],
                assistant_text=row[3],
                created_at=row[4],
                distance=float(row[5]),
            )
            for row in rows
        ]

    async def cancel_nudge(self, nudge_id: int, chat_id: int) -> bool:
        """Cancel a pending nudge. Scoped to chat_id so one user can't cancel another's.
        Returns True if a row was cancelled, False if none matched or already fired.
        """
        db = self._conn
        async with db.execute(
            """
            UPDATE scheduled_nudges
            SET cancelled_at = ?
            WHERE id = ? AND chat_id = ? AND sent_at IS NULL AND cancelled_at IS NULL
            """,
            (_now_iso(), nudge_id, chat_id),
        ) as cur:
            changed = cur.rowcount or 0
        await db.commit()
        return changed > 0

    async def get_saved_file_by_relpath(self, relpath: str) -> SavedFile | None:
        db = self._conn
        async with db.execute(
            """
            SELECT id, chat_id, relpath, kind, file_name, mime_type,
                   file_size, caption, description, created_at
            FROM saved_files WHERE relpath = ?
            """,
            (relpath,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SavedFile(*row)
