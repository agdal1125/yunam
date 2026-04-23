"""MemoryTools — semantic recall over prior conversation turns.

Loaded history (the last ~20 messages) gives the model short-term context.
For anything older — "what did we decide about X last month?" — the model
needs to explicitly search. This module exposes a `recall` tool that embeds
the query with Voyage and runs KNN against the `message_embeddings` vec0
table, returning the closest prior turns as formatted text.

Embedding at write-time happens in the orchestrator's `_persist_node` as a
background task. This module only does the read-side query.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..embeddings import VoyageEmbedder
from ..sessions import SessionStore
from ..tools.vault import VaultError

logger = logging.getLogger("yunam.tools.memory")

# Cap on how much of each matched turn we return. A matched turn can be
# long (tool-heavy assistant replies especially), and stuffing multiple
# huge turns into the model's context via a single recall would blow the
# prompt budget. Each side is truncated independently.
MAX_RECALL_BYTES_PER_SIDE = 600

MIN_LIMIT = 1
MAX_LIMIT = 10
DEFAULT_LIMIT = 5

# Distance above this = probably irrelevant. voyage-multimodal-3 returns
# cosine-ish distances; empirically matches below ~0.55 are strong, 0.55-0.75
# are okay, and above 0.85 are noise. We filter conservatively so recall
# doesn't feed the model low-signal results.
MAX_DISTANCE = 0.85


class MemoryTools:
    def __init__(self, store: SessionStore, embedder: VoyageEmbedder, timezone_name: str):
        self._store = store
        self._embedder = embedder
        self._tz = ZoneInfo(timezone_name)

    async def recall(self, chat_id: int, query: str, limit: int = DEFAULT_LIMIT) -> str:
        if not isinstance(query, str) or not query.strip():
            raise VaultError("query must be a non-empty string")
        if not isinstance(limit, int):
            raise VaultError("limit must be an integer")
        limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))

        query_vec = await self._embedder.embed_query(query.strip())
        turns = await self._store.search_messages_semantic(
            chat_id=chat_id,
            query_embedding=query_vec,
            limit=limit,
        )
        # Drop matches that are too far to be meaningful.
        turns = [t for t in turns if t.distance <= MAX_DISTANCE]
        if not turns:
            return "no prior conversation turns matched (or memory is still empty)."

        blocks: list[str] = []
        for t in turns:
            when = self._format_local(t.created_at)
            user = _truncate(t.user_text, MAX_RECALL_BYTES_PER_SIDE)
            asst = _truncate(t.assistant_text, MAX_RECALL_BYTES_PER_SIDE)
            blocks.append(
                f"[{when}, distance {t.distance:.2f}]\n"
                f"jaekeun: {user}\n"
                f"yunam: {asst}"
            )
        return "\n\n---\n\n".join(blocks)

    def _format_local(self, iso_utc: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_utc)
        except ValueError:
            return iso_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, limit_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    # Truncate on byte boundary but don't split a utf-8 codepoint.
    cut = encoded[:limit_bytes]
    while cut and (cut[-1] & 0b1100_0000) == 0b1000_0000:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore") + "…"
