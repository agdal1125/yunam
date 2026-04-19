#!/usr/bin/env python3
"""Local REPL for Yunam dev.

Runs the full orchestrator (LangGraph + SQLite + Obsidian tools) with an
optional fake Claude client so you can exercise the wiring without burning
Anthropic tokens.

Usage (from repo root):

    # No token burn — fake Claude with scripted scenarios.
    PYTHONPATH=gateway python scripts/repl.py

    # Real Anthropic. Requires ANTHROPIC_API_KEY env var.
    PYTHONPATH=gateway python scripts/repl.py --real

The fake client recognizes a few trigger strings in your input:
  - "write"  → fake Claude calls vault_write(...)
  - "read"   → fake Claude calls vault_read(...)
  - "escape" → fake Claude tries to vault_read a path that escapes the vault (tests sandbox)
  - anything else → fake Claude replies with a single text block, no tools

Environment (both modes):
  - YUNAM_VAULT_PATH: defaults to ./dev-vault (created if missing)
  - YUNAM_DB_PATH:    defaults to ./dev-yunam.db
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------- fake Claude client ---------------------------------------------


@dataclass
class _FakeBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self, exclude_none: bool = True):
        d = {"type": self.type}
        for k in ("text", "id", "name", "input"):
            v = getattr(self, k)
            if v is not None or not exclude_none:
                d[k] = v
        return d


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]
    stop_reason: str
    usage: _FakeUsage = field(default_factory=_FakeUsage)


class FakeMessagesClient:
    """Emulates `anthropic.AsyncAnthropic().messages` for local dev."""

    def __init__(self):
        self._turn = 0

    async def create(self, *, messages, **kwargs) -> _FakeResponse:
        last = messages[-1]

        # If last message is a tool_result, we're mid-loop — respond with final text.
        if last["role"] == "user" and isinstance(last["content"], list):
            return _FakeResponse(
                content=[_FakeBlock(type="text", text="(fake) tool run complete.")],
                stop_reason="end_turn",
                usage=_FakeUsage(input_tokens=100, output_tokens=20),
            )

        user_text = last["content"] if isinstance(last["content"], str) else ""
        lowered = user_text.lower()

        if "escape" in lowered:
            # Tool call that should fail the path-safety check.
            return _FakeResponse(
                content=[
                    _FakeBlock(type="text", text="(fake) trying to read outside vault…"),
                    _FakeBlock(
                        type="tool_use",
                        id=f"toolu_{uuid.uuid4().hex[:12]}",
                        name="vault_read",
                        input={"path": "../../etc/passwd"},
                    ),
                ],
                stop_reason="tool_use",
                usage=_FakeUsage(input_tokens=80, output_tokens=15),
            )

        if "write" in lowered:
            return _FakeResponse(
                content=[
                    _FakeBlock(type="text", text="(fake) saving note…"),
                    _FakeBlock(
                        type="tool_use",
                        id=f"toolu_{uuid.uuid4().hex[:12]}",
                        name="vault_write",
                        input={
                            "path": "repl-test.md",
                            "content": f"# REPL test\n\nUser said: {user_text}\n",
                            "mode": "overwrite",
                        },
                    ),
                ],
                stop_reason="tool_use",
                usage=_FakeUsage(input_tokens=80, output_tokens=15),
            )

        if "read" in lowered:
            return _FakeResponse(
                content=[
                    _FakeBlock(type="text", text="(fake) looking it up…"),
                    _FakeBlock(
                        type="tool_use",
                        id=f"toolu_{uuid.uuid4().hex[:12]}",
                        name="vault_read",
                        input={"path": "repl-test.md"},
                    ),
                ],
                stop_reason="tool_use",
                usage=_FakeUsage(input_tokens=80, output_tokens=15),
            )

        # Plain text response, no tools.
        self._turn += 1
        return _FakeResponse(
            content=[_FakeBlock(type="text", text=f"(fake) echo: {user_text}")],
            stop_reason="end_turn",
            usage=_FakeUsage(
                input_tokens=50,
                output_tokens=10,
                # Simulate a cache hit on turn 2+ so the test can verify the log line.
                cache_read_input_tokens=30 if self._turn > 1 else 0,
                cache_creation_input_tokens=30 if self._turn == 1 else 0,
            ),
        )


class FakeClaude:
    def __init__(self):
        self.messages = FakeMessagesClient()


# ---------- main ------------------------------------------------------------


async def _run(real: bool, chat_id: int) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))
    # Imports must happen after sys.path munging.
    from yunam.config import configure_logging  # noqa: E402
    from yunam.orchestrator import Orchestrator  # noqa: E402
    from yunam.sessions import SessionStore  # noqa: E402
    from yunam.tools.obsidian import ObsidianTools  # noqa: E402

    configure_logging()

    vault = Path(os.environ.get("YUNAM_VAULT_PATH", "./dev-vault")).resolve()
    db_path = Path(os.environ.get("YUNAM_DB_PATH", "./dev-yunam.db")).resolve()
    vault.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if real:
        import anthropic

        if "ANTHROPIC_API_KEY" not in os.environ:
            print("ANTHROPIC_API_KEY not set — cannot use --real", file=sys.stderr)
            sys.exit(2)
        client: Any = anthropic.AsyncAnthropic()
    else:
        client = FakeClaude()

    store = await SessionStore.open(db_path)
    tools = ObsidianTools(vault)
    orch = Orchestrator(client, store, tools)

    print(f"Yunam REPL — vault={vault}  db={db_path}  mode={'real' if real else 'fake'}")
    print("Type a message (Ctrl-D or 'quit' to exit).\n")

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            try:
                reply = await orch.handle_turn(chat_id, line)
            except Exception as e:  # surface for dev; don't crash the REPL
                print(f"[orchestrator error] {e!r}")
                continue
            print(f"< {reply}\n")
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use real Anthropic API (requires ANTHROPIC_API_KEY).",
    )
    parser.add_argument("--chat-id", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(_run(real=args.real, chat_id=args.chat_id))


if __name__ == "__main__":
    main()
