"""MCP adapters — each module wraps one external MCP server as a Yunam Skill.

Adapters plug into SkillRegistry exactly like in-process skills — the
orchestrator treats them identically via `lookup(tool_name)`.

Current adapters:
- gcal: Google Calendar via nspady/google-calendar-mcp (streamable-http)
- stock: Stock-Agent supply/demand analysis (FastMCP / SSE)
"""

from .gcal import GCalMCPClient, build_gcal_mcp_skill
from .stock import StockMCPClient, build_stock_mcp_skill

__all__ = [
    "GCalMCPClient",
    "StockMCPClient",
    "build_gcal_mcp_skill",
    "build_stock_mcp_skill",
]
