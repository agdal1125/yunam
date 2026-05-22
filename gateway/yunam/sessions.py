"""SQLite session store for Yunam.

Persists conversation history (plain text only — no Claude content blocks), a
brief log of tool calls per turn, pending/saved file-attachment metadata, and a
co-located `sqlite-vec` virtual table of multimodal embeddings for semantic
file search. One SQLite database file, one connection, WAL mode.
"""

from __future__ import annotations

import asyncio
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
    created_at    TEXT NOT NULL,
    -- Multi-principal additions (v6). For role='user', user_id is the speaker's
    -- Telegram user id; for role='assistant' it's NULL (no human author).
    -- visibility is 'shared' (every principal in the chat may load/recall it)
    -- or 'private:<user_id>' (only that principal may load/recall it). The
    -- DB-side filter in load_history / search_messages_semantic is the primary
    -- ACL; the prompt-side instruction is defense in depth.
    user_id       INTEGER,
    visibility    TEXT NOT NULL DEFAULT 'shared'
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id, created_at);
-- idx_messages_visibility is created inside the v6 migration step, AFTER the
-- visibility column is added on upgrade DBs. Leaving it here would cause
-- `executescript` to fail on a pre-v6 DB before the ALTER TABLE has run.

CREATE TABLE IF NOT EXISTS tool_calls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             INTEGER NOT NULL REFERENCES sessions(chat_id),
    turn_message_id     INTEGER REFERENCES messages(id),
    name                TEXT NOT NULL,
    input_json          TEXT NOT NULL,
    result_preview      TEXT,
    is_error            INTEGER NOT NULL DEFAULT 0,
    elapsed_ms          INTEGER,
    created_at          TEXT NOT NULL,
    -- v6: which principal triggered this dispatch. NULL on legacy rows.
    principal_user_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_chat ON tool_calls(chat_id, created_at);

-- Attachments received via Telegram but not yet committed to the filevault.
-- We don't download until /save — `file_id` stays valid on Telegram's side.
CREATE TABLE IF NOT EXISTS pending_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    file_id         TEXT NOT NULL,
    file_unique_id  TEXT,
    media_group_id  TEXT,
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
    created_at           TEXT NOT NULL,
    -- Mirror messages.user_id / visibility so recall can ACL-filter without
    -- re-joining messages. user_id = the speaker of user_message_id.
    user_id              INTEGER,
    visibility           TEXT NOT NULL DEFAULT 'shared'
);
CREATE INDEX IF NOT EXISTS idx_message_turns_chat_created
    ON message_turns(chat_id, created_at DESC);
-- idx_message_turns_visibility is created inside the v6 migration step,
-- AFTER the visibility column is added on upgrade DBs.

