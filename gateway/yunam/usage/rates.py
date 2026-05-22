"""Pricing tables for external services Yunam pays for.

Single source of truth — every wrapper that converts call-volume to a USD
estimate goes through `*_cost_micro()` so a price change is one edit.

Costs are stored as integer µUSD (1 µUSD = 1e-6 USD) in `api_usage.cost_usd_micro`
to avoid decimal-rounding drift across millions of rows. A 1-million-token Sonnet
input call costs ~3,000,000 µUSD; a single Voyage embedding ~10–100 µUSD.

Numbers below reflect public pricing as of 2026-05. Update them here when
Anthropic/Voyage post new pricing — the docstring is the diff target.
"""

from __future__ import annotations


# --- Anthropic ---
# Per-1M tokens (USD). Cache-write is the inflated "write" rate Anthropic
# charges for the first call that primes a cache prefix; cache-read is the
# discounted rate for subsequent hits. Keep new model entries explicit.
ANTHROPIC_RATES_USD_PER_M: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-opus-4-7": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.1,
        "cache_write": 1.25,
    },
}


# --- Voyage ---
# voyage-multimodal-3 is billed per token (text) and per image. We don't have
# a precise per-image figure in our public docs cache; a conservative estimate
# is used so cost trends are at least monotone-correct.
VOYAGE_RATES_USD: dict[str, dict[str, float]] = {
    "voyage-multimodal-3": {
        "per_1m_tokens": 0.12,    # text tokens
        "per_image": 0.0002,      # ~$0.0002 per image (conservative)
    },
}


# --- Per-request external services ---
# These have either negligible per-request cost (free tier) or are billed by
# request counts not by tokens. We track request counts so we can detect when
# a free tier is about to be exceeded.
REST_RATES_USD_PER_REQUEST: dict[str, float] = {
    "jina:reader": 0.0,            # keyless tier; key tier free up to quota
    "jina:search": 0.0,
    "duckduckgo:html": 0.0,
    "sweettracker:trackingInfo": 0.0,
    "open-meteo:geocoding": 0.0,
    "open-meteo:airquality": 0.0,
}


def _to_micro_usd(usd: float) -> int:
    """Round a USD float to integer µUSD."""
    # Use round-half-to-even (banker's) implicitly via Python int()/round();
    # a $3.0 / 1e6 token * 1k tokens = $0.003 = 3000 µUSD — no precision issue.
    return int(round(usd * 1_000_000))


def anthropic_cost_micro(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
) -> int:
    """Estimate µUSD for one Anthropic `messages.create` call.

    Unknown model → 0. We'd rather under-count an experimental model than
    have a wrong-model magic number leak into reports.
    """
    rates = ANTHROPIC_RATES_USD_PER_M.get(model)
    if rates is None:
        return 0
    usd = (
        (input_tokens / 1_000_000) * rates["input"]
        + (output_tokens / 1_000_000) * rates["output"]
        + (cache_read_tokens / 1_000_000) * rates["cache_read"]
        + (cache_create_tokens / 1_000_000) * rates["cache_write"]
    )
    return _to_micro_usd(usd)


def voyage_cost_micro(
    model: str,
    *,
    text_tokens: int = 0,
    images: int = 0,
) -> int:
    """Estimate µUSD for one Voyage embed call.

    For multimodal calls, both `text_tokens` and `images` may be > 0. Unknown
    model → 0 (same posture as anthropic_cost_micro).
    """
    rates = VOYAGE_RATES_USD.get(model)
    if rates is None:
        return 0
    usd = (text_tokens / 1_000_000) * rates["per_1m_tokens"] + images * rates["per_image"]
    return _to_micro_usd(usd)


def rest_cost_micro(provider_endpoint: str, units: int = 1) -> int:
    """Estimate µUSD for `units` calls to a per-request external endpoint."""
    rate = REST_RATES_USD_PER_REQUEST.get(provider_endpoint, 0.0)
    return _to_micro_usd(rate * units)


__all__ = [
    "ANTHROPIC_RATES_USD_PER_M",
    "VOYAGE_RATES_USD",
    "REST_RATES_USD_PER_REQUEST",
    "anthropic_cost_micro",
    "voyage_cost_micro",
    "rest_cost_micro",
]
