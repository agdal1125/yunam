"""LangGraph orchestrator wrapping the Claude + tool loop.

Graph shape: START -> load_history -> agent_step -> persist -> END.
The tool loop lives inside `agent_step` — splitting it into separate LangGraph
nodes buys nothing for Phase 1 and complicates prompt-caching audits.

See /Users/nowgeun/.claude/plans/velvet-swimming-koala.md for the full design.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph

from .config import Principal
from .context_primer import build_preference_context
from .prompts import SYSTEM_PROMPT
from .sessions import SessionStore, ToolCall
from .skills.base import DispatchContext, SkillRegistry
from .tools.vault import VaultError


# Substring triggers for the privacy heuristic. Hit on any of these and the
# turn is auto-marked `private:<speaker>` before persistence — first line of
# defense for "이건 비밀이야" without forcing the model to call the tool.
# False positives (e.g. "그건 비밀이 아니야") just over-protect, which is the
# safer failure mode for multi-principal chats.
_PRIVACY_TRIGGERS: tuple[str, ...] = (
    "비밀이야", "비밀이지", "비밀이니", "비밀이라",
    "비밀로 해", "비밀로 하자", "비밀로 가자", "비밀로 둘",
    "비밀로 부탁", "비밀로 알",
    "와이프한테 말하지", "와이프한테는 비밀", "와이프 모르게",
    "유림한테 말하지", "유림이한테 말하지", "유림 모르게",
    "둘만 알", "둘이만 알", "너만 알아", "너만 알고",
    "혼자만 알", "오프 더 레코드", "오프더레코드",
    "off the record", "don't tell", "do not tell",
    "between us", "just between us", "keep this private",
)


def _detect_private_visibility(user_text: str, speaker_user_id: int | None) -> str:
    """Return 'private:<id>' if the heuristic triggers, else 'shared'.

    Conservative: any substring match wins — we'd rather over-mark than leak.
    The model can still call `mark_turn_private` when the heuristic misses;
    the heuristic exists so the common case doesn't depend on the model
    remembering to invoke a tool.
    """
    if speaker_user_id is None:
        return "shared"
    lowered = user_text.lower()
    for trigger in _PRIVACY_TRIGGERS:
        if trigger in user_text or trigger in lowered:
            return f"private:{int(speaker_user_id)}"
    return "shared"


class _TextEmbedder(Protocol):
    async def embed_text_document(self, text: str) -> list[float]: ...  # noqa: E704

logger = logging.getLogger("yunam.orchestrator")

MAX_ITERATIONS = 10
RESULT_PREVIEW_CHARS = 500

# Main-path defaults: Sonnet 4.6 at 4k output, no extended thinking.
# Deep-think mode (subagents/deep_think.py) overrides these to Opus 4.7 with
# adaptive thinking at high effort. Keep these defaults cheap; /think opts in.
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096


class AgentState(TypedDict):
    chat_id: int
    user_text: str
    user_content: list[dict[str, Any]] | None
    history: list[dict[str, Any]]
    response_text: str
    tool_calls: list[ToolCall]
    # v6: multi-principal — speaker identity threaded through every node so
    # _load_history_node can ACL-filter by viewer and _persist_node can write
    # the visibility column. principal=None is reserved for legacy/test paths
    # that don't need ACL filtering.
    principal: Principal | None
    visibility: str


class ClaudeClient(Protocol):
    """Minimal async interface the orchestrator needs.

    `anthropic.AsyncAnthropic` satisfies this. The fake client in scripts/repl.py
    also does — that's the whole point of the Protocol.
    """

    @property
    def messages(self) -> Any: ...  # noqa: E704 — Protocol stub


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert a Claude response ContentBlock to the dict shape expected in messages history.

    We re-send the full assistant turn (including tool_use blocks) back as part of
    the messages list when continuing a tool loop — the API requires that tool_use
    blocks be echoed with matching tool_result blocks in the next user turn.
    """
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    # Fall through for mocks / dicts
    if isinstance(block, dict):
        return block
    raise TypeError(f"cannot serialize block of type {type(block).__name__}")


