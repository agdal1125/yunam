"""MCP adapters — each module wraps one external MCP server as a Yunam Skill.

First (and currently only) adapter: Google Calendar via nspady/google-calendar-mcp.
Adapters plug into SkillRegistry exactly like in-process skills — the
orchestrator treats them identically via `lookup(tool_name)`.
"""

from .gcal import GCalMCPClient, build_gcal_mcp_skill

__all__ = ["GCalMCPClient", "build_gcal_mcp_skill"]
