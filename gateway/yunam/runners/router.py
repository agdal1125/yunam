"""Route a scored item into URGENT / DIGEST / DROP.

The router has no I/O of its own — it's a pure policy fn. Kept separate from
the curator so threshold-tuning is one diff, and so tests can drive it with
synthetic scores. URGENT/DIGEST/DROP labels match `curated_items.routed_as`.
"""

from __future__ import annotations

URGENT = "urgent"
DIGEST = "digest"
DROP = "drop"


def route(
    *,
    score: float,
    urgent_threshold: float,
    digest_threshold: float,
) -> str:
    """Pure routing decision. Scores >= urgent → push immediately; scores
    between digest and urgent → defer to the 21:00 newsletter; below digest
    → drop (still stored, useful for retro analysis of what we missed).
    """
    if score >= urgent_threshold:
        return URGENT
    if score >= digest_threshold:
        return DIGEST
    return DROP
