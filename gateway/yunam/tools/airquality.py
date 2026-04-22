"""Air-quality primitives via Open-Meteo (keyless, free, no rate-limit friction).

Two endpoints:
  - geocoding-api.open-meteo.com  → "Seoul" → (lat, lng, resolved_name)
  - air-quality-api.open-meteo.com → (lat, lng) → PM2.5 / PM10 / AQI now

Using Open-Meteo instead of 에어코리아 (airkorea.or.kr) because Open-Meteo is
keyless and offers worldwide coverage in one call. Accuracy for Korean cities
is comparable since Open-Meteo pulls from CAMS + ECMWF which ingest 에어코리아
station data anyway. No registration, no public-data-portal API-key dance.

Korean PM grade bands follow the 환경부 standard for user-friendly labeling.
"""

from __future__ import annotations

from typing import Final

import httpx

GEOCODE_ENDPOINT: Final = "https://geocoding-api.open-meteo.com/v1/search"
AIRQ_ENDPOINT: Final = "https://air-quality-api.open-meteo.com/v1/air-quality"
DEFAULT_TIMEOUT_S: Final = 10.0

# Open-Meteo's geocoder is backed by GeoNames which doesn't index Korean
# Hangul place names — "서울" returns zero results, while "Seoul" works. Rather
# than carry a translator, we hard-code coords for the 17 광역 시/도 plus Seoul
# district (구) centroids. A miss falls through to Open-Meteo as-is.
_KR_COORDS: dict[str, tuple[float, float, str]] = {
    # 광역 시/도
    "서울": (37.5665, 126.9780, "서울특별시"),
    "서울특별시": (37.5665, 126.9780, "서울특별시"),
    "부산": (35.1796, 129.0756, "부산광역시"),
    "부산광역시": (35.1796, 129.0756, "부산광역시"),
    "인천": (37.4563, 126.7052, "인천광역시"),
    "인천광역시": (37.4563, 126.7052, "인천광역시"),
    "대구": (35.8714, 128.6014, "대구광역시"),
    "대구광역시": (35.8714, 128.6014, "대구광역시"),
    "광주": (35.1595, 126.8526, "광주광역시"),
    "광주광역시": (35.1595, 126.8526, "광주광역시"),
    "대전": (36.3504, 127.3845, "대전광역시"),
    "대전광역시": (36.3504, 127.3845, "대전광역시"),
    "울산": (35.5384, 129.3114, "울산광역시"),
    "울산광역시": (35.5384, 129.3114, "울산광역시"),
    "세종": (36.4800, 127.2890, "세종특별자치시"),
    "세종특별자치시": (36.4800, 127.2890, "세종특별자치시"),
    "경기": (37.4138, 127.5183, "경기도"),
    "경기도": (37.4138, 127.5183, "경기도"),
    "강원": (37.8228, 128.1555, "강원특별자치도"),
    "강원도": (37.8228, 128.1555, "강원특별자치도"),
    "충북": (36.6356, 127.4913, "충청북도"),
    "충청북도": (36.6356, 127.4913, "충청북도"),
    "충남": (36.5184, 126.8000, "충청남도"),
    "충청남도": (36.5184, 126.8000, "충청남도"),
    "전북": (35.7175, 127.1530, "전북특별자치도"),
    "전북도": (35.7175, 127.1530, "전북특별자치도"),
    "전남": (34.8679, 126.9910, "전라남도"),
    "전라남도": (34.8679, 126.9910, "전라남도"),
    "경북": (36.5760, 128.5056, "경상북도"),
    "경상북도": (36.5760, 128.5056, "경상북도"),
    "경남": (35.4606, 128.2132, "경상남도"),
    "경상남도": (35.4606, 128.2132, "경상남도"),
    "제주": (33.4996, 126.5312, "제주특별자치도"),
    "제주도": (33.4996, 126.5312, "제주특별자치도"),
    # 서울 25개 자치구
    "강남구": (37.5172, 127.0473, "서울 강남구"),
    "강남": (37.5172, 127.0473, "서울 강남구"),
    "강동구": (37.5301, 127.1238, "서울 강동구"),
    "강서구": (37.5509, 126.8495, "서울 강서구"),
    "강북구": (37.6396, 127.0257, "서울 강북구"),
    "관악구": (37.4784, 126.9516, "서울 관악구"),
    "광진구": (37.5384, 127.0822, "서울 광진구"),
    "구로구": (37.4954, 126.8874, "서울 구로구"),
    "금천구": (37.4569, 126.8955, "서울 금천구"),
    "노원구": (37.6542, 127.0568, "서울 노원구"),
    "도봉구": (37.6688, 127.0471, "서울 도봉구"),
    "동대문구": (37.5744, 127.0395, "서울 동대문구"),
    "동작구": (37.5124, 126.9393, "서울 동작구"),
    "마포구": (37.5663, 126.9014, "서울 마포구"),
    "서대문구": (37.5791, 126.9368, "서울 서대문구"),
    "서초구": (37.4836, 127.0327, "서울 서초구"),
    "성동구": (37.5634, 127.0370, "서울 성동구"),
    "성북구": (37.5894, 127.0167, "서울 성북구"),
    "송파구": (37.5145, 127.1060, "서울 송파구"),
    "양천구": (37.5170, 126.8665, "서울 양천구"),
    "영등포구": (37.5263, 126.8962, "서울 영등포구"),
    "용산구": (37.5324, 126.9900, "서울 용산구"),
    "은평구": (37.6027, 126.9290, "서울 은평구"),
    "종로구": (37.5729, 126.9794, "서울 종로구"),
    "중구": (37.5636, 126.9976, "서울 중구"),
    "중랑구": (37.6066, 127.0927, "서울 중랑구"),
}


