"""Air-quality skill — wraps `AirQualityTools` with a scope + schema + prompt.

Backed by Open-Meteo (keyless). See `tools/airquality.py` for the primitive
implementation and the Korean PM grade bands used in the response.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.airquality import AirQualityTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "airquality"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Air quality (미세먼지)

You can look up current PM2.5 / PM10 / AQI for any location worldwide via
`airquality_lookup`. Use it when jaekeun asks about 미세먼지, 공기질,
"밖에 나가도 괜찮아?" etc.

- Default to passing a Korean place name (e.g. "서울 강남구", "부산 해운대")
  as `location`. Korean names work — the geocoder is multilingual.
- Pass `lat` and `lng` instead only when jaekeun gives explicit coordinates.

The returned grade bands (좋음/보통/나쁨/매우나쁨) follow the 환경부 standard.
Summarize the result in plain Korean with one actionable takeaway (e.g.
"마스크 쓰고 나가는 게 좋겠어요"), not a raw data dump.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "airquality_lookup": {
        "name": "airquality_lookup",
        "description": (
            "Look up current air quality (PM2.5, PM10, EU AQI, US AQI) for a "
            "location. Provide either `location` (place name — Korean OK) or "
            "both `lat` and `lng`. Returns a short text summary with Korean "
            "grade labels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": (
                        "Place name, Korean or English (e.g. '서울', '강남구', "
                        "'Busan'). Used only if lat/lng are not provided."
                    ),
                },
                "lat": {
                    "type": "number",
                    "description": "Latitude in decimal degrees. Use with `lng`.",
                },
                "lng": {
                    "type": "number",
                    "description": "Longitude in decimal degrees. Use with `lat`.",
                },
            },
            "required": [],
        },
    },
}


def build_airquality_skill(tools: AirQualityTools) -> Skill:
    """Wrap a resolved `AirQualityTools` instance as a Skill."""

    async def _lookup(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.airquality_lookup(**inputs)

    specs: tuple[ToolSpec, ...] = (
        ToolSpec(
            "airquality_lookup",
            Scope.AIRQUALITY_READ,
            _SCHEMAS["airquality_lookup"],
            _lookup,
        ),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
