"""Score curated items by stock-market impact.

Asks Haiku to bucket each item into one of five impact tiers
(NONE/LOW/MEDIUM/HIGH/EXTREME) and tag the affected sector or theme. The
bucket is mapped to a numeric score that lines up with the router
thresholds. Only EXTREME is allowed to fire an urgent Telegram push:

    EXTREME → 0.95   (>= urgent_threshold ~0.90 → URGENT)
    HIGH    → 0.75   (digest)
    MEDIUM  → 0.55   (digest at default 0.55; drop at user 0.70 env)
    LOW     → 0.20   (drop)
    NONE    → 0.00   (drop)

The model judges *price impact*, not topical relevance. Routine earnings
within consensus are NONE; macro/policy shifts, sector-leader strategy
moves, supply chain disruptions, and key-person actions with strategic
signal lift toward HIGH. EXTREME is reserved for genuinely rare events
(unscheduled rate move, ceasefire/war declaration, surprise mega-cap M&A,
unprecedented sanctions). HIGH-and-below items go into the daily digest
rather than spamming as urgent push.

Scorer never raises through to the caller — Haiku failure returns
(score=0.0, matched_interest=None) so the router drops the item. The
embedding step is best-effort and independent of scoring; it powers
in-conversation `search_curated` only.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..embeddings import VoyageEmbedder
from ..usage import UsageRecorder

logger = logging.getLogger("yunam.runners.scorer")

SCORER_MODEL = "claude-haiku-4-5"
SCORER_MAX_TOKENS = 60

SCORER_SYSTEM = """You are a market-impact rater for an investor's news curator.

Your job: bucket each news item by how much it could move stock prices.
Korean equities take priority; global news matters only when it has read-
through to Korean sectors (memory, batteries, auto, biotech, defense,
shipbuilding, nuclear, solar).

Be conservative. Only EXTREME is pushed to the user immediately as an
urgent Telegram alert — everything else is batched into a single daily
digest. The cost of a missed EXTREME is small (it'll appear in the digest
hours later); the cost of a false-positive EXTREME is a noisy phone.

LEVELS — pick exactly one:

  EXTREME — Reserve for genuinely rare, catalyst-grade events. Roughly
    "at most a few per week, globally." Examples:
      • Unscheduled / surprise central bank action (off-cycle Fed move,
        BOK emergency cut, FOMC decision that diverges from consensus).
      • War declaration, ceasefire confirmation, regime change in a
        major economy, sovereign default, election outcome reversing
        macro policy.
      • Mega-cap (>$500B mcap, or KOSPI top-10) acquisition / spin-off /
        bankruptcy / sovereign sanction.
      • Unprecedented export controls on a strategic commodity (chips,
        rare earths, energy) at a level that hasn't been priced in.
      • CPI/PCE/jobs prints that ALREADY moved futures >1% pre-market.
    A scheduled FOMC meeting that lands AT consensus is NOT EXTREME.
    Geopolitical commentary or "tensions rising" pieces are NOT EXTREME.

  HIGH — Sector-mover but routine in the news flow. Will be summarized
    in tonight's digest, not pushed live.
    • Supply chain disruptions or resolutions, capacity changes, key
      supplier visits, export-control rumors not yet confirmed.
    • Major contracts, partnerships, or strategy pivots by sector
      leaders ("Nvidia moves beyond GPUs into optical networking").
    • Key-person moves with strategic signal — sector-leader CEO
      changes, factory visits, sovereign-wealth or activist positioning.
    • Big earnings surprises (>30% revenue swing vs. consensus) — note,
      the EARNINGS PRINT itself is HIGH, not EXTREME.
    • Standard Fed-speaker commentary, FOMC minutes, ECB press
      conferences — these are HIGH at best, NEVER EXTREME unless they
      contain an actual surprise policy signal.

  MEDIUM — Sector or theme relevance but not a single-day catalyst.
    Trend pieces, secondary supply chain news, mid-tier policy shifts,
    second-derivative reads on a leader's news, ordinary inflation /
    growth / trade-balance data.

  LOW — Mild relevance. Generic industry commentary, opinion columns,
    mid-cap individual news without sector read-through, market wraps
    that only restate the tape.

  NONE — Routine earnings within consensus, individual micro-news, single
    stock recommendations, off-topic (weather, sports, lifestyle), repeated
    coverage of yesterday's story, "X 의장 취임" / appointment items where
    the appointment is already known.