-- Per-external-call audit. One row per paid API request (Anthropic message,
-- Voyage embed, Jina/Sweet Tracker/Open-Meteo HTTP call, MCP tool invocation).
-- Costs are stored as integer µUSD so summing 100k rows in SQLite stays exact.
-- Token columns are nullable: a Sweet Tracker request has no tokens, just a
-- units count. `chat_id` is nullable so background runners (curation worker,
-- nightly reflector) can still record their usage.
CREATE TABLE IF NOT EXISTS api_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL,
    model_or_endpoint   TEXT NOT NULL,
    chat_id             INTEGER,
    skill_id            TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_create_tokens INTEGER,
    units               INTEGER,
    cost_usd_micro      INTEGER NOT NULL DEFAULT 0,
    elapsed_ms          INTEGER,
    status              TEXT NOT NULL DEFAULT 'ok',
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_usage_created
    ON api_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider_created
    ON api_usage(provider, created_at);
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
DB_USER_VERSION = 7


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

    # v5: pending attachments remember Telegram album/media-group identity.
    if version < 5:
        if not await _column_exists(db, "pending_attachments", "media_group_id"):
            await db.execute("ALTER TABLE pending_attachments ADD COLUMN media_group_id TEXT")

    # v6: multi-principal — messages and message_turns gain `user_id` (speaker)
    # and `visibility` (shared / private:<user_id>) for the per-speaker ACL.
    # tool_calls gains principal_user_id for audit. Backfill leaves user_id
    # NULL on legacy rows; load_history treats NULL user_id as 'shared' so the
    # historical solo-user data stays visible to every principal.
    if version < 6:
        if not await _column_exists(db, "messages", "user_id"):
            await db.execute("ALTER TABLE messages ADD COLUMN user_id INTEGER")
        if not await _column_exists(db, "messages", "visibility"):
            await db.execute(
                "ALTER TABLE messages ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'"
            )
        if not await _column_exists(db, "message_turns", "user_id"):
            await db.execute("ALTER TABLE message_turns ADD COLUMN user_id INTEGER")
        if not await _column_exists(db, "message_turns", "visibility"):
            await db.execute(
                "ALTER TABLE message_turns ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'"
            )
        if not await _column_exists(db, "tool_calls", "principal_user_id"):
            await db.execute("ALTER TABLE tool_calls ADD COLUMN principal_user_id INTEGER")
        # Indexes are CREATE IF NOT EXISTS so re-running is safe.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_visibility ON messages(chat_id, visibility)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_turns_visibility "
            "ON message_turns(chat_id, visibility)"
        )

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
    # v6: which principal triggered this call. NULL only when persist runs
    # outside a turn context (no current speaker, e.g. system-driven proactive).
    principal_user_id: int | None = None


@dataclass(frozen=True)
class PendingAttachment:
    id: int
    chat_id: int
    file_id: str
    file_unique_id: str | None
    media_group_id: str | None
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
    # v6: speaker of the user side of this turn. None for legacy rows. The
    # memory tool uses this to render a `[from: <name>]` marker in recall
    # output so a multi-principal recall is unambiguous.
    user_id: int | None = None


