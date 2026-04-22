#!/usr/bin/env python3
"""Smoke test for the Korean-skill bundle (M2): airquality + parcel.

Verifies registry boot + live API calls where feasible:

  1. Both skills register cleanly, declared order, scopes correct.
  2. airquality_lookup('서울') — real Open-Meteo call, keyless.
  3. parcel_track — with SWEETTRACKER_API_KEY unset we just confirm the
     onboarding error fires. With the env var set we do a real call if
     PARCEL_TEST_CARRIER/PARCEL_TEST_INVOICE are also set.

Usage (from repo root):
    PYTHONPATH=gateway python3 scripts/smoke_korean.py

Optional environment overrides:
    SWEETTRACKER_API_KEY=<key>
    PARCEL_TEST_CARRIER=CJ대한통운
    PARCEL_TEST_INVOICE=1234567890
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path


def _setup_path() -> None:
    gateway = Path(__file__).resolve().parent.parent / "gateway"
    sys.path.insert(0, str(gateway))
    for name, subpath in [
        ("yunam", "yunam"),
        ("yunam.skills", "yunam/skills"),
        ("yunam.tools", "yunam/tools"),
    ]:
        mod = types.ModuleType(name)
        mod.__path__ = [str(gateway / subpath)]
        sys.modules[name] = mod


async def _test_registry() -> None:
    print("[1/3] registry boot (2 Korean skills)...")
    from yunam.capabilities import Scope
    from yunam.skills.airquality import build_airquality_skill
    from yunam.skills.base import SkillRegistry
    from yunam.skills.parcel import build_parcel_skill
    from yunam.tools.airquality import AirQualityTools
    from yunam.tools.parcel import ParcelTools

    registry = SkillRegistry(
        [
            build_airquality_skill(AirQualityTools()),
            build_parcel_skill(ParcelTools(api_key=None)),
        ]
    )
    names = [s["name"] for s in registry.tool_schemas]
    expected = ["airquality_lookup", "parcel_track"]
    assert names == expected, f"tool order off: {names}"

    _, t_aq = registry.lookup("airquality_lookup")
    _, t_pt = registry.lookup("parcel_track")
    assert t_aq.scope == Scope.AIRQUALITY_READ
    assert t_pt.scope == Scope.PARCEL_READ

    fragments = registry.system_prompt_fragments
    assert any("Air quality" in f for f in fragments)
    assert any("Parcel tracking" in f for f in fragments)
    print(f"       ✓ 2 tools across 2 skills registered in order: {' → '.join(names)}")


async def _test_airquality_live() -> None:
    print("[2/3] airquality_lookup('서울') via Open-Meteo...")
    from yunam.tools.airquality import AirQualityTools

    tools = AirQualityTools()
    result = await tools.airquality_lookup(location="서울")
    assert isinstance(result, str), type(result)
    assert "PM" in result or "AQI" in result, result[:200]
    print(f"       ✓ {len(result)} chars")
    for line in result.splitlines()[:6]:
        print(f"       | {line}")


async def _test_parcel() -> None:
    print("[3/3] parcel_track onboarding / live call...")
    from yunam.tools.parcel import ParcelError, ParcelTools

    key = os.environ.get("SWEETTRACKER_API_KEY") or None
    tools = ParcelTools(api_key=key)

    if not key:
        raised = False
        try:
            await tools.parcel_track(carrier="CJ", tracking_no="1234567890")
        except ParcelError as e:
            raised = True
            msg = str(e)
            assert "SWEETTRACKER_API_KEY" in msg, msg
            assert "info.sweettracker.co.kr" in msg, msg
        assert raised, "expected ParcelError for missing key"
        print("       ✓ onboarding error fires when key is unset")
        return

    carrier = os.environ.get("PARCEL_TEST_CARRIER")
    invoice = os.environ.get("PARCEL_TEST_INVOICE")
    if not (carrier and invoice):
        print("       ⚠ key set but no PARCEL_TEST_CARRIER/INVOICE — skipping live call")
        return

    try:
        result = await tools.parcel_track(carrier=carrier, tracking_no=invoice)
    except ParcelError as e:
        print(f"       ⚠ tracker returned: {e}")
        return
    assert isinstance(result, str)
    print(f"       ✓ tracking returned {len(result)} chars")
    for line in result.splitlines()[:6]:
        print(f"       | {line}")


async def _main() -> None:
    _setup_path()
    await _test_registry()
    await _test_airquality_live()
    await _test_parcel()
    print("\nall Korean-skill smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
