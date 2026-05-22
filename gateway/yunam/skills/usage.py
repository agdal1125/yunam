"""Usage / cost reporting skill — read-only view over `api_usage`.

Three tools, all `Scope.USAGE_READ`:
  - `usage_summary(period)` — total tokens + cost for a period (today, week, ...)
  - `usage_breakdown(period, group_by)` — same totals grouped by provider /
    model_or_endpoint / skill_id, sorted by cost
  - `cost_alert_status()` — today/month spend vs ENV thresholds

All three are safe to call without any side effects. Cost numbers come from
the rate tables in `usage/rates.py`; tweak there, not here.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.usage import UsageTools, _VALID_GROUPS, _VALID_PERIODS
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "usage"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## API usage / cost

When jaekeun asks about API spend, token usage, or cache efficiency
('비용 얼마 썼어?', '오늘 토큰 얼마나 썼지?', 'cache hit rate 어때?'),
call one of these read-only tools:

- `usage_summary(period)` — overall totals (tokens in/out, cache hit %,
  estimated USD) for `today`, `yesterday`, `week`, `month`, `7d`, or `30d`.
- `usage_breakdown(period, group_by)` — same totals grouped by `provider`,
  `model_or_endpoint`, or `skill_id`. Use this when jaekeun wants to see
  *which* skill or model is driving spend.
- `cost_alert_status()` — today/month dollars vs the configured limits.
  Use when jaekeun asks 'limit 얼마 남았어?' or 'over budget?'.

Don't speculate from memory — these tools are the only authoritative source.
The numbers are estimates based on published per-1M-token pricing in
`yunam/usage/rates.py`; surface that caveat if jaekeun is making a procurement
decision.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "usage_summary": {
        "name": "usage_summary",
        "description": (
            "Total tokens, cache hit rate, and estimated USD cost over a "
            "period. Use for 'how much have we spent?' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": list(_VALID_PERIODS),
                    "description": (
                        "Window to summarize: today, yesterday, this calendar "
                        "week (Mon-anchored), this calendar month, last 7 "
                        "days, or last 30 days. Defaults to today."
                    ),
                },
            },
        },
    },
    "usage_breakdown": {
        "name": "usage_breakdown",
        "description": (
            "Group api_usage by provider, model, or skill, returning rows "
            "sorted by cost desc. Use when jaekeun asks which skill/model "
            "drove cost."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": list(_VALID_PERIODS),
                    "description": "Same period vocabulary as usage_summary.",
                },
                "group_by": {
                    "type": "string",
                    "enum": list(_VALID_GROUPS),
                    "description": (
                        "Column to group on. `provider` = anthropic | voyage | "
                        "jina | ... ; `model_or_endpoint` = claude-sonnet-4-6 | "
                        "voyage-multimodal-3 | reader | ...; `skill_id` = the "
                        "owning skill name."
                    ),
                },
            },
            "required": ["group_by"],
        },
    },
    "cost_alert_status": {
        "name": "cost_alert_status",
        "description": (
            "Today and month-to-date spend compared to the configured USD "
            "alert thresholds. Returns a one-shot status ('ok' / 'near limit' "
            "/ 'over limit') plus the raw numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


def build_usage_skill(tools: UsageTools) -> Skill:
    async def _summary(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        period = (inputs.get("period") or "today").strip()
        return await tools.summary(period=period)

    async def _breakdown(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        period = (inputs.get("period") or "today").strip()
        group_by = (inputs.get("group_by") or "provider").strip()
        return await tools.breakdown(period=period, group_by=group_by)

    async def _alert(_inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.cost_alert_status()

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("usage_summary", Scope.USAGE_READ, _SCHEMAS["usage_summary"], _summary),
        ToolSpec(
            "usage_breakdown",
            Scope.USAGE_READ,
            _SCHEMAS["usage_breakdown"],
            _breakdown,
        ),
        ToolSpec(
            "cost_alert_status",
            Scope.USAGE_READ,
            _SCHEMAS["cost_alert_status"],
            _alert,
        ),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