def _lookup_kr_coords(query: str) -> tuple[float, float, str] | None:
    """Return (lat, lng, pretty_name) if `query` matches a Korean place
    in our built-in LUT, else None. Accepts exact + trailing-'시' variants."""
    q = query.strip()
    hit = _KR_COORDS.get(q)
    if hit is not None:
        return hit
    # "서울시" / "부산시" / "성남시" — strip trailing 시 then retry.
    if q.endswith("시") and len(q) > 1:
        return _KR_COORDS.get(q[:-1])
    return None


class AirQualityError(Exception):
    """Raised by airquality primitives for anything the tool should surface."""


def _grade_pm25(v: float) -> str:
    # Korean 환경부 PM2.5 standard (µg/m³)
    if v <= 15:
        return "좋음"
    if v <= 35:
        return "보통"
    if v <= 75:
        return "나쁨"
    return "매우나쁨"


def _grade_pm10(v: float) -> str:
    # Korean 환경부 PM10 standard (µg/m³)
    if v <= 30:
        return "좋음"
    if v <= 80:
        return "보통"
    if v <= 150:
        return "나쁨"
    return "매우나쁨"


class AirQualityTools:
    """Async air-quality lookups. One instance per process."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._timeout_s = timeout_s

    async def airquality_lookup(
        self,
        location: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
    ) -> str:
        """Look up current air quality by place name OR (lat, lng)."""
        if lat is not None and lng is not None:
            resolved_name = f"{lat:.3f}°, {lng:.3f}°"
        else:
            if not location or not location.strip():
                raise AirQualityError("either `location` or both (lat, lng) are required")
            # Korean place names: try the built-in LUT first — Open-Meteo's
            # geocoder doesn't index Hangul.
            kr_hit = _lookup_kr_coords(location.strip())
            if kr_hit is not None:
                lat, lng, resolved_name = kr_hit
            else:
                lat, lng, resolved_name = await self._geocode(location.strip())

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            r = await client.get(
                AIRQ_ENDPOINT,
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "current": "pm10,pm2_5,european_aqi,us_aqi",
                    "timezone": "auto",
                },
            )
            if r.status_code != 200:
                raise AirQualityError(
                    f"Open-Meteo air-quality returned HTTP {r.status_code}: {r.text[:200]}"
                )
            data = r.json()

        current = data.get("current") or {}
        time_tag = current.get("time", "")
        pm10 = current.get("pm10")
        pm25 = current.get("pm2_5")
        eu_aqi = current.get("european_aqi")
        us_aqi = current.get("us_aqi")

        lines = [f"Air quality — {resolved_name}" + (f" ({time_tag})" if time_tag else "")]
        if pm25 is not None:
            lines.append(f"  PM2.5: {pm25} µg/m³ ({_grade_pm25(pm25)})")
        if pm10 is not None:
            lines.append(f"  PM10:  {pm10} µg/m³ ({_grade_pm10(pm10)})")
        if eu_aqi is not None:
            lines.append(f"  EU AQI: {eu_aqi}")
        if us_aqi is not None:
            lines.append(f"  US AQI: {us_aqi}")
        if len(lines) == 1:
            raise AirQualityError("Open-Meteo returned no air-quality metrics for this location")
        return "\n".join(lines)

    async def _geocode(self, location: str) -> tuple[float, float, str]:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            r = await client.get(
                GEOCODE_ENDPOINT,
                params={"name": location, "count": 1, "language": "ko"},
            )
            if r.status_code != 200:
                raise AirQualityError(
                    f"Open-Meteo geocoding returned HTTP {r.status_code}: {r.text[:200]}"
                )
            data = r.json()
        results = data.get("results") or []
        if not results:
            raise AirQualityError(f"location not found: {location!r}")
        top = results[0]
        # Build a human-readable name: "Seoul, Republic of Korea"
        parts = [top.get("name", location)]
        if top.get("admin1") and top["admin1"] != top.get("name"):
            parts.append(top["admin1"])
        if top.get("country"):
            parts.append(top["country"])
        pretty = ", ".join(parts)
        return float(top["latitude"]), float(top["longitude"]), pretty
