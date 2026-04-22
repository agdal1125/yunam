#!/usr/bin/env python3
"""Smoke test for the dual-model (Sonnet default + Opus /think) setup.

Verifies without burning API tokens that:

  1. `Orchestrator` default config = Sonnet 4.6, max_tokens=4096, no thinking.
  2. `build_deep_think_orchestrator` = Opus 4.7 + adaptive thinking / high effort.
  3. Both share the same system prompt + tool schemas (same registry).
  4. A fake Claude turn through each path emits the correct model in the
     `messages.create(model=...)` call (so we know /think really routes to Opus).

Usage (from repo root):
    PYTHONPATH=gateway python3 scripts/smoke_dual_model.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path


def _setup_path() -> None:
    """Inject namespace-only package stubs so we avoid aiosqlite etc."""
    gateway = Path(__file__).resolve().parent.parent / "gateway"
    sys.path.insert(0, str(gateway))

    for name, subpath in [
        ("yunam", "yunam"),
        ("yunam.skills", "yunam/skills"),
        ("yunam.tools", "yunam/tools"),
        ("yunam.subagents", "yunam/subagents"),
    ]:
        mod = types.ModuleType(name)
        mod.__path__ = [str(gateway / subpath)]
        sys.modules[name] = mod


class _RecordingMessages:
    """Records every `create()` call's kwargs so we can inspect the model sent."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        # Minimal stub response — end_turn with one text block.
        class _Block:
            type = "text"
            text = "(fake) ok"

            def model_dump(self, exclude_none: bool = True):
                return {"type": "text", "text": "(fake) ok"}

        class _Usage:
            input_tokens = 100
            output_tokens = 10
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        class _Resp:
            content = [_Block()]
            stop_reason = "end_turn"
            usage = _Usage()

        return _Resp()


class _FakeClaude:
    def __init__(self):
        self.messages = _RecordingMessages()


class _FakeStore:
    async def load_history(self, chat_id):
        return []

    async def persist_turn(self, **kwargs):
        return


async def _test_defaults_are_sonnet() -> None:
    print("[1/4] Orchestrator defaults...")
    from yunam.orchestrator import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, Orchestrator
    from yunam.skills.base import SkillRegistry
    from yunam.skills.web import build_web_skill
    from yunam.tools.web import WebTools

    assert DEFAULT_MODEL == "claude-sonnet-4-6", DEFAULT_MODEL
    assert DEFAULT_MAX_TOKENS == 4096, DEFAULT_MAX_TOKENS

    client = _FakeClaude()
    registry = SkillRegistry([build_web_skill(WebTools())])
    orch = Orchestrator(client, _FakeStore(), registry)

    # Don't exercise the full graph (persist_turn etc.) — just poke agent_step.
    await orch._agent_step_node({"chat_id": 1, "user_text": "hi", "history": []})

    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-6", call["model"]
    assert call["max_tokens"] == 4096, call["max_tokens"]
    assert "thinking" not in call, f"Sonnet path should omit `thinking`, got: {call.get('thinking')}"
    assert "output_config" not in call, (
        f"Sonnet path should omit `output_config`, got: {call.get('output_config')}"
    )
    print("       ✓ model=Sonnet, max_tokens=4096, no thinking, no output_config")


async def _test_deep_think_is_opus() -> None:
    print("[2/4] build_deep_think_orchestrator routes to Opus...")
    from yunam.skills.base import SkillRegistry
    from yunam.skills.web import build_web_skill
    from yunam.subagents.deep_think import build_deep_think_orchestrator
    from yunam.tools.web import WebTools

    client = _FakeClaude()
    registry = SkillRegistry([build_web_skill(WebTools())])
    deep = build_deep_think_orchestrator(client, _FakeStore(), registry)

    await deep._agent_step_node({"chat_id": 1, "user_text": "hard problem", "history": []})

    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-7", call["model"]
    assert call["max_tokens"] == 8000, call["max_tokens"]
    assert call.get("thinking") == {"type": "adaptive"}, call.get("thinking")
    assert call.get("output_config") == {"effort": "high"}, call.get("output_config")
    print("       ✓ model=Opus, max_tokens=8000, thinking=adaptive, effort=high")


async def _test_both_share_registry() -> None:
    print("[3/4] both paths share the same SkillRegistry / system prompt...")
    from yunam.orchestrator import Orchestrator
    from yunam.skills.base import SkillRegistry
    from yunam.skills.web import build_web_skill
    from yunam.subagents.deep_think import build_deep_think_orchestrator
    from yunam.tools.web import WebTools

    client = _FakeClaude()
    registry = SkillRegistry([build_web_skill(WebTools())])
    main_orch = Orchestrator(client, _FakeStore(), registry)
    deep_orch = build_deep_think_orchestrator(client, _FakeStore(), registry)

    assert main_orch._tool_schemas == deep_orch._tool_schemas
    assert main_orch._system_prompt == deep_orch._system_prompt
    print("       ✓ identical tool schemas + system prompt across paths")


def _test_web_bytes_cap_reduced() -> None:
    print("[4/4] WebTools MAX_BYTES lowered...")
    from yunam.tools.web import MAX_BYTES

    assert MAX_BYTES == 80_000, MAX_BYTES
    print(f"       ✓ MAX_BYTES = {MAX_BYTES} (was 500_000)")


async def _main() -> None:
    _setup_path()
    await _test_defaults_are_sonnet()
    await _test_deep_think_is_opus()
    await _test_both_share_registry()
    _test_web_bytes_cap_reduced()
    print("\nall dual-model smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
