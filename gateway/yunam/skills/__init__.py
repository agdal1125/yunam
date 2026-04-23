"""Skills package — governance layer over tool bundles.

Import `SkillRegistry` from here and construct it with an ordered list of
skills. Don't reach into `base` or the per-skill modules from outside tests.
"""

from .airquality import build_airquality_skill
from .base import DispatchContext, Skill, SkillRegistry, ToolHandler, ToolSpec
from .files import build_files_skill
from .memory import build_memory_skill
from .obsidian import build_obsidian_skill
from .obsidian_graph import build_obsidian_graph_skill
from .parcel import build_parcel_skill
from .reminders import build_reminders_skill
from .web import build_web_skill

__all__ = [
    "DispatchContext",
    "Skill",
    "SkillRegistry",
    "ToolHandler",
    "ToolSpec",
    "build_airquality_skill",
    "build_files_skill",
    "build_memory_skill",
    "build_obsidian_skill",
    "build_obsidian_graph_skill",
    "build_parcel_skill",
    "build_reminders_skill",
    "build_web_skill",
]
