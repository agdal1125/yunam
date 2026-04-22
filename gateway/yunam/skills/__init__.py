"""Skills package — governance layer over tool bundles.

Import `SkillRegistry` from here and construct it with an ordered list of
skills. Don't reach into `base` or the per-skill modules from outside tests.
"""

from .base import DispatchContext, Skill, SkillRegistry, ToolHandler, ToolSpec
from .files import build_files_skill
from .obsidian import build_obsidian_skill
from .web import build_web_skill

__all__ = [
    "DispatchContext",
    "Skill",
    "SkillRegistry",
    "ToolHandler",
    "ToolSpec",
    "build_files_skill",
    "build_obsidian_skill",
    "build_web_skill",
]