def _extract_text(content: list[Any]) -> str:
    """Pick the final text block from an assistant response."""
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype == "text":
            text = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else None
            )
            if text:
                return text
    return "(no response)"


class Orchestrator:
    def __init__(
        self,
        claude_client: ClaudeClient,
        store: SessionStore,
        registry: SkillRegistry,
        timezone: str = "Asia/Seoul",
        *,
        vault_path: Path | None = None,
        embedder: _TextEmbedder | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
        principals: tuple[Principal, ...] = (),
    ):
        self._claude = claude_client
        self._store = store
        self._registry = registry
        self._tz = ZoneInfo(timezone)
        self._vault_path = vault_path
        self._embedder = embedder
        self._model = model
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._output_config = output_config
        # principals — frozen at construction so the rendered `[from: <name>]`
        # markers in history are stable for prompt-cache friendliness. Adding
        # a principal requires a gateway restart, which is the same rule we
        # already apply to YUNAM_PRINCIPALS env changes.
        self._principals: tuple[Principal, ...] = tuple(principals)
        self._principals_by_id: dict[int, Principal] = {
            p.user_id: p for p in self._principals
        }
        # Background embed tasks are tracked so the asyncio runtime keeps a
        # strong reference to them (otherwise they may be GC'd mid-flight).
        # The done-callback self-unregisters.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Compose the system prompt and tool-schemas list exactly once, in the
        # registry's declared skill order. Both must be byte-stable across
        # turns for Anthropic's prompt cache to keep hitting.
        self._system_prompt = self._build_system_prompt()
        self._tool_schemas = registry.tool_schemas
        self._graph = self._build_graph()

    def _build_system_prompt(self) -> str:
        fragments = self._registry.system_prompt_fragments
        if not fragments:
            return SYSTEM_PROMPT
        return SYSTEM_PROMPT + "\n\n" + "\n\n".join(fragments)

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("load_history", self._load_history_node)
        builder.add_node("agent_step", self._agent_step_node)
        builder.add_node("persist", self._persist_node)
        builder.add_edge(START, "load_history")
        builder.add_edge("load_history", "agent_step")
        builder.add_edge("agent_step", "persist")
        builder.add_edge("persist", END)
        return builder.compile()

    async def handle_turn(
        self,
        chat_id: int,
        user_text: str,
        *,
        user_content: list[dict[str, Any]] | None = None,
        principal: Principal | None = None,
    ) -> str:
        initial: AgentState = {
            "chat_id": chat_id,
            "user_text": user_text,
            "user_content": user_content,
            "history": [],
            "response_text": "",
            "tool_calls": [],
            "principal": principal,
            "visibility": _detect_private_visibility(
                user_text, principal.user_id if principal else None
            ),
        }
        final = await self._graph.ainvoke(initial)
        return final["response_text"]

    # ---- nodes -------------------------------------------------------------

    async def _load_history_node(self, state: AgentState) -> dict[str, Any]:
        principal = state.get("principal")
        viewer_user_id = principal.user_id if principal else None
        raw = await self._store.load_history(
            state["chat_id"], viewer_user_id=viewer_user_id
        )
        # Render history for Claude:
        #  - assistant messages stay as plain `{role, content}` strings
        #  - user messages get a `[from: <name>]` prefix so the model knows
        #    who said each thing in a multi-principal chat. We resolve names
        #    from the principal allowlist; unknown user_id (legacy / removed
        #    principal) renders as `[from: user-<id>]`. NULL user_id
        #    (pre-v6 backfill or proactive scheduler messages) is treated as
        #    'shared' / unknown — no prefix.
        principals = self._principals_by_id
        rendered: list[dict[str, str]] = []
        for entry in raw:
            role = entry["role"]
            content = entry["content"]
            uid = entry.get("user_id")
            if role == "user" and uid is not None:
                speaker = principals.get(int(uid))
                name = speaker.name if speaker else f"user-{int(uid)}"
                content = f"[from: {name}]\n{content}"
            rendered.append({"role": role, "content": content})
        return {"history": rendered}

    async def _agent_step_node(self, state: AgentState) -> dict[str, Any]:
        messages: list[dict[str, Any]] = list(state["history"])
        # Per-turn context (date, preferences) goes in the user message, never
        # the system prompt — otherwise every edit to a preferences file or
        # change of day would invalidate the cached prefix.
        now_local = datetime.now(self._tz)
        date_tag = now_local.strftime("%Y-%m-%d %H:%M %Z")
        primer = await build_preference_context(state["user_text"], self._vault_path)
        principal = state.get("principal")
        prelude_parts = [f"[meta: now is {date_tag}]"]
        if principal is not None:
            prelude_parts.append(f"[from: {principal.name}]")
        if primer:
            prelude_parts.append(primer)
        prelude = "\n\n".join(prelude_parts)
        wrapped = f"{prelude}\n\n{state['user_text']}"
        if state.get("user_content"):
            turn_content = list(state["user_content"] or [])
            if turn_content and turn_content[0].get("type") == "text":
                turn_content[0] = {
                    **turn_content[0],
                    "text": f"{prelude}\n\n{turn_content[0].get('text', '')}",
                }
            else:
                turn_content.insert(0, {"type": "text", "text": wrapped})
            messages.append({"role": "user", "content": turn_content})
        else:
            messages.append({"role": "user", "content": wrapped})

        # Shared dispatch context for every tool handler invoked this turn.
        # `turn_meta` is mutable scratch — the privacy skill writes
        # `visibility` here and we read it back after the loop. Seeding it
        # with the heuristic-derived visibility ensures the tool's only role
        # is to OVERRIDE the default, not to invent visibility from nothing.
        turn_meta: dict[str, Any] = {
            "visibility": state.get("visibility", "shared"),
            "visibility_source": "heuristic",
        }
        dispatch_ctx = DispatchContext(
            chat_id=state["chat_id"],
            principal_user_id=principal.user_id if principal else None,
            principal_name=principal.name if principal else None,
            turn_meta=turn_meta,
        )

        tool_calls_log: list[ToolCall] = []
        final_response: Any = None

        for iteration in range(MAX_ITERATIONS):
            create_kwargs: dict[str, Any] = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": [
                    {
                        "type": "text",
                        "text": self._system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "tools": self._tool_schemas,
                "messages": messages,
            }
            # Extended thinking / effort are only sent when this Orchestrator
            # was constructed with them — keeps the Sonnet main path from
            # emitting Opus-only parameters the API would reject.
            if self._thinking is not None:
                create_kwargs["thinking"] = self._thinking
            if self._output_config is not None:
                create_kwargs["output_config"] = self._output_config
            response = await self._claude.messages.create(**create_kwargs)
            final_response = response

            usage = getattr(response, "usage", None)
            if usage is not None:
                logger.info(
                    "claude model=%s turn=%d stop=%s in=%s out=%s cache_read=%s cache_create=%s",
                    self._model,
                    iteration,
                    getattr(response, "stop_reason", "?"),
                    getattr(usage, "input_tokens", "?"),
                    getattr(usage, "output_tokens", "?"),
                    getattr(usage, "cache_read_input_tokens", "?"),
                    getattr(usage, "cache_creation_input_tokens", "?"),
                )

            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "end_turn":
                break
            if stop_reason == "pause_turn":
                # Server-side tool hit its own iteration cap — re-send as-is.
                messages.append(
                    {
                        "role": "assistant",
                        "content": [_block_to_dict(b) for b in response.content],
                    }
                )
                continue
            if stop_reason != "tool_use":
                logger.warning("unexpected stop_reason=%r; treating as end_turn", stop_reason)
                break

            # stop_reason == "tool_use": echo assistant turn, then execute tools.
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in response.content],
                }
            )

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                name = block.name
                inputs = block.input or {}
                t0 = time.monotonic()
                is_error = False
                # Capture skill_id/scope before invoking so an exception inside
                # the handler still produces a complete audit row.
                skill_id: str | None = None
                scope: str | None = None
                try:
                    skill, tool_spec = self._registry.lookup(name)
                    skill_id = skill.id
                    scope = str(tool_spec.scope)
                    handler_inputs = inputs if isinstance(inputs, dict) else {}
                    result = await tool_spec.handler(
                        handler_inputs,
                        dispatch_ctx,
                    )
                except VaultError as e:
                    result = f"Tool error: {e}"
                    is_error = True
                except TypeError as e:
                    # bad arguments from model
                    result = f"Tool error: {e}"
                    is_error = True
                except Exception:
                    logger.exception("tool %s raised (skill=%s)", name, skill_id)
                    result = "Tool error: internal failure"
                    is_error = True
                elapsed_ms = int((time.monotonic() - t0) * 1000)

                tool_calls_log.append(
                    ToolCall(
                        name=name,
                        input=inputs if isinstance(inputs, dict) else {"raw": str(inputs)},
                        result_preview=(result or "")[:RESULT_PREVIEW_CHARS],
                        is_error=is_error,
                        elapsed_ms=elapsed_ms,
                        skill_id=skill_id,
                        scope=scope,
                        principal_user_id=(
                            principal.user_id if principal else None
                        ),
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": is_error,
                    }
                )

            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning(
                "tool loop hit MAX_ITERATIONS=%d for chat_id=%s",
                MAX_ITERATIONS,
                state["chat_id"],
            )

        response_text = (
            _extract_text(final_response.content) if final_response else "(no response)"
        )
        # turn_meta['visibility'] may have been overridden by the privacy
        # skill mid-loop; read it back here so _persist_node sees the final
        # value. The default ('shared') is what the heuristic seeded, so
        # missing key would still be safe — explicit anyway.
        final_visibility = turn_meta.get("visibility", state.get("visibility", "shared"))
        return {
            "response_text": response_text,
            "tool_calls": tool_calls_log,
            "visibility": final_visibility,
        }

    async def _persist_node(self, state: AgentState) -> dict[str, Any]:
        principal = state.get("principal")
        user_msg_id, assistant_msg_id = await self._store.persist_turn(
            chat_id=state["chat_id"],
            user_text=state["user_text"],
            assistant_text=state["response_text"],
            tool_calls=state["tool_calls"],
            principal_user_id=principal.user_id if principal else None,
            visibility=state.get("visibility", "shared"),
        )
        # Fire-and-forget embed of the combined turn. Keeps Telegram reply
        # latency unchanged; on failure the turn exists in DB but isn't
        # searchable by `recall` (logged at warning).
        if self._embedder is not None:
            task = asyncio.create_task(
                self._embed_turn_bg(
                    assistant_msg_id=assistant_msg_id,
                    user_text=state["user_text"],
                    assistant_text=state["response_text"],
                ),
                name=f"yunam-embed-{assistant_msg_id}",
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        return {}

    async def _embed_turn_bg(
        self,
        *,
        assistant_msg_id: int,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Embed a just-persisted turn. Any failure is logged and swallowed."""
        try:
            combined = f"[user] {user_text}\n\n[assistant] {assistant_text}"
            vector = await self._embedder.embed_text_document(combined)  # type: ignore[union-attr]
            await self._store.record_message_embedding(assistant_msg_id, vector)
        except Exception:
            logger.warning(
                "background embed failed for assistant_msg_id=%s",
                assistant_msg_id, exc_info=True,
            )
