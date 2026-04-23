"""Per-turn context primer — loads relevant `preferences/*.md` into the user message.

The Obsidian vault's `preferences/` directory is jaekeun's editable rulebook
for agent behavior. Passively relying on the model to call `vault_read` on
its own is unreliable: the model only reads when it decides it's in the right
context, and things like web search or calendar listings don't trigger that
judgment. This module loads the relevant preference files eagerly on every
turn and prepends their contents to the user message, behind a
`[preferences: ...]` tag that mirrors the existing `[meta: now is ...]` tag.

Why not put this in SYSTEM_PROMPT? Because jaekeun edits these files from
the Obsidian app; every edit would bust Anthropic's prompt cache. Per-turn
user-message injection keeps the cached prefix byte-stable while still
guaranteeing the current rules are in context for every reply.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger("yunam.context_primer")

# Always loaded regardless of turn content — small universal-style files.
# Duplicates the style rules already inlined into SYSTEM_PROMPT, but kept
# editable here so jaekeun can refine nuance without a cache invalidation.
# `vault-writing.md` governs `[[wikilink]]` + `#tag` conventions for every
# vault_write, so it has to be in context for *every* turn where the agent
# might decide to save something.
ALWAYS_LOAD: tuple[str, ...] = (
    "preferences/communication-style.md",
    "preferences/yunam-behavior.md",
    "preferences/vault-writing.md",
)

# Domain-specific files loaded only when the user message mentions a matching
# keyword. Case-insensitive substring match. Matching is deliberately loose:
# a false positive costs ~200 bytes of prompt, a false negative costs a missed
# rule — prefer the former.
DOMAIN_TRIGGERS: dict[str, tuple[str, ...]] = {
    "preferences/calendar.md": (
        "calendar", "schedule", "meeting", "event", "appointment", "booking",
        "일정", "캘린더", "약속", "스케줄", "미팅", "회의", "이벤트",
    ),
    "preferences/daily-logging.md": (
        "daily", "retrospective", "journal", "일기", "회고", "하루",
    ),
}

# Combined cap on injected preference text. Realistic usage is under 2 KB;
# this is a guard against a preferences file accidentally growing huge and
# blowing up every turn's prompt.
MAX_PRIMER_BYTES = 8 * 1024


async def build_preference_context(user_text: str, vault_path: Path | None) -> str:
    """Return a `[preferences: ...]` block to prepend to the user message, or ''.

    Never raises — missing files are logged at INFO and skipped. Reads happen
    in a thread so the orchestrator's event loop isn't blocked on vault I/O.
    """
    if vault_path is None or not vault_path.is_dir():
        return ""

    needle = user_text.casefold()
    relpaths: list[str] = list(ALWAYS_LOAD)
    for relpath, keywords in DOMAIN_TRIGGERS.items():
        if any(kw.casefold() in needle for kw in keywords):
            relpaths.append(relpath)

    sections: list[str] = []
    total = 0
    for relpath in relpaths:
        contents = await asyncio.to_thread(_read_or_none, vault_path / relpath)
        if contents is None:
            continue
        section = f"=== {relpath} ===\n{contents.strip()}"
        projected = total + len(section.encode("utf-8")) + 2  # + "\n\n"
        if projected > MAX_PRIMER_BYTES:
            logger.info(
                "primer budget hit at %s; skipping remaining preferences", relpath
            )
            break
        sections.append(section)
        total = projected

    if not sections:
        return ""

    return "[preferences:\n" + "\n\n".join(sections) + "\n]"


def _read_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("failed to read preference file %s: %s", path, e)
        return None