KEY RULES (apply in order):
  1. Default DOWN one tier. When in doubt between two tiers, pick the
     lower one. EXTREME especially: if you're not certain, it's HIGH.
  2. Ordinary earnings = NONE unless the surprise/miss is unmistakably
     catalyst-grade (then HIGH).
  3. Sector-leader news (삼성전자, SK하이닉스, 현대차, LG에너지솔루션,
     포스코, 한화에어로스페이스, Nvidia, TSMC, Apple, Tesla, Aramco) is
     one tier higher than the same story on a mid-cap — capped at HIGH.
  4. Routine macro coverage (Fed-speaker quotes, scheduled CPI/PCE,
     standard FOMC minutes, "기상도" / outlook pieces, retrospectives)
     is HIGH at most, NEVER EXTREME.
  5. If the headline is an analyst opinion ("could…", "might…",
     "전망", "예상", "기상도", "리뷰"), drop by one tier.

OUTPUT — exactly two lines, no preamble, no explanation:
LEVEL: <NONE|LOW|MEDIUM|HIGH|EXTREME>
CATEGORY: <2-10자 한글 라벨, 영향 받는 섹터/테마 (예: 반도체, 연준·금리, 지정학, AI 인프라, 배터리, 조선, 규제, 원자재)>"""

_LEVEL_SCORES = {
    "NONE": 0.0,
    "LOW": 0.20,
    "MEDIUM": 0.55,
    "HIGH": 0.75,
    "EXTREME": 0.95,
}

_LEVEL_RE = re.compile(r"LEVEL\s*:\s*(NONE|LOW|MEDIUM|HIGH|EXTREME)", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"CATEGORY\s*:\s*([^\n\r]+)")


@dataclass(frozen=True)
class ScoreResult:
    score: float
    matched_interest: str | None
    embedding: list[float] | None


class Scorer:
    def __init__(
        self,
        client: Any,
        embedder: VoyageEmbedder,
        *,
        usage_recorder: UsageRecorder | None = None,
        model: str = SCORER_MODEL,
        skill_id: str = "curation",
    ):
        self._client = client
        self._embedder = embedder
        self._usage = usage_recorder
        self._model = model
        self._skill_id = skill_id

    async def score(self, *, title: str, summary_or_excerpt: str | None) -> ScoreResult:
        title = (title or "").strip()
        body = (summary_or_excerpt or "").strip()
        if not title and not body:
            return ScoreResult(score=0.0, matched_interest=None, embedding=None)

        # Embedding is for `search_curated` only; never gates scoring.
        embedding = await self._safe_embed(title, body)

        text = title if not body else f"제목: {title}\n\n요약/본문:\n{body[:2000]}"
        rated = await self._rate_with_haiku(text, title_for_logs=title)
        if rated is None:
            return ScoreResult(score=0.0, matched_interest=None, embedding=embedding)
        level, category = rated
        score = _LEVEL_SCORES.get(level, 0.0)
        return ScoreResult(
            score=score, matched_interest=category, embedding=embedding
        )

    async def _safe_embed(self, title: str, body: str) -> list[float] | None:
        text = title if not body else f"{title}\n\n{body}"
        try:
            return await self._embedder.embed_text_document(text)
        except Exception:
            logger.warning(
                "scorer: embed failed for %r", title[:60], exc_info=True
            )
            return None

    async def _rate_with_haiku(
        self, user_text: str, *, title_for_logs: str
    ) -> tuple[str, str | None] | None:
        t0 = time.monotonic()
        status = "ok"
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=SCORER_MAX_TOKENS,
                system=SCORER_SYSTEM,
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception:
            status = "error"
            if self._usage is not None:
                self._usage.record_anthropic(
                    model=self._model,
                    usage=None,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    status=status,
                    skill_id=self._skill_id,
                )
            logger.warning(
                "scorer: rate call failed for %r", title_for_logs[:60], exc_info=True
            )
            return None
        usage = getattr(response, "usage", None)
        if self._usage is not None:
            self._usage.record_anthropic(
                model=self._model,
                usage=usage,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                status=status,
                skill_id=self._skill_id,
            )

        text = _extract_text(response)
        if not text:
            return None
        m_level = _LEVEL_RE.search(text)
        if not m_level:
            logger.warning(
                "scorer: no LEVEL line in response for %r: %r",
                title_for_logs[:60],
                text[:120],
            )
            return None
        level = m_level.group(1).upper()
        m_cat = _CATEGORY_RE.search(text)
        category = m_cat.group(1).strip() if m_cat else None
        if category:
            # Defensive trim — keep the label snug for headers and bullets.
            category = category.strip().strip("'\"`").strip()
            if len(category) > 20:
                category = category[:20]
            if not category:
                category = None
        return level, category


def _extract_text(response: Any) -> str:
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            t = getattr(block, "text", None)
            if t:
                parts.append(t)
    return "".join(parts).strip()
