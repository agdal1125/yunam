"""Parcel-tracking primitives via Sweet Tracker API.

Sweet Tracker (info.sweettracker.co.kr) is the standard free-tier Korean
parcel-tracking proxy — 1 API per day for unregistered, 1000/day after a
one-minute free signup. Getting a true keyless endpoint for Korean carriers
isn't practical: tracker.delivery also gates behind Authorization, CJ /
우체국 / 한진 don't publish open APIs, and scraping carrier pages is
fragile.

Endpoint (documented form):
    GET http://info.sweettracker.co.kr/api/v1/trackingInfo
    params: t_key (API key), t_code (carrier code), t_invoice (tracking number)
    response: JSON with trackingDetails[] and lastDetail

If `SWEETTRACKER_API_KEY` is unset, the tool surfaces a clear onboarding
error rather than silently failing — user finishes signup and re-deploys.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

ENDPOINT: Final = "https://info.sweettracker.co.kr/api/v1/trackingInfo"
DEFAULT_TIMEOUT_S: Final = 10.0
MAX_EVENTS: Final = 15


class ParcelError(Exception):
    """Raised by parcel primitives for anything the tool should surface."""


# Sweet Tracker carrier codes for the major Korean carriers. Friendly aliases
# (Korean + English) are mapped to the official code so Claude can pass
# whatever the user says.
_CARRIER_ALIASES: dict[str, str] = {
    # 04 — CJ 대한통운
    "cj": "04",
    "cj대한통운": "04",
    "cj 대한통운": "04",
    "대한통운": "04",
    "cjlogistics": "04",
    # 01 — 우체국택배 (Korea Post)
    "우체국": "01",
    "우체국택배": "01",
    "epost": "01",
    "koreapost": "01",
    "korea post": "01",
    # 05 — 한진택배
    "한진": "05",
    "한진택배": "05",
    "hanjin": "05",
    # 08 — 롯데택배
    "롯데": "08",
    "롯데택배": "08",
    "lotte": "08",
    # 06 — 로젠택배
    "로젠": "06",
    "로젠택배": "06",
    "logen": "06",
    # 48 — 쿠팡 로지스틱스
    "쿠팡": "48",
    "쿠팡택배": "48",
    "coupang": "48",
    "cls": "48",
}


def _resolve_carrier(carrier: str) -> str:
    """Convert a friendly name to a Sweet Tracker 2-digit code."""
    c = (carrier or "").strip().lower()
    if not c:
        raise ParcelError("carrier is required")
    # Already a 2-digit code?
    if c.isdigit() and len(c) == 2:
        return c
    code = _CARRIER_ALIASES.get(c)
    if code is None:
        raise ParcelError(
            f"unknown carrier {carrier!r}. Supported: CJ대한통운, 우체국, 한진, "
            "롯데, 로젠, 쿠팡 (or Sweet Tracker 2-digit codes)."
        )
    return code


class ParcelTools:
    """Async wrapper around Sweet Tracker. One instance per process."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def parcel_track(self, carrier: str, tracking_no: str) -> str:
        if not self._api_key:
            raise ParcelError(
                "SWEETTRACKER_API_KEY is not configured. Sign up (free, 1000/day) at "
                "https://info.sweettracker.co.kr/apikey/add and add the key to .env."
            )
        tracking_no = (tracking_no or "").strip()
        if not tracking_no:
            raise ParcelError("tracking_no is required")

        carrier_code = _resolve_carrier(carrier)

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            r = await client.get(
                ENDPOINT,
                params={
                    "t_key": self._api_key,
                    "t_code": carrier_code,
                    "t_invoice": tracking_no,
                },
            )
            if r.status_code != 200:
                raise ParcelError(
                    f"Sweet Tracker returned HTTP {r.status_code}: {r.text[:200]}"
                )
            data = r.json()

        # Sweet Tracker error shape: {"code": "...", "msg": "...", "status": false}
        if data.get("status") is False or data.get("code"):
            raise ParcelError(
                f"Sweet Tracker error: {data.get('msg') or data.get('code') or data}"
            )

        return _format_tracking(data, carrier, tracking_no)


def _format_tracking(data: dict[str, Any], carrier: str, tracking_no: str) -> str:
    details = data.get("trackingDetails") or []
    last = data.get("lastDetail") or {}
    sender = data.get("senderName")
    receiver = data.get("receiverName")
    item = data.get("itemName")
    level = data.get("level")
    complete = data.get("complete")

    lines = [
        f"택배 조회 — {carrier} / {tracking_no}",
    ]
    meta_parts = []
    if item:
        meta_parts.append(f"상품: {item}")
    if sender:
        meta_parts.append(f"보낸이: {sender}")
    if receiver:
        meta_parts.append(f"받는이: {receiver}")
    if meta_parts:
        lines.append("  " + " · ".join(meta_parts))

    if complete:
        lines.append("  상태: 배송완료 ✅")
    elif level is not None:
        # Sweet Tracker levels: 1 상품인수 / 2 상품이동중 / 3 배송지도착 /
        # 4 배송출발 / 5 배송완료 / 6 미배달 / 7 기타
        stage = {
            1: "상품인수",
            2: "상품이동중",
            3: "배송지도착",
            4: "배송출발",
            5: "배송완료",
            6: "미배달",
        }.get(level, f"단계 {level}")
        lines.append(f"  상태: {stage}")

    if last:
        last_time = last.get("timeString") or last.get("time") or ""
        last_where = last.get("where") or ""
        last_kind = last.get("kind") or ""
        tail = " · ".join(x for x in (last_time, last_where, last_kind) if x)
        if tail:
            lines.append(f"  최근: {tail}")

    if details:
        lines.append("")
        lines.append("이력:")
        for event in details[-MAX_EVENTS:]:
            t = event.get("timeString") or event.get("time") or ""
            where = event.get("where") or ""
            kind = event.get("kind") or ""
            lines.append(f"  {t}  {where}  {kind}".rstrip())

    return "\n".join(lines)
