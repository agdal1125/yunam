"""Usage reporting primitives — queries over the `api_usage` audit table.

Pure data layer: ranges are resolved to UTC ISO timestamps, totals/breakdowns
come from the `SessionStore` repository methods, then formatted into compact
plain-text the model can stuff into a tool_result. Cost-alert thresholds are
read from config; the tool returns the comparison verdict, not a UI nag.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..sessions import SessionStore


# Period vocabulary the model passes in. Each maps to a (since_local_dt,
# until_local_dt) pair anchored on the user's local TZ — we convert to UTC at
# the query boundary so the DB only ever sees ISO-UTC. "month" = current
# calendar month start.
_VALID_PERIODS = ("today", "yesterday", "week", "month", "7d", "30d")
_VALID_GROUPS = ("provider", "model_or_endpoint", "skill_id")


def _period_window_utc(
    period: str, tz_name: str = "Asia/Seoul"
) -> tuple[str, str]:
    """Return `(since_iso_utc, until_iso_utc)` for the requested period label."""
    if period not in _VALID_PERIODS:
        raise ValueError(
            f"period must be one of {_VALID_PERIODS}, got {period!r}"
        )
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        since_local = today_local
        until_local = today_local + timedelta(days=1)
    elif period == "yesterday":
        since_local = today_local - timedelta(days=1)
        until_local = today_local
    elif period == "week":
        # Calendar week, Monday-anchored — matches Korean weekly reporting conv.
        weekday = today_local.weekday()
        since_local = today_local - timedelta(days=weekday)
        until_local = since_local + timedelta(days=7)
    elif period == "month":
        since_local = today_local.replace(day=1)
        # Next month start = (today.replace(day=28) + 4d).replace(day=1)
        next_month_anchor = since_local.replace(day=28) + timedelta(days=4)
        until_local = next_month_anchor.replace(day=1)
    elif period == "7d":
        since_local = today_local - timedelta(days=6)
        until_local = today_local + timedelta(days=1)
    elif period == "30d":
        since_local = today_local - timedelta(days=29)
        until_local = today_local + timedelta(days=1)
    else:  # pragma: no cover — guarded above
        raise ValueError(period)

    since_utc = since_local.astimezone(timezone.utc).isoformat()
    until_utc = until_local.astimezone(timezone.utc).isoformat()
    return since_utc, until_utc


def _format_usd(cost_micro: int) -> str:
    """`$1.2345` style — 4 decimals to keep small cohorts visible."""
    return f"${cost_micro / 1_000_000:.4f}"


def _format_int(n: int) -> str:
    return f"{n:,}"


class UsageTools:
    """Async queries against api_usage. One instance per process."""

    def __init__(
        self,
        store: SessionStore,
        *,
        timezone_name: str = "Asia/Seoul",
        daily_alert_usd: float = 5.0,
        monthly_alert_usd: float = 100.0,
    ):
        self._store = store
        self._tz = timezone_name
        self._daily_usd = daily_alert_usd
        self._monthly_usd = monthly_alert_usd

    async def summary(self, period: str = "today") -> str:
        since_utc, until_utc = _period_window_utc(period, self._tz)
        row = await self._store.usage_totals_between(since_utc, until_utc)
        cache_total = row["cache_read_tokens"] + row["cache_create_tokens"]
        input_total = row["input_tokens"] + cache_total
        if input_total > 0:
            hit_rate = row["cache_read_tokens"] / input_total
        else:
            hit_rate = 0.0
        lines = [
            f"API usage — {period} ({self._tz})",
            f"  calls: {_format_int(row['calls'])}"
            + (f"  (errors: {row['errors']})" if row["errors"] else ""),
            f"  input tokens:  {_format_int(row['input_tokens'])}",
            f"  output tokens: {_format_int(row['output_tokens'])}",
            f"  cache read:    {_format_int(row['cache_read_tokens'])}",
            f"  cache create:  {_format_int(row['cache_create_tokens'])}",
            f"  cache hit:     {hit_rate * 100:.1f}%",
            f"  units (REST):  {_format_int(row['units'])}",
            f"  est. cost:     {_format_usd(row['cost_usd_micro'])}",
        ]
        return "\n".join(lines)

    async def breakdown(
        self, period: str = "today", group_by: str = "provider"
    ) -> str:
        if group_by not in _VALID_GROUPS:
            raise ValueError(
                f"group_by must be one of {_VALID_GROUPS}, got {group_by!r}"
            )
        since_utc, until_utc = _period_window_utc(period, self._tz)
        rows = await self._store.usage_breakdown_between(
            since_utc, until_utc, group_by=group_by
        )
        if not rows:
            return f"No api_usage rows for period={period} group_by={group_by}."
        lines = [f"API usage — {period} grouped by {group_by} ({self._tz})"]
        for r in rows:
            errs = f"  errors={r['errors']}" if r["errors"] else ""
            lines.append(
                f"  {r['bucket']:<30}  "
                f"calls={_format_int(r['calls']):>6}  "
                f"in={_format_int(r['input_tokens']):>8}  "
                f"out={_format_int(r['output_tokens']):>8}  "
                f"cost={_format_usd(r['cost_usd_micro'])}{errs}"
            )
        return "\n".join(lines)

    async def cost_alert_status(self) -> str:
        """Compare today / this-month spend against ENV thresholds.

        Returns a one- or two-line summary suitable for both a retrospective
        message and an ad-hoc query. Includes a 🚨 if either threshold is
        exceeded, ⚠️ if at >=80% of either.
        """
        today_since, today_until = _period_window_utc("today", self._tz)
        month_since, month_until = _period_window_utc("month", self._tz)
        today_row = await self._store.usage_totals_between(today_since, today_until)
        month_row = await self._store.usage_totals_between(month_since, month_until)
        today_usd = today_row["cost_usd_micro"] / 1_000_000
        month_usd = month_row["cost_usd_micro"] / 1_000_000
        today_pct = (
            today_usd / self._daily_usd * 100 if self._daily_usd > 0 else 0.0
        )
        month_pct = (
            month_usd / self._monthly_usd * 100 if self._monthly_usd > 0 else 0.0
        )
        flag = _alert_flag(today_pct, month_pct)
        lines = [
            f"Cost alert {flag}".rstrip(),
            f"  today:  ${today_usd:.4f} / ${self._daily_usd:.2f}  ({today_pct:.1f}%)",
            f"  month:  ${month_usd:.4f} / ${self._monthly_usd:.2f}  ({month_pct:.1f}%)",
        ]
        return "\n".join(lines)


def _alert_flag(today_pct: float, month_pct: float) -> str:
    if today_pct >= 100 or month_pct >= 100:
        return "🚨 over limit"
    if today_pct >= 80 or month_pct >= 80:
        return "⚠️ near limit"
    return "ok"


def alert_is_above_threshold(today_pct: float, month_pct: float, pct: float) -> bool:
    """Predicate used by the retrospective writer to decide whether to surface
    the cost banner. Module-level so it stays in one place."""
    return today_pct >= pct or month_pct >= pct


__all__ = [
    "UsageTools",
    "_VALID_PERIODS",
    "_VALID_GROUPS",
    "alert_is_above_threshold",
]
