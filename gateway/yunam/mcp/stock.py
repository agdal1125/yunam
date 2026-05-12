import logging
from typing import Any
from ..skills.base import DispatchContext, Skill, ToolSpec
from ..capabilities import Scope

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

logger = logging.getLogger(__name__)

SKILL_ID = "stock"
SKILL_VERSION = "1"

SYSTEM_PROMPT_FRAGMENT = """\
## Stock Agent (수급 분석)

You have access to a Stock Agent MCP that can perform supply/demand analysis of Korean stocks (specifically analyzing institutional and pension fund buying).
Use the `analyze_supply` tool to fetch the latest supply/demand data.
Use the `get_historical_supply` tool to fetch past analysis data for a specific date (YYYYMMDD).
When answering questions about the stock market or institutional buying, use these tools to provide data-driven answers.
"""

class StockMCPClient:
    def __init__(self, url: str):
        self._url = url
        self._sse_ctx = None
        self._session = None
        self._tools: tuple[dict[str, Any], ...] = ()

    async def connect(self) -> None:
        if self._session is not None:
            raise RuntimeError("StockMCPClient already connected")
        
        self._sse_ctx = sse_client(self._url)
        streams = await self._sse_ctx.__aenter__()
        
        self._session = ClientSession(streams[0], streams[1])
        await self._session.__aenter__()
        await self._session.initialize()
        
        listed = await self._session.list_tools()
        raw_tools = []
        for tool in listed.tools:
            raw_tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema
            })
            
        self._tools = tuple(sorted(raw_tools, key=lambda t: t.get("name", "")))
        logger.info(
            "stock MCP connected url=%s tools=%d (%s)",
            self._url,
            len(self._tools),
            ", ".join(t.get("name", "?") for t in self._tools)
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._sse_ctx is not None:
            await self._sse_ctx.__aexit__(None, None, None)
            self._sse_ctx = None

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("mcp is not connected")
        try:
            result = await self._session.call_tool(name, arguments=args)
            return [c.model_dump() for c in result.content] if result.content else []
        except McpError as e:
            logger.error("mcp call_tool error: %s", e)
            return f"Error: {e}"
        except Exception as e:
            logger.error("mcp call_tool exception: %s", e)
            return f"Exception: {e}"

class StockSkill(Skill):
    def __init__(self, mcp_client: StockMCPClient):
        self._mcp = mcp_client
        self.scopes = frozenset([Scope.KNOWLEDGE])

    @property
    def id(self) -> str:
        return SKILL_ID

    @property
    def version(self) -> str:
        return SKILL_VERSION

    def system_prompt(self, ctx: DispatchContext) -> str | None:
        return SYSTEM_PROMPT_FRAGMENT

    def specs(self) -> list[ToolSpec]:
        specs = []
        for t in self._mcp._tools:
            specs.append(
                ToolSpec(
                    name=t["name"],
                    description=t.get("description", ""),
                    schema=t.get("inputSchema", {}),
                    scopes=frozenset([Scope.KNOWLEDGE]),
                )
            )
        return specs

    async def dispatch(
        self, name: str, args: dict[str, Any], ctx: DispatchContext
    ) -> Any:
        logger.info("stock mcp call: %s(args=%r)", name, args)
        return await self._mcp.call_tool(name, args)
