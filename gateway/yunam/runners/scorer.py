"""Score curated items by stock-market impact.

Asks Haiku to bucket each item into one of five impact tiers
(NONE/LOW/MEDIUM/HIGH/EXTREME) and tag the affected sector or theme. The
bucket is mapped to a numeric score that lines up with the existing router
thresholds:

    EXTREME → 0.95   (urgent at 0.82)
    HIGH    → 0.85   (urgent at 0.82)
    MEDIUM  → 0.60   (digest at 0.55)
    LOW     → 0.30   (drop)
    NONE    → 0.00   (drop)

The model judges *price impact*, not topical relevance. Routine earnings
within consensus are NONE; macro/policy shifts, sector-leader strategy
moves, supply chain disruptions, and key-person actions with strategic
signal lift toward HIGH/EXTREME.

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
Korean equities take priority; global news matters when it has read-through
to Korean sectors (memory, batteries, auto, biotech, defense, shipbuilding,
nuclear, solar).

LEVELS — pick exactly one:

  EXTREME — Could shift major indices >2% or reset sector multiples.
    Macro / policy shocks (FOMC surprise, war or ceasefire, China stimulus,
    sovereign sanctions), regulatory bombshells, mega-cap M&A, sovereign
    supply-chain shocks (export bans on chips, rare earths, energy).

  HIGH — Could move a sector leader 3-5% or its sector 1-2%.
    • Supply chain disruptions or resolutions — strikes ending, factory
      fires, capacity additions, export controls, key supplier visits
      ("Musk's Tesla/SpaceX staff visited Chinese solar suppliers").
    • Major contracts, partnerships, or strategy pivots by sector leaders
      ("Nvidia moves beyond GPUs into optical networking").
    • Key-person moves with strategic signal — sector-leader CEO changes,
      Tim Cook / Jensen Huang / Musk factory visits, sovereign-wealth or
      activist-investor positioning.
    • Earnings ONLY if the surprise/miss is so large the print itself is
      the catalyst (e.g. >30% revenue swing vs. consensus).

  MEDIUM — Genuine sector or theme impact but not a single-day catalyst.
    Trend pieces backed by data, secondary supply chain news, mid-tier
    policy shifts, second-derivative reads on a leader's news.

  LOW — Mild relevance. Generic industry commentary, opinion columns,
    mid-cap individual news without sector read-through, market wraps that
    only restate the tape.

  NONE — Routine earnings within consensus, individual stock micro-news
    with no sector signal, single-stock recommendations, off-topic
    (weather, sports, lifestyle).

KEY RULES (apply in order):
  1. Ordinary earnings = NONE unless surprise/miss is unmistakably
     catalyst-grade (then HIGH).
  2. Sector-leader news (삼성전자, SK하이닉스, 현대차, LG에너지솔루션,
     포스코, 한화에어로스페이스, Nvidia, TSMC, Apple, Tesla, Aramco) gets
     one tier higher than the same story on a mid-cap.
  3. Macro events (Fed minutes, war/ceasefire, election outcomes, CPI/PCE
     prints, major commodity moves) default to HIGH or EXTREME.
  4. Supply-chain or key-person stories lift to HIGH whenever they signal
     a capacity, sourcing, or strategy shift.
  5. When in doubt between two adjacent tiers, pick the lower one.

OUTPUT — exactly two lines, no preamble, no explanation:
LEVEL: <NONE|LOW|MEDIUM|HIGH|EXTREME>
CATEGORY: <2-10자 한글 라벨, 영향 받는 섹터/테마 (예: 반도체, 연준·금리, 지정학, AI 인프라, 배터리, 조선, 규제, 원자재)>"""

_LEVEL_SCORES = {
    "NONE": 0.0,
    "LOW": 0.30,
    "MEDIUM": 0.60,
    "HIGH": 0.85,
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
