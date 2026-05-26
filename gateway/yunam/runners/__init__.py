"""Background runners for Yunam.

This package owns work that happens *outside* the live agent turn — fetching
curated news on a tick, building the 21:00 newsletter, pushing urgent items
proactively to Telegram. Runners do not extend the SkillRegistry; they read
from CurationStore and call PTBSender directly. The agent's tool surface only
exposes read-only / admin views into the same data.

Public entry points:
  - `run_curation_loop(...)` — the hourly tick + newsletter cron, started by
    main.py when `YUNAM_CURATION_ENABLED=true`.
"""

from .curator import run_curation_loop

__all__ = ["run_curation_loop"]