class SessionStore:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._has_vec = False
        # aiosqlite shares one Python sqlite3 connection across all coroutines.
        # Python's sqlite3 driver auto-opens an implicit transaction on the
        # first DML statement and only closes it on commit. When two coroutines
        # interleave their write paths — e.g. a background `record_api_usage`
        # INSERT yields control mid-transaction, then `persist_turn` calls
        # explicit `BEGIN` — sqlite raises "cannot start a transaction within
        # a transaction." Every method that issues a write (auto- or explicit-
        # transaction) acquires this lock to serialize transactions cleanly.
        self._write_lock: asyncio.Lock = asyncio.Lock()

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

    async def load_history(
        self,
        chat_id: int,
        *,
        viewer_user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return last HISTORY_LIMIT messages visible to `viewer_user_id`.

        Visibility ACL: a row is visible iff `visibility = 'shared'` or
        `visibility = 'private:<viewer_user_id>'`. NULL `viewer_user_id`
        bypasses the ACL — used by maintenance scripts; never by the live
        orchestrator. The shape returned is a list of `{role, content, user_id}`
        in chronological order. The current turn's user message is NOT included
        — the orchestrator appends it.

        `user_id` is included so the orchestrator can prefix multi-principal
        history with `[from: <name>]` markers when rendering for Claude.
        """
        db = self._conn
        if viewer_user_id is None:
            async with db.execute(
                """
                SELECT role, content, user_id FROM (
                    SELECT role, content, user_id, created_at, id
                    FROM messages
                    WHERE chat_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                ) ORDER BY created_at ASC, id ASC
                """,
                (chat_id, HISTORY_LIMIT),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            private_marker = f"private:{int(viewer_user_id)}"
            async with db.execute(
                """
                SELECT role, content, user_id FROM (
                    SELECT role, content, user_id, created_at, id
                    FROM messages
                    WHERE chat_id = ?
                      AND (visibility = 'shared' OR visibility = ?)
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                ) ORDER BY created_at ASC, id ASC
                """,
                (chat_id, private_marker, HISTORY_LIMIT),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {"role": role, "content": content, "user_id": user_id}
            for role, content, user_id in rows
        ]

    async def record_proactive_message(
        self,
        chat_id: int,
        text: str,
        *,
        target_user_id: int | None = None,
    ) -> None:
        """Record an assistant-initiated message (e.g. nightly retrospective prompt).

        Writes the session row (upsert) and an `assistant` message, so the next
        user reply's `load_history` sees Yunam's prompt as prior context. If
        `target_user_id` is set, the message is private to that principal — used
        for nudges/retrospective prompts directed at a specific person in a
        shared chat. Default `None` → 'shared' so legacy callers stay correct.
        """
        db = self._conn
        now = _now_iso()
        visibility = (
            f"private:{int(target_user_id)}" if target_user_id is not None else "shared"
        )
        async with self._write_lock:
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
                    """
                    INSERT INTO messages (chat_id, role, content, created_at, visibility)
                    VALUES (?, 'assistant', ?, ?, ?)
                    """,
                    (chat_id, text, now, visibility),
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
        *,
        principal_user_id: int | None = None,
        visibility: str = "shared",
    ) -> tuple[int, int]:
        """Write one full turn (user + assistant + tool calls + turn index) atomically.

        Returns `(user_message_id, assistant_message_id)` so the caller can
        schedule a background embedding task keyed to the assistant message.
        Does not embed — that's the orchestrator's job.

        `principal_user_id` identifies the speaker; both user and assistant
        rows get this value for the user side and NULL for the assistant
        side respectively (assistant has no human author). `visibility`
        applies to both rows in the turn — yunam's response inherits the
        visibility of the user message that triggered it, so a recall by
        another principal doesn't leak the assistant half of a private turn.
        """
        db = self._conn
        now = _now_iso()
        async with self._write_lock:
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
                    """
                    INSERT INTO messages (chat_id, role, content, created_at, user_id, visibility)
                    VALUES (?, 'user', ?, ?, ?, ?)
                    """,
                    (chat_id, user_text, now, principal_user_id, visibility),
                ) as cur:
                    user_msg_id = cur.lastrowid
                async with db.execute(
                    """
                    INSERT INTO messages (chat_id, role, content, created_at, user_id, visibility)
                    VALUES (?, 'assistant', ?, ?, NULL, ?)
                    """,
                    (chat_id, assistant_text, now, visibility),
                ) as cur:
                    assistant_msg_id = cur.lastrowid
                await db.execute(
                    """
                    INSERT INTO message_turns
                        (assistant_message_id, chat_id, user_message_id,
                         user_text, assistant_text, created_at,
                         user_id, visibility)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (assistant_msg_id, chat_id, user_msg_id,
                     user_text, assistant_text, now,
                     principal_user_id, visibility),
                )

                if tool_calls:
                    await db.executemany(
                        """
                        INSERT INTO tool_calls
                            (chat_id, turn_message_id, name, input_json,
                             result_preview, is_error, elapsed_ms, created_at,
                             skill_id, scope, principal_user_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                (
                                    tc.principal_user_id
                                    if tc.principal_user_id is not None
                                    else principal_user_id
                                ),
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
        media_group_id: str | None,
        kind: str,
        file_name: str | None,
        mime_type: str | None,
        file_size: int | None,
        caption: str | None,
    ) -> int:
        db = self._conn
        now = _now_iso()
        async with self._write_lock:
            async with db.execute(
                """
                INSERT INTO pending_attachments
                    (chat_id, file_id, file_unique_id, media_group_id, kind, file_name,
                     mime_type, file_size, caption, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    file_id,
                    file_unique_id,
                    media_group_id,
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
            SELECT id, chat_id, file_id, file_unique_id, media_group_id, kind, file_name,
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

    async def list_pending_attachments(
        self,
        chat_id: int,
        *,
        pending_ids: list[int] | None = None,
        media_group_id: str | None = None,
        limit: int | None = None,
    ) -> list[PendingAttachment]:
        """Return pending attachments for a chat in arrival order.

        Optional filters keep album/caption-driven tool calls scoped to the
        attachments from the triggering Telegram update rather than old pending
        files in the same chat.
        """
        db = self._conn
        clauses = ["chat_id = ?"]
        params: list[Any] = [chat_id]
        if pending_ids:
            placeholders = ", ".join("?" for _ in pending_ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(int(pid) for pid in pending_ids)
        if media_group_id:
            clauses.append("media_group_id = ?")
            params.append(media_group_id)
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(1, int(limit)))
        async with db.execute(
            f"""
            SELECT id, chat_id, file_id, file_unique_id, media_group_id, kind, file_name,
                   mime_type, file_size, caption
            FROM pending_attachments
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at ASC, id ASC
            {limit_sql}
            """,
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
        return [PendingAttachment(*row) for row in rows]

    async def delete_pending_attachment(self, pending_id: int) -> None:
        db = self._conn
        async with self._write_lock:
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
        async with self._write_lock:
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
        async with self._write_lock:
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
        async with self._write_lock:
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
            async with self._write_lock:
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
        *,
        viewer_user_id: int | None = None,
    ) -> list[RecalledTurn]:
        """KNN over embedded turns, scoped to a single chat and visibility ACL.

        Returns `RecalledTurn` ordered by ascending distance. Empty list if
        sqlite-vec isn't loaded or the chat has no embedded turns yet.

        Visibility ACL: a turn is recallable iff `visibility = 'shared'` or
        `visibility = 'private:<viewer_user_id>'`. NULL viewer_user_id
        bypasses the ACL — used for tests/maintenance only. The over-fetch
        widens further when filtering so private rows pruned post-KNN don't
        starve the result set.
        """
        if not self._has_vec:
            return []
        db = self._conn
        packed = _pack_embedding(query_embedding)
        # Over-fetch from vec0 then filter by chat_id + visibility — vec0
        # doesn't natively support WHERE predicates beyond MATCH + k, and our
        # scale is tiny. Bump the multiplier when we're filtering so visibility
        # pruning doesn't shrink the result below `limit`.
        overfetch_k = max(limit * (8 if viewer_user_id is not None else 4), 32)
        if viewer_user_id is None:
            sql = f"""
                SELECT t.assistant_message_id, t.chat_id, t.user_text, t.assistant_text,
                       t.created_at, me.distance, t.user_id
                FROM message_embeddings me
                JOIN message_turns t ON t.assistant_message_id = me.assistant_message_id
                WHERE me.embedding MATCH ? AND k = {int(overfetch_k)}
                  AND t.chat_id = ?
                ORDER BY me.distance
                LIMIT ?
            """
            params: tuple[Any, ...] = (packed, chat_id, int(limit))
        else:
            private_marker = f"private:{int(viewer_user_id)}"
            sql = f"""
                SELECT t.assistant_message_id, t.chat_id, t.user_text, t.assistant_text,
                       t.created_at, me.distance, t.user_id
                FROM message_embeddings me
                JOIN message_turns t ON t.assistant_message_id = me.assistant_message_id
                WHERE me.embedding MATCH ? AND k = {int(overfetch_k)}
                  AND t.chat_id = ?
                  AND (t.visibility = 'shared' OR t.visibility = ?)
                ORDER BY me.distance
                LIMIT ?
            """
            params = (packed, chat_id, private_marker, int(limit))
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            RecalledTurn(
                assistant_message_id=row[0],
                chat_id=row[1],
                user_text=row[2],
                assistant_text=row[3],
                created_at=row[4],
                distance=float(row[5]),
                user_id=row[6],
            )
            for row in rows
        ]

    async def cancel_nudge(self, nudge_id: int, chat_id: int) -> bool:
        """Cancel a pending nudge. Scoped to chat_id so one user can't cancel another's.
        Returns True if a row was cancelled, False if none matched or already fired.
        """
        db = self._conn
        async with self._write_lock:
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

    # ---- api_usage (Phase 2.0 audit) -------------------------------------

    async def record_api_usage(
        self,
        *,
        provider: str,
        model_or_endpoint: str,
        chat_id: int | None,
        skill_id: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_create_tokens: int | None,
        units: int | None,
        cost_usd_micro: int,
        elapsed_ms: int | None,
        status: str = "ok",
    ) -> None:
        """Append one row to `api_usage`. Called by UsageRecorder; never raises
        through to the caller — the recorder wraps this in a background task
        and logs failures separately.
        """
        db = self._conn
        now = _now_iso()
        async with self._write_lock:
            await db.execute(
                """
                INSERT INTO api_usage
                    (provider, model_or_endpoint, chat_id, skill_id,
                     input_tokens, output_tokens, cache_read_tokens, cache_create_tokens,
                     units, cost_usd_micro, elapsed_ms, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    model_or_endpoint,
                    chat_id,
                    skill_id,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_create_tokens,
                    units,
                    int(cost_usd_micro),
                    elapsed_ms,
                    status,
                    now,
                ),
            )
            await db.commit()

    async def usage_totals_between(
        self, since_iso_utc: str, until_iso_utc: str
    ) -> dict[str, Any]:
        """Aggregate api_usage rows in `[since, until)` into a single summary.

        Returns totals across all providers — `usage_breakdown_between` does
        the grouped form.
        """
        db = self._conn
        async with db.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0)        AS in_tok,
                COALESCE(SUM(output_tokens), 0)       AS out_tok,
                COALESCE(SUM(cache_read_tokens), 0)   AS cache_r,
                COALESCE(SUM(cache_create_tokens), 0) AS cache_c,
                COALESCE(SUM(units), 0)               AS units,
                COALESCE(SUM(cost_usd_micro), 0)      AS cost_micro,
                COUNT(*)                              AS calls,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
            FROM api_usage
            WHERE created_at >= ? AND created_at < ?
            """,
            (since_iso_utc, until_iso_utc),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
                "units": 0,
                "cost_usd_micro": 0,
                "calls": 0,
                "errors": 0,
            }
        return {
            "input_tokens": int(row[0]),
            "output_tokens": int(row[1]),
            "cache_read_tokens": int(row[2]),
            "cache_create_tokens": int(row[3]),
            "units": int(row[4]),
            "cost_usd_micro": int(row[5]),
            "calls": int(row[6]),
            "errors": int(row[7] or 0),
        }

    async def usage_breakdown_between(
        self,
        since_iso_utc: str,
        until_iso_utc: str,
        *,
        group_by: str = "provider",
    ) -> list[dict[str, Any]]:
        """Aggregate api_usage by one of provider / model_or_endpoint / skill_id.

        Returns a list of dict rows ordered by cost_usd_micro descending. The
        usage skill renders these into a human-readable table.
        """
        valid_columns = {"provider", "model_or_endpoint", "skill_id"}
        if group_by not in valid_columns:
            raise ValueError(
                f"group_by must be one of {sorted(valid_columns)}, got {group_by!r}"
            )
        db = self._conn
        # group_by has been validated against an allowlist above — safe to
        # interpolate into the SQL.
        sql = f"""
            SELECT
                {group_by}                              AS bucket,
                COALESCE(SUM(input_tokens), 0)         AS in_tok,
                COALESCE(SUM(output_tokens), 0)        AS out_tok,
                COALESCE(SUM(cache_read_tokens), 0)    AS cache_r,
                COALESCE(SUM(cache_create_tokens), 0)  AS cache_c,
                COALESCE(SUM(units), 0)                AS units,
                COALESCE(SUM(cost_usd_micro), 0)       AS cost_micro,
                COUNT(*)                               AS calls,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
            FROM api_usage
            WHERE created_at >= ? AND created_at < ?
            GROUP BY {group_by}
            ORDER BY cost_micro DESC, calls DESC
        """
        async with db.execute(sql, (since_iso_utc, until_iso_utc)) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "bucket": row[0] if row[0] is not None else "(none)",
                    "input_tokens": int(row[1]),
                    "output_tokens": int(row[2]),
                    "cache_read_tokens": int(row[3]),
                    "cache_create_tokens": int(row[4]),
                    "units": int(row[5]),
                    "cost_usd_micro": int(row[6]),
                    "calls": int(row[7]),
                    "errors": int(row[8] or 0),
                }
            )
        return out

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
