"""Usage tracking — record every paid external call to SQLite for cost reports.

The package exposes a single `UsageRecorder` plus a thin ContextVar shim that
ties each record to the chat_id/skill_id active when the call happened. Rates
are isolated in `rates.py` so the next price-change edit is local.
"""

from .rates import (
    anthropic_cost_micro,
    rest_cost_micro,
    voyage_cost_micro,
)
from .recorder import (
    UsageRecorder,
    current_chat_id,
    current_skill_id,
    reset_skill_context,
    set_skill_context,
    set_turn_context,
)

__all__ = [
    "UsageRecorder",
    "anthropic_cost_micro",
    "rest_cost_micro",
    "voyage_cost_micro",
    "current_chat_id",
    "current_skill_id",
    "reset_skill_context",
    "set_skill_context",
    "set_turn_context",
]
