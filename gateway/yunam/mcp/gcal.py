"""Google Calendar skill via nspady/google-calendar-mcp (streamable-http).

Runs as a sibling Docker service (`calendar-mcp` in docker-compose). Yunam
connects over the internal Docker network at http://calendar-mcp:3000/mcp.
No inbound port is exposed to the host or internet.

Key design choices:

1. **Raw httpx JSON-RPC** instead of the `mcp` Python SDK's ClientSession.
   nspady runs in stateless mode (`sessionIdGenerator: undefined`), which
   closes the SSE response stream after each POST. The SDK's ClientSession
   treats that closure as a broken transport and any subsequent `send_request`
   raises `anyio.BrokenResourceError`. Raw POSTs sidestep the abstraction
   mismatch entirely — every call is an isolated HTTP request.

2. **Tool discovery at startup, cached thereafter**. `connect()` issues
   `initialize` + `tools/list` once and caches the result. Subsequent tool
   invocations are single `tools/call` POSTs — no re-discovery per turn.

3. **Sorted tool order**. We sort the discovered tools by name before flattening
   into ToolSpecs so the prompt-cache prefix stays byte-stable across gateway
   restarts — a cache-flushing invariant from CLAUDE.md.

4. **Scope assignment is policy, not inference**. We explicitly classify each
   nspady tool name into `calendar:read` vs `calendar:write` via a prefix
   table. Unknown tool names default to read (safer than guessing write)
   but log a warning for human review.

5. **Token ownership**. Refresh tokens live in the MCP server's own Docker
   volume (`calendar-tokens`), not yunam.db. Accepted trade-off — see
   `dev/milestones.md` M3 discussion.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from ..capabilities import Scope
from ..skills.base import DispatchContext, Skill, ToolSpec
from ..tools.vault import VaultError

logger = logging.getLogger(__name__)


SKILL_ID = "gcal"
SKILL_VERSION = "1"

# MCP protocol version nspady advertises (as of 2026-04). Send this in
# initialize so server + client agree on the wire format. Bumping this is a
# deliberate policy decision — nspady may not support newer specs yet.
PROTOCOL_VERSION = "2024-11-05"

# Reasonable per-request timeout. Most Calendar MCP calls finish in < 2s;
# a 30s ceiling tolerates slow Google API days without hanging Claude turns.
DEFAULT_TIMEOUT_S = 30.0


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
    """Raw-JSON-RPC client for nspady's streamable-http MCP endpoint.

    Stateless: each MCP method is sent as an independent POST. No SSE
    stream is kept open between calls. Construct once, call `connect()` at
    gateway startup to populate the tool cache, use `call_tool()` per turn.
    """

    def __init__(self, url: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        self._url = url
        self._timeout_s = timeout_s
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._tools: tuple[dict[str, Any], ...] = ()

    async def connect(self) -> None:
        """Initialize the server side and cache its tool list.

        Failing here crashes the gateway at boot (see main.py) — better to
        surface misconfiguration loudly than silently serve without calendar.
        """
        if self._http is not None:
            raise RuntimeError("GCalMCPClient already connected")
        self._http = httpx.AsyncClient(timeout=self._timeout_s)
        try:
            # `initialize` is special: we issue it manually so we can read the
            # `mcp-session-id` response header. The forked nspady (stateful
            # mode) assigns a fresh session id per initialize; subsequent
            # requests on this session must echo it via header.
            init_response = await self._http.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": uuid.uuid4().hex,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "yunam-gateway", "version": "1"},
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            if init_response.status_code >= 400:
                body_preview = init_response.text[:500].replace("\n", " ")
                raise RuntimeError(
                    f"MCP HTTP {init_response.status_code} on initialize: {body_preview}"
                )
            self._session_id = init_response.headers.get("mcp-session-id")
            if not self._session_id:
                logger.warning(
                    "gcal MCP: initialize response has no mcp-session-id header; "
                    "server may not be in stateful mode — tools/list will likely 500"
                )
            # Surface init-level errors embedded in the payload (protocol errors
            # vs transport errors).
            init_payload = _parse_mcp_response(init_response.text)
            if isinstance(init_payload, dict) and "error" in init_payload:
                err = init_payload["error"]
                raise RuntimeError(
                    f"MCP initialize error: code={err.get('code')} "
                    f"msg={err.get('message')!r}"
                )
            # MCP spec: after `initialize` response, the client MUST send the
            # `notifications/initialized` notification before issuing further
            # requests. The `mcp` SDK's ClientSession did this automatically;
            # we replicate it here or nspady's server-side state machine
            # rejects subsequent tools/list / tools/call with HTTP 500.
            await self._notify("notifications/initialized", {})
            listed = await self._rpc("tools/list", {})
        except Exception:
            await self._http.aclose()
            self._http = None
            self._session_id = None
            raise
        raw_tools = listed.get("tools", []) if isinstance(listed, dict) else []
        # Sort by name for prompt-cache prefix stability (CLAUDE.md invariant).
        self._tools = tuple(sorted(raw_tools, key=lambda t: t.get("name", "")))
        logger.info(
            "gcal MCP connected url=%s tools=%d (%s)",
            self._url,
            len(self._tools),
            ", ".join(t.get("name", "?") for t in self._tools[:8])
            + ("..." if len(self._tools) > 8 else ""),
        )

    async def close(self) -> None:
        if self._http is None:
            return
        try:
            await self._http.aclose()
        except Exception:
            logger.exception("gcal MCP http close raised")
        finally:
            self._http = None
            self._session_id = None
            self._tools = ()

    def _request_headers(self) -> dict[str, str]:
        """Shared headers for POSTs to the MCP endpoint.

        Includes `mcp-session-id` when set (stateful mode, which is what our
        forked nspady uses). Omitting the header is fine for the initial
        `initialize` request — the server assigns the id in its response.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke an MCP tool by name. Returns the tool's content as text.

        Raises VaultError on transport/protocol failure so the orchestrator
        surfaces a clean tool-error to Claude (per the governance layer).
        """
        if self._http is None:
            raise VaultError("gcal MCP client is not connected")
        try:
            result = await self._rpc(
                "tools/call",
                {"name": name, "arguments": arguments},
            )
        except Exception as e:
            logger.info("gcal call_tool %s failed: %r", name, e)
            raise VaultError(f"gcal MCP error ({name}): {e}") from e
        if not isinstance(result, dict):
            return str(result)
        text = _stringify_content(result.get("content"))
        if result.get("isError"):
            return f"Tool error: {text}"
        return text

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        return self._tools

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Send one JSON-RPC request and return the `result` field.

        nspady responds with a text/event-stream-shaped body even for a
        single request (one `event: message` frame with `data: {...}`).
        We parse either SSE or plain JSON transparently.
        """
        assert self._http is not None
        request_id = uuid.uuid4().hex
        response = await self._http.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            headers=self._request_headers(),
        )
        if response.status_code >= 400:
            # Surface the server's error body — nspady usually returns a
            # JSON-RPC error payload even on HTTP 500, which is the most
            # useful diagnostic for "why did the server reject this?".
            body_preview = response.text[:500].replace("\n", " ")
            raise RuntimeError(
                f"MCP HTTP {response.status_code} on {method}: {body_preview}"
            )
        payload = _parse_mcp_response(response.text)
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected MCP response shape: {type(payload).__name__}")
        if "error" in payload:
            err = payload["error"]
            raise RuntimeError(
                f"MCP error: code={err.get('code')} msg={err.get('message')!r}"
            )
        return payload.get("result")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response parsing).

        MCP uses notifications for one-way messages like `notifications/initialized`.
        We don't raise on non-2xx here — the server typically returns HTTP 202
        or empty 200, and a weird status on a notification is less critical
        than on a request.
        """
        assert self._http is not None
        response = await self._http.post(
            self._url,
            json={
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            },
            headers=self._request_headers(),
        )
        if response.status_code >= 400:
            body_preview = response.text[:300].replace("\n", " ")
            logger.info(
                "MCP notification %s returned HTTP %d: %s",
                method,
                response.status_code,
                body_preview,
            )


def _parse_mcp_response(body: str) -> Any:
    """Parse either raw JSON or SSE-framed JSON response from nspady.

    SSE frame shape:
        event: message
        data: {"jsonrpc":"2.0","id":...,"result":...}
    """
    body = body.strip()
    if not body:
        raise RuntimeError("empty MCP response body")
    if body.startswith("{") or body.startswith("["):
        return json.loads(body)
    # SSE path: last `data:` line wins (server may send multiple events).
    data_line: str | None = None
    for line in body.splitlines():
        if line.startswith("data:"):
            data_line = line[len("data:"):].strip()
    if data_line is None:
        raise RuntimeError(f"no JSON payload in MCP response: {body[:200]!r}")
    return json.loads(data_line)


def _stringify_content(content: Any) -> str:
    """Flatten MCP content blocks to a single string for tool_result.

    Content is usually a list of `{type: "text", text: "..."}` blocks.
    Non-text blocks (image, embedded resource) get a `str()` fallback.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if text is not None:
                parts.append(str(text))
                continue
            parts.append(json.dumps(block, ensure_ascii=False))
        else:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
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
        name = mcp_tool.get("name", "")
        if not name:
            logger.warning("gcal MCP tool missing name, skipping: %r", mcp_tool)
            continue
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
