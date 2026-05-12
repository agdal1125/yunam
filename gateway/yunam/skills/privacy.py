"""Privacy skill — turn-level visibility marking.

Exposes one tool, `mark_turn_private`, that the model invokes when a
principal explicitly asks for a turn to stay private (e.g. jaekeun says
"이건 비밀이야"). The handler writes the resulting visibility into the
`turn_meta` mutable scratch dict on `DispatchContext`; the orchestrator
reads it back at persist time to set the `messages.visibility` and
`message_turns.visibility` columns.

This is a complement to the orchestrator's keyword heuristic (see
`_detect_private_visibility` in orchestrator.py). The heuristic catches
common phrases automatically; the tool is the explicit path for cases
the heuristic would miss, or for cases where the model decides
mid-conversation that retrospectively the turn should be private.

Scope is `privacy:write` rather than overloading an existing one — the
governance layer guideline says scope assignment is a policy decision,
and "the model can change visibility of a stored turn" is materially
different from any other write the agent does.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "privacy"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Privacy / visibility

When a principal explicitly asks for the current turn to be private —
'비밀이야', '와이프한테 말하지 마', 'don't tell yoolim', 'between us', or
similar — call `mark_turn_private` once during the turn. The tool needs no
arguments. It marks both the user's message and your reply for this turn
as `private:<speaker>` so future history loads / recalls by other
principals filter them out at the database layer.

When NOT to call:
- The principal didn't ask. Do not pre-emptively privatize neutral
  conversation; the default 'shared' is correct.
- A simple keyword heuristic already triggered — calling again is a no-op
  but adds an audit row. Prefer to skip if the heuristic obviously fired
  (the user input contains '비밀', '둘만 알', 'don't tell', etc.).

The tool only affects the CURRENT turn. It cannot retroactively edit
prior persisted turns — those visibility values are immutable once
written. If the principal asks to "make our last conversation private,"
explain that you can only mark from now on.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "mark_turn_private": {
        "name": "mark_turn_private",
        "description": (
            "Mark the current turn (the user's message and your reply) "
            "as private to the speaker. After this call, only the speaker "
            "sees the turn in future history loads or `recall` searches. "
            "Use when the speaker explicitly asks for privacy. Idempotent "
            "within a turn — second call is a no-op."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


def build_privacy_skill() -> Skill:
    async def _mark_turn_private(_inputs: dict[str, Any], ctx: DispatchContext) -> str:
        if ctx.principal_user_id is None:
            # No speaker on a system-driven turn — nothing meaningful to mark.
            # Return a soft message rather than erroring; this should be rare.
            return "no current speaker — visibility unchanged"
        # Idempotent: if already private to this speaker, just confirm.
        target = f"private:{int(ctx.principal_user_id)}"
        existing = ctx.turn_meta.get("visibility")
        if existing == target:
            return "turn already marked private — no change"
        ctx.turn_meta["visibility"] = target
        ctx.turn_meta["visibility_source"] = "tool"
        speaker = ctx.principal_name or str(ctx.principal_user_id)
        return f"turn marked private to {speaker}"

    specs: tuple[ToolSpec, ...] = (
        ToolSpec(
            "mark_turn_private",
            Scope.PRIVACY_WRITE,
            _SCHEMAS["mark_turn_private"],
            _mark_turn_private,
        ),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
