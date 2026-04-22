"""Parcel-tracking skill — wraps `ParcelTools` (Sweet Tracker) with scope + schema.

Sweet Tracker free tier (1000/day) requires a one-time signup; the key goes in
`.env` as `SWEETTRACKER_API_KEY`. Without it, the tool surfaces a clear
onboarding error rather than silently failing.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.parcel import ParcelTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "parcel"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Parcel tracking (택배 배송조회)

Call `parcel_track(carrier, tracking_no)` when jaekeun asks "택배 어디까지 왔어?",
"배송 조회해줘", or provides a tracking number.

- `carrier` accepts friendly names: "CJ대한통운"/"CJ"/"대한통운", "우체국",
  "한진", "롯데", "로젠", "쿠팡". Pass whatever jaekeun says.
- `tracking_no` is the invoice/운송장 number.

If jaekeun doesn't name the carrier, ask once — tracking number format alone
isn't enough to disambiguate reliably.

The tool needs `SWEETTRACKER_API_KEY` in `.env` (free signup, 1000 requests/day).
If it returns an onboarding error, relay the signup URL to jaekeun verbatim;
don't fabricate an alternative tool.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "parcel_track": {
        "name": "parcel_track",
        "description": (
            "Track a Korean parcel by carrier + tracking number. Returns the "
            "current status, latest event, and recent history. Carrier accepts "
            "friendly Korean or English names for major carriers (CJ대한통운, "
            "우체국, 한진, 롯데, 로젠, 쿠팡)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "carrier": {
                    "type": "string",
                    "description": (
                        "Carrier name — 'CJ대한통운', '우체국', '한진', '롯데', "
                        "'로젠', '쿠팡' (Korean or English acceptable)."
                    ),
                },
                "tracking_no": {
                    "type": "string",
                    "description": "Tracking (운송장) number.",
                },
            },
            "required": ["carrier", "tracking_no"],
        },
    },
}


def build_parcel_skill(tools: ParcelTools) -> Skill:
    """Wrap a resolved `ParcelTools` instance as a Skill."""

    async def _track(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.parcel_track(**inputs)

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("parcel_track", Scope.PARCEL_READ, _SCHEMAS["parcel_track"], _track),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
