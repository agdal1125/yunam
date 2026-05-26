"""CurationTools — read surface for the curated items stream.

Read paths (Scope.CURATION_READ):
  - list_recent_curated(period, routed_as) — what came in recently
  - search_curated(query) — semantic search across curated items

Background fetch / scoring / push is all run by `runners/curator.py`. Scoring
is LLM-driven (market-impact rater in runners/scorer.py); there is no editable
interest profile from the chat surface. The agent never invokes the fetcher
either — that would let it trigger real-time external scrapes (cost + prompt
injection surface).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..embeddings import VoyageEmbedder
from ..sessions import CuratedItem, SessionStore
from .vault import VaultError


_VALID_PERIODS = ("today", "yesterday", "week", "month", "7d", "30d")
_VALID_ROUTES = ("urgent", "digest", "drop", "any")


def _period_since_utc(period: str, tz_name: str) -> str:
    """Convert a period label to an ISO-UTC `since` timestamp."""
    if period not in _VALID_PERIODS:
        raise VaultError(
            f"period must be one of {_VALID_PERIODS}, got {period!r}"
        )
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        since_local = today_local
    elif period == "yesterday":
        since_local = today_local - timedelta(days=1)
    elif period == "week":
        weekday = today_local.weekday()
        since_local = today_local - timedelta(days=weekday)
    elif period == "month":
        since_local = today_local.replace(day=1)
    elif period == "7d":
        since_local = today_local - timedelta(days=6)
    else:  # "30d"
        since_local = today_local - timedelta(days=29)
    return since_local.astimezone(timezone.utc).isoformat()


def _format_items(items: list[CuratedItem], *, with_summary: bool = True) -> str:
    if not items:
        return "최근 curated 항목이 없어요."
    lines: list[str] = []
    for it in items:
        route = it.routed_as or "?"
        category = it.matched_interest or "—"
        score = f"{it.score:.2f}" if it.score is not None else "—"
        lines.append(
            f"• [{route}] {it.title}\n"
            f"  카테고리: {category}  score={score}  source={it.source}"
        )
        if with_summary and it.summary:
            short = it.summary if len(it.summary) <= 240 else it.summary[:237] + "…"
            lines.append(f"  {short}")
        lines.append(f"  {it.url}")
    return "\n".join(lines)


class CurationTools:
    def __init__(
        self,
        store: SessionStore,
        embedder: VoyageEmbedder | None,
        *,
        timezone_name: str = "Asia/Seoul",
    ):
        self._store = store
        self._embedder = embedder
        self._tz = timezone_name

    # ---- read ------------------------------------------------------------

    async def list_recent(
        self, *, period: str = "today", routed_as: str = "any", limit: int = 10
    ) -> str:
        if routed_as not in _VALID_ROUTES:
            raise VaultError(
                f"routed_as must be one of {_VALID_ROUTES}, got {routed_as!r}"
            )
        since_utc = _period_since_utc(period, self._tz)
        items = await self._store.list_recent_curated_items(
            since_iso_utc=since_utc,
            routed_as=None if routed_as == "any" else routed_as,
            limit=int(limit),
        )
        if not items:
            return f"period={period}, routed_as={routed_as} 결과 없음."
        return _format_items(items)

    async def search(self, query: str, *, limit: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            raise VaultError("query is required")
        if self._embedder is None:
            return "embedder가 설정되지 않아 의미 검색을 사용할 수 없어요."
        try:
            vec = await self._embedder.embed_query(query)
        except Exception as e:
            raise VaultError(f"임베딩 실패: {e}") from e
        hits = await self._store.search_curated_items_semantic(vec, limit=limit)
        if not hits:
            return f"'{query}' 관련된 curated 항목을 못 찾았어요."
        # Format with distance prefix
        items = [it for it, _ in hits]
        text = _format_items(items)
        return text

__all__ = ["CurationTools", "_VALID_PERIODS", "_VALID_ROUTES"]
