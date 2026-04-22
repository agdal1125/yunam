"""Google Calendar skill via nspady/google-calendar-mcp (streamable-http).

Runs as a sibling Docker service (`calendar-mcp` in docker-compose). Yunam
connects over the internal Docker network at http://calendar-mcp:3000/mcp.
No inbound port is exposed to the host or internet.

Key design choices:

1. **Sorted tool discovery**. MCP `list_tools()` returns tools in insertion
   order, which can shift across server versions. We sort by name before
   flattening into ToolSpecs so the prompt-cache prefix stays byte-stable
   across gateway restarts — a cache-flushing invariant from CLAUDE.md.

2. **Optional skill**. The MCP URL comes from env; if unset, `main.py`
   skips this skill entirely. If set but unreachable at boot, we fail fast
   so misconfiguration is loud, not silent (per the MCP checklist).

3. **Scope assignment is policy, not inference**. We explicitly classify
   each nspady tool name into `calendar:read` vs `calendar:write` via a
   prefix table. Unknown tool names default to read (safer than guessing
   write) but log a warning for human review.

4. **Token ownership**. Refresh tokens live in the MCP server's own
   Docker volume (`calendar-tokens`), not yunam.db. Accepted trade-off —
   see `dev/milestones.md` M3 discussion.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..capabilities import Scope
from ..skills.base import DispatchContext, Skill, ToolSpec
from ..tools.vault import VaultError

logger = logging.getLogger(__name__)


SKILL_ID = "gcal"
SKILL_VERSION = "1"


# nspady tool-name prefixes that mutate calendar state → need write scope.
# Everything else is read-only.
_WRITE_PREFIXES: tuple[str, ...] = (
    "create-",
    "update-",
    "delete-",
    "respond-",
    "manage-",  # manage-accounts rotates OAuth state → treat as write
)


def _scope_for(tool_name: str) -> Scope:
    if any(tool_name.startswith(p) for p in _WRITE_PREFIXES):
        return Scope.CALENDAR_WRITE
    return Scope.CALENDAR_READ


SYSTEM_PROMPT_FRAGMENT = """\
## Google Calendar (구글 캘린더)

jaekeun's Google Calendar(s) are available through MCP tools named with
`list-/get-/search-/create-/update-/delete-/respond-` prefixes. Reach for
these whenever a request involves schedules, meetings, events, or
availability — never fabricate dates or assume what's on the calendar.

Core flow for "일정 잡자" / "언제 시간 돼?" requests:
1. `list-calendars` if you don't already know which calendar ID to use
   (personal vs work vs shared). Cache nothing across turns — re-list
   only if the user mentions a calendar you don't recognize.
2. `get-freebusy` on the relevant calendar(s) over the candidate window
   to find open slots.
3. Present 2-3 candidate slots in plain Korean. WAIT for jaekeun to pick
   one before calling `create-event` — auto-creating events without
   explicit confirmation is a hard line.

When creating / updating events, default to `Asia/Seoul` time unless
jaekeun specifies otherwise. Include attendees only if explicitly named
(adding attendees sends emails — be careful).

The `respond-to-event` tool accepts invitations. Use only when jaekeun
tells you which event to respond to and with what status.

If an MCP call returns an auth / token error, don't retry blindly —
surface the failure and suggest re-running the one-time OAuth bootstrap.
"""


class GCalMCPClient:
    """Persistent streamable-http MCP client for the nspady calendar server.

    Opens a single session at `connect()` (typically called from main.py
    during gateway startup) and keeps it alive until `close()`. Thread-safe
    within a single asyncio event loop — don't share across loops.
    """

    def __init__(self, url: str):
        self._url = url
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: tuple[Any, ...] = ()

    async def connect(self) -> None:
        """Establish the MCP session and discover tools. Raises on failure.

        Callers decide fail-open vs fail-fast — main.py fails fast, so
        misconfigured gateways crash at boot rather than silently missing
        a skill.
        """
        if self._session is not None:
            raise RuntimeError("GCalMCPClient already connected")
        stack = AsyncExitStack()
        try:
            transport = await stack.enter_async_context(streamablehttp_client(self._url))
            read_stream, write_stream, _ = transport
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            listed = await session.list_tools()
        except Exception:
            await stack.aclose()
            raise
        # Sort by name for prompt-cache prefix stability (CLAUDE.md invariant).
        self._tools = tuple(sorted(listed.tools, key=lambda t: t.name))
        self._session = session
        self._exit_stack = stack
        logger.info(
            "gcal MCP connected url=%s tools=%d (%s)",
            self._url,
            len(self._tools),
            ", ".join(t.name for t in self._tools[:8])
            + ("..." if len(self._tools) > 8 else ""),
        )

    async def close(self) -> None:
        if self._exit_stack is None:
            return
        try:
            await self._exit_stack.aclose()
        except Exception:
            logger.exception("gcal MCP close raised")
        finally:
            self._exit_stack = None
            self._session = None
            self._tools = ()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke an MCP tool and return its content as a string.

        Exceptions are re-raised as VaultError so the orchestrator surfaces
        a clean tool-error message to Claude instead of a 500.
        """
        if self._session is None:
            raise VaultError("gcal MCP client is not connected")
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as e:
            logger.info("gcal call_tool %s failed: %r", name, e)
            raise VaultError(f"gcal MCP error ({name}): {e}") from e
        text = _stringify_content(result.content)
        if getattr(result, "isError", False):
            return f"Tool error: {text}"
        return text

    @property
    def tools(self) -> tuple[Any, ...]:
        return self._tools


def _stringify_content(content: Any) -> str:
    """Flatten MCP content blocks to a single string for tool_result.

    MCP content is usually a list of TextContent objects each with a `text`
    attribute; guard for ImageContent / EmbeddedResource by stringifying.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts).strip()


def build_gcal_mcp_skill(client: GCalMCPClient) -> Skill:
    """Wrap a connected `GCalMCPClient` as a Skill.

    The client must already have called `connect()` — we read `client.tools`
    synchronously here to build the ToolSpec tuple.
    """
    if not client.tools:
        raise RuntimeError(
            "build_gcal_mcp_skill called before connect(); no tools discovered"
        )

    specs: list[ToolSpec] = []
    for mcp_tool in client.tools:
        name = mcp_tool.name
        scope = _scope_for(name)
        if scope == Scope.CALENDAR_READ and any(
            kw in name for kw in ("create", "update", "delete", "respond", "manage")
        ):
            # Defense in depth: substring check in case a tool is named oddly
            # (e.g. 'batch-create-events' without the leading 'create-'). Log
            # so a human can tighten the prefix table if this fires.
            logger.warning(
                "gcal tool %r contains write-ish substring but was classified "
                "as read; review _WRITE_PREFIXES",
                name,
            )

        schema = {
            "name": name,
            "description": mcp_tool.description or "",
            "input_schema": mcp_tool.inputSchema
            or {"type": "object", "properties": {}},
        }

        def _make_handler(tool_name: str):
            async def handler(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
                return await client.call_tool(tool_name, inputs)

            return handler

        specs.append(
            ToolSpec(
                name=name,
                scope=scope,
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
