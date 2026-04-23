"""Memory skill — semantic recall over prior conversation turns.

Wraps `MemoryTools.recall` with a scope, schema, and prompt fragment. The
loaded-history window (last ~20 messages) covers recent context; this skill
covers the long tail — "what did we decide about X last month?", "did I
mention Y before?", etc.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.memory import DEFAULT_LIMIT, MAX_LIMIT, MemoryTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "memory"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Conversation memory

The last ~20 messages are already in your message history. For anything
older, use `recall(query, limit?)` to semantic-search every past turn
jaekeun and you have exchanged.

When to call it:
- jaekeun references past context ("지난번에 그거…", "내가 전에 말한 거…",
  "what did we decide about…") and it's not in the visible history.
- You're about to answer a question where prior context would change the
  answer (plans, decisions, recurring preferences) and you're not sure
  whether it's been discussed.
- jaekeun asks directly — "예전에 무슨 얘기 했지?".

When NOT to call it:
- The topic is clearly in the last few messages. Reading them is free.
- The question has no prior-conversation dependency (weather, current
  calendar, general facts).
- You've already called `recall` this turn. One call per turn is plenty.

The tool returns up to 10 prior turns, formatted as dated blocks. Each match
includes the jaekeun→yunam exchange so you get full context, not just one
side. Results are ordered by relevance (closest semantic match first).

Treat the recalled text as evidence, not as a script — cite what's relevant,
paraphrase, don't quote verbatim unless jaekeun asks.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "recall": {
        "name": "recall",
        "description": (
            "Semantic search over every past conversation turn. Returns up "
            "to N closest matches, each as a 'jaekeun: ... / yunam: ...' "
            "block with the date and distance. Use when prior context "
            "matters and isn't in the visible history; skip when the topic "
            "is clearly recent or has no history dependency."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to search for. A natural-language phrase works "
                        "best — write it as if describing the topic to a "
                        "search engine, not as the raw question."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max matches to return (1-{MAX_LIMIT}). "
                        f"Default {DEFAULT_LIMIT}. More than 5 is rarely useful."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


def build_memory_skill(tools: MemoryTools) -> Skill:
    async def _recall(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        limit = inputs.get("limit")
        if limit is None:
            limit = DEFAULT_LIMIT
        return await tools.recall(
            chat_id=ctx.chat_id,
            query=inputs.get("query", ""),
            limit=int(limit),
        )

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("recall", Scope.MEMORY_READ, _SCHEMAS["recall"], _recall),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
