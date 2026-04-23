"""Reminders skill — wraps `ReminderTools` with scopes, schemas, and a prompt fragment.

Lets the agent schedule future proactive messages ("nudges") that the sweeper
dispatches when due. Useful pattern: jaekeun mentions a plan mid-conversation,
agent schedules a follow-up so nothing falls through the cracks.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.reminders import ReminderTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "reminders"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Reminders / follow-up nudges

You can schedule proactive reminders that will be sent to jaekeun at a
future time. Use this tool in TWO situations, and only these two:

### 1. jaekeun explicitly asks

When jaekeun says "알려줘", "알람 맞춰줘", "리마인더 걸어줘", "remind me ...",
or any direct request to be reminded — always schedule. If the time is
ambiguous, ask one short clarifying question, then schedule.

### 2. High-stakes plans with external consequences

When jaekeun mentions a plan that carries real consequences if missed —
medical, financial, legal, or a deadline imposed by someone else —
schedule without asking. The test is: "would jaekeun be genuinely annoyed
at himself for forgetting this?" If you're not confident the answer is
yes, do NOT schedule.

Auto-schedule (high-stakes examples):
- "내일 병원 예약 있어" — medical, external appointment.
- "다음 주까지 세금 납부해야 해" — legal deadline, real consequences.
- "금요일 계약서 서명하러 가야 돼" — legal, external commitment.
- "월세 25일이야" — financial obligation with late-fee penalty.
- "비행기 목요일 오전 7시야" — travel, non-refundable, external.

Do NOT auto-schedule (casual, low-stakes, vague, or recurring):
- "내일 이발하러 가려고" — casual personal plan, no external deadline.
- "이번 주에 운동 시작할까 생각 중" — intention, not commitment.
- "나중에 그 책 읽어야지" — open-ended, no time.
- "어제 ~했어" — past.
- Daily habits (wake-up, exercise, meals) — recurrence belongs in the
  calendar; this tool is one-shot.

**When in doubt, don't schedule.** Jaekeun can always ask explicitly if he
wants a reminder. Over-scheduling is more annoying than under-scheduling.

### Using the tool

`schedule_reminder(fire_at, message)`:
- `fire_at` is an absolute local time `"YYYY-MM-DD HH:MM"`. Use the
  `[meta: now is ...]` tag as your anchor for "tomorrow", "next week" —
  resolve relative words into an absolute date yourself, don't pass them
  through.
- `message` is what jaekeun will see. Write it conversationally, in his
  voice — e.g. "병원 갈 시간이야, 준비됐어?" not "reminder that jaekeun had
  a doctor's appointment."

Also available: `list_reminders()` to see what's pending, and
`cancel_reminder(nudge_id)` to remove one.

When you schedule a reminder, briefly confirm: "알람 맞췄어 — 내일 아침 9시에
알려줄게." One line, no markdown. When you decide NOT to schedule (because
it's casual/low-stakes), don't mention the tool at all — just reply
normally. Silence beats noise.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "schedule_reminder": {
        "name": "schedule_reminder",
        "description": (
            "Schedule a proactive reminder message that Yunam will send to "
            "jaekeun at the specified future local time. Use when jaekeun "
            "mentions a plan or task worth following up on. Do NOT send "
            "relative times — resolve them against the [meta: now is ...] "
            "tag first. Past times are rejected. One-shot; no recurrence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fire_at": {
                    "type": "string",
                    "description": (
                        "Absolute local time to fire the reminder, as "
                        "'YYYY-MM-DD HH:MM' (local tz from [meta] tag) or "
                        "ISO 8601 with offset. At least 1 minute in the "
                        "future; at most 1 year out."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Text jaekeun will see when the reminder fires. "
                        "Written in his voice, conversational, no markdown. "
                        "Max 1000 chars."
                    ),
                },
            },
            "required": ["fire_at", "message"],
        },
    },
    "list_reminders": {
        "name": "list_reminders",
        "description": (
            "List pending (not-yet-fired, not-cancelled) reminders for this "
            "chat. Returns one line per reminder with its id, local fire time, "
            "and message preview. Use when jaekeun asks what's scheduled."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    "cancel_reminder": {
        "name": "cancel_reminder",
        "description": (
            "Cancel a pending reminder by id. Use after `list_reminders` "
            "identifies which one jaekeun wants cancelled. No-op if the id "
            "doesn't exist, already fired, or was already cancelled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nudge_id": {
                    "type": "integer",
                    "description": "The reminder id from `list_reminders`.",
                },
            },
            "required": ["nudge_id"],
        },
    },
}


def build_reminders_skill(tools: ReminderTools) -> Skill:
    async def _schedule(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.schedule_reminder(
            chat_id=ctx.chat_id,
            fire_at=inputs.get("fire_at", ""),
            message=inputs.get("message", ""),
        )

    async def _list(_inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.list_reminders(chat_id=ctx.chat_id)

    async def _cancel(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.cancel_reminder(
            chat_id=ctx.chat_id,
            nudge_id=int(inputs.get("nudge_id", 0)),
        )

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("schedule_reminder", Scope.REMINDER_SCHEDULE, _SCHEMAS["schedule_reminder"], _schedule),
        ToolSpec("list_reminders", Scope.REMINDER_SCHEDULE, _SCHEMAS["list_reminders"], _list),
        ToolSpec("cancel_reminder", Scope.REMINDER_SCHEDULE, _SCHEMAS["cancel_reminder"], _cancel),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
