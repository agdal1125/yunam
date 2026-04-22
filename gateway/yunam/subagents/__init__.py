"""Sub-agents — separately-configured Claude calls invoked out-of-band.

Unlike skills (tools the main agent can choose to call), sub-agents here are
user-invoked via explicit Telegram commands (e.g. `/think`). Each is a second
Orchestrator instance with its own model / thinking budget / cost profile,
sharing the same SkillRegistry so tools and vault state are identical.
"""

from .deep_think import build_deep_think_orchestrator

__all__ = ["build_deep_think_orchestrator"]
