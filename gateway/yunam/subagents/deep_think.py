"""Deep-think sub-agent — Opus 4.7 with adaptive thinking at high effort.

Invoked only via the Telegram `/think <query>` command. Main daily chat runs
on the cheaper Sonnet path; `/think` opts in per-turn to a more expensive,
more thorough model for problems that actually warrant it.

Structurally this is a second `Orchestrator` instance sharing the same
`SkillRegistry` as the main path — same tools, same vault/web surface, same
session history. Only the model and thinking budget differ. Prompt-cache
prefixes are maintained per-model automatically by Anthropic, so each path
stabilizes its own cache on first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..orchestrator import Orchestrator
from ..sessions import SessionStore
from ..skills.base import SkillRegistry

# Keep these narrow — the whole point of /think being opt-in is to make Opus
# use deliberate. Widening the budget here silently raises cost.
DEEP_THINK_MODEL = "claude-opus-4-7"
DEEP_THINK_MAX_TOKENS = 8000


def build_deep_think_orchestrator(
    claude_client,
    store: SessionStore,
    registry: SkillRegistry,
    timezone: str = "Asia/Seoul",
    *,
    vault_path: Path | None = None,
    embedder: Any | None = None,
) -> Orchestrator:
    """Build a deep-think Orchestrator (Opus 4.7 + adaptive thinking / high effort).

    Reuses the same registry as the main orchestrator so the tool surface is
    identical. Conversation history is shared via the common `SessionStore`,
    so `/think` turns appear in the same message log as regular turns.
    """
    return Orchestrator(
        claude_client,
        store,
        registry,
        timezone=timezone,
        vault_path=vault_path,
        embedder=embedder,
        model=DEEP_THINK_MODEL,
        max_tokens=DEEP_THINK_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
    )
