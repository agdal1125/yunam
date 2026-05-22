<<<<<<< HEAD
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
=======
"""Stock-Agent MCP adapter (SSE transport).

Wraps the sibling `stock-mcp` container (FastMCP / SSE) as a Yunam Skill. The
MCP server itself is implemented in the separate `stock-agent` repo; this
module discovers its tools at startup and exposes them through the standard
SkillRegistry contract so the orchestrator treats them identically to
in-process skills.

Design parallels `mcp/gcal.py`:
- Tool discovery at `connect()` time, cached thereafter (no per-turn list_tools).
- Tools are sorted by name before being flattened into ToolSpecs so the
  prompt-cache prefix stays byte-stable across restarts.
- Every tool gets a single explicit `Scope.STOCK_SUPPLY_READ` — finance
  data is read-only; policy decision lives here, not inferred from the model.
- `build_stock_mcp_skill(client)` returns a frozen `Skill` dataclass; the
  earlier class-based StockSkill (which inherited from `Skill` and exposed
  `specs()` / `dispatch()`) does not match the current registry contract and
  is gone.

Failures inside `call_tool` raise `VaultError` so the orchestrator surfaces
them as clean tool errors to Claude (rather than 500-ing the turn).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

from ..capabilities import Scope
from ..skills.base import DispatchContext, Skill, ToolSpec
from ..tools.vault import VaultError

logger = logging.getLogger(__name__)

SKILL_ID = "stock"
SKILL_VERSION = "2"

>>>>>>> c68d3c9 (Seychelles commit job)

SYSTEM_PROMPT_FRAGMENT = """\
## Stock Agent (수급 분석)

<<<<<<< HEAD
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
=======
You have access to a Stock Agent MCP that performs supply/demand analysis of
Korean equities — institutional and pension-fund net buying by ticker and
sector. Use `analyze_supply` for the latest snapshot and
`get_historical_supply(date=YYYYMMDD)` for a specific past day.

When jaekeun asks about institutional flows, KOSPI/KOSDAQ rotation, or "어제
수급 좋았던 종목" style questions, reach for these tools before falling
back to web search. Cite the date the data covers in your reply.
"""


class StockMCPClient:
    """Thin async wrapper around the FastMCP SSE endpoint.

    Lifecycle:
      1. construct with the SSE URL
      2. `await connect()` once at startup — caches the tool list
      3. `await call_tool(name, args)` per turn
      4. `await close()` at shutdown
    """

    def __init__(self, url: str):
        self._url = url
        self._sse_ctx: Any = None
        self._session: ClientSession | None = None
>>>>>>> c68d3c9 (Seychelles commit job)
        self._tools: tuple[dict[str, Any], ...] = ()

    async def connect(self) -> None:
        if self._session is not None:
            raise RuntimeError("StockMCPClient already connected")
<<<<<<< HEAD
        
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
            
=======

        self._sse_ctx = sse_client(self._url)
        streams = await self._sse_ctx.__aenter__()

        self._session = ClientSession(streams[0], streams[1])
        await self._session.__aenter__()
        await self._session.initialize()

        listed = await self._session.list_tools()
        raw_tools: list[dict[str, Any]] = []
        for tool in listed.tools:
            raw_tools.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema
                    or {"type": "object", "properties": {}},
                }
            )

        # Sort by name for prompt-cache stability — flattened ToolSpec order
        # must be identical across gateway restarts.
>>>>>>> c68d3c9 (Seychelles commit job)
        self._tools = tuple(sorted(raw_tools, key=lambda t: t.get("name", "")))
        logger.info(
            "stock MCP connected url=%s tools=%d (%s)",
            self._url,
            len(self._tools),
<<<<<<< HEAD
            ", ".join(t.get("name", "?") for t in self._tools)
=======
            ", ".join(t.get("name", "?") for t in self._tools),
>>>>>>> c68d3c9 (Seychelles commit job)
        )

    async def close(self) -> None:
        if self._session is not None:
<<<<<<< HEAD
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
=======
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                logger.exception("stock MCP session close raised")
            finally:
                self._session = None
        if self._sse_ctx is not None:
            try:
                await self._sse_ctx.__aexit__(None, None, None)
            except Exception:
                logger.exception("stock MCP sse close raised")
            finally:
                self._sse_ctx = None
        self._tools = ()

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise VaultError("stock MCP client is not connected")
        try:
            result = await self._session.call_tool(name, arguments=arguments)
        except McpError as e:
            logger.info("stock call_tool %s mcp-error: %s", name, e)
            raise VaultError(f"stock MCP error ({name}): {e}") from e
        except Exception as e:
            logger.info("stock call_tool %s exception: %r", name, e)
            raise VaultError(f"stock MCP error ({name}): {e}") from e

        # `result.content` is a list of content blocks (text / image / resource).
        # We collapse to a single string for the tool_result block.
        content = getattr(result, "content", None)
        if not content:
            return ""
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
                continue
            # Non-text block — fall back to its dict representation.
            if hasattr(block, "model_dump"):
                parts.append(str(block.model_dump()))
            else:
                parts.append(str(block))
        return "\n".join(parts).strip()


def build_stock_mcp_skill(client: StockMCPClient) -> Skill:
    """Wrap a connected `StockMCPClient` as a Skill.

    Must be called after `await client.connect()` — we read `client.tools`
    synchronously here to build the ToolSpec tuple.
    """
    if not client.tools:
        raise RuntimeError(
            "build_stock_mcp_skill called before connect(); no tools discovered"
        )

    specs: list[ToolSpec] = []
    for mcp_tool in client.tools:
        name = mcp_tool.get("name", "")
        if not name:
            logger.warning("stock MCP tool missing name, skipping: %r", mcp_tool)
            continue

        schema = {
            "name": name,
            "description": mcp_tool.get("description") or "",
            "input_schema": mcp_tool.get("inputSchema")
            or {"type": "object", "properties": {}},
        }

        def _make_handler(tool_name: str):
            async def handler(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
                return await client.call_tool(tool_name, inputs)

            return handler

        specs.append(
            ToolSpec(
                name=name,
                scope=Scope.STOCK_SUPPLY_READ,
                schema=schema,
                handler=_make_handler(name),
            )
        )

    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=tuple(specs),
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
>>>>>>> c68d3c9 (Seychelles commit job)
