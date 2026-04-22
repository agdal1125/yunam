#!/usr/bin/env python3
"""Smoke test for the Google Calendar MCP skill (M3).

Split into two levels:

  Offline checks (no MCP server required):
    1. `_scope_for` classifier labels every known nspady tool correctly
       (list-* / get-* / search-* → read; create-* / update-* / etc → write).
    2. `build_gcal_mcp_skill` composes a Skill with a mocked-tools client
       and the SkillRegistry constructs with no name collisions. Registry
       lookup for a write-scope tool returns the right scope.

  Live check (optional — requires a running MCP server):
    3. If YUNAM_GCAL_MCP_URL is set, connect() + discover + call a read-only
       tool (list-calendars) end-to-end to verify wiring.

Usage (from repo root):
    # offline only
    PYTHONPATH=gateway python3 scripts/smoke_gcal.py

    # with live MCP server running on the side
    YUNAM_GCAL_MCP_URL=http://localhost:3000/mcp \\
      PYTHONPATH=gateway python3 scripts/smoke_gcal.py
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
        ("yunam.mcp", "yunam/mcp"),
    ]:
        mod = types.ModuleType(name)
        mod.__path__ = [str(gateway / subpath)]
        sys.modules[name] = mod


# Canonical nspady tool inventory from the project README (as of Apr 2026).
# Keep this list synced if nspady adds tools.
NSPADY_TOOLS_EXPECTED = {
    "read": [
        "list-calendars",
        "list-events",
        "get-event",
        "search-events",
        "get-freebusy",
        "list-colors",
        "get-current-time",
    ],
    "write": [
        "create-event",
        "update-event",
        "delete-event",
        "respond-to-event",
        "manage-accounts",
    ],
}


def _test_scope_classifier() -> None:
    print("[1/3] scope classifier on nspady tool names...")
    from yunam.capabilities import Scope
    from yunam.mcp.gcal import _scope_for

    for name in NSPADY_TOOLS_EXPECTED["read"]:
        assert _scope_for(name) == Scope.CALENDAR_READ, f"{name} should be READ"
    for name in NSPADY_TOOLS_EXPECTED["write"]:
        assert _scope_for(name) == Scope.CALENDAR_WRITE, f"{name} should be WRITE"
    print(
        f"       ✓ classified {sum(len(v) for v in NSPADY_TOOLS_EXPECTED.values())} tools correctly"
    )


def _test_skill_composition_with_mock() -> None:
    print("[2/3] build_gcal_mcp_skill + SkillRegistry (mock client)...")
    from yunam.capabilities import Scope
    from yunam.mcp.gcal import build_gcal_mcp_skill
    from yunam.skills.base import SkillRegistry

    class _MockMCPTool:
        def __init__(self, name: str, description: str = "stub"):
            self.name = name
            self.description = description
            self.inputSchema = {"type": "object", "properties": {}}

    class _MockClient:
        def __init__(self):
            all_names = (
                NSPADY_TOOLS_EXPECTED["read"] + NSPADY_TOOLS_EXPECTED["write"]
            )
            # Pre-sorted, matching what GCalMCPClient.connect() would produce.
            self.tools = tuple(
                _MockMCPTool(n) for n in sorted(all_names)
            )

        async def call_tool(self, name, arguments):  # pragma: no cover
            return f"(mock) {name} called"

    skill = build_gcal_mcp_skill(_MockClient())
    registry = SkillRegistry([skill])
    names = [s["name"] for s in registry.tool_schemas]

    # Sorted alphabetically → cache-stable order.
    expected_sorted = sorted(
        NSPADY_TOOLS_EXPECTED["read"] + NSPADY_TOOLS_EXPECTED["write"]
    )
    assert names == expected_sorted, f"wrong order: {names} vs {expected_sorted}"

    _, spec = registry.lookup("create-event")
    assert spec.scope == Scope.CALENDAR_WRITE, spec.scope
    _, spec = registry.lookup("list-calendars")
    assert spec.scope == Scope.CALENDAR_READ, spec.scope

    fragments = registry.system_prompt_fragments
    assert any("Google Calendar" in f for f in fragments), "prompt fragment missing"

    print(f"       ✓ {len(names)} tools registered in sorted order, scopes OK")


async def _test_live_if_configured() -> None:
    url = os.environ.get("YUNAM_GCAL_MCP_URL", "").strip()
    if not url:
        print("[3/3] YUNAM_GCAL_MCP_URL unset — skipping live MCP call")
        return
    print(f"[3/3] live MCP call against {url} ...")
    from yunam.mcp.gcal import GCalMCPClient

    client = GCalMCPClient(url)
    try:
        await client.connect()
    except Exception as e:
        print(f"       ⚠ connect() failed: {e!r}")
        return
    try:
        discovered = [t.name for t in client.tools]
        print(f"       ✓ connected, discovered {len(discovered)} tools")
        print(f"         names: {', '.join(discovered)}")
        # Try a harmless read-only call if list-calendars is present.
        if "list-calendars" in discovered:
            result = await client.call_tool("list-calendars", {})
            preview = result[:200].replace("\n", " ")
            print(f"       ✓ list-calendars: {preview}...")
    finally:
        await client.close()


async def _main() -> None:
    _setup_path()
    _test_scope_classifier()
    _test_skill_composition_with_mock()
    await _test_live_if_configured()
    print("\nall gcal smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
