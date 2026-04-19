"""LangGraph orchestrator wrapping the Claude + tool loop.

Graph shape: START -> load_history -> agent_step -> persist -> END.
The tool loop lives inside `agent_step` — splitting it into separate LangGraph
nodes buys nothing for Phase 1 and complicates prompt-caching audits.

See /Users/nowgeun/.claude/plans/velvet-swimming-koala.md for the full design.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from .prompts import SYSTEM_PROMPT
from .sessions import SessionStore, ToolCall
from .tools.obsidian import TOOL_SCHEMAS, ObsidianTools
from .tools.vault import VaultError

logger = logging.getLogger("yunam.orchestrator")

MODEL = "claude-opus-4-7"
MAX_TOKENS = 16000
MAX_ITERATIONS = 10
RESULT_PREVIEW_CHARS = 500


class AgentState(TypedDict):
    chat_id: int
    user_text: str
    history: list[dict[str, Any]]
    response_text: str
    tool_calls: list[ToolCall]


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
        tools: ObsidianTools,
    ):
        self._claude = claude_client
        self._store = store
        self._tools = tools
        self._graph = self._build_graph()

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

    async def handle_turn(self, chat_id: int, user_text: str) -> str:
        initial: AgentState = {
            "chat_id": chat_id,
            "user_text": user_text,
            "history": [],
            "response_text": "",
            "tool_calls": [],
        }
        final = await self._graph.ainvoke(initial)
        return final["response_text"]

    # ---- nodes -------------------------------------------------------------

    async def _load_history_node(self, state: AgentState) -> dict[str, Any]:
        history = await self._store.load_history(state["chat_id"])
        return {"history": history}

    async def _agent_step_node(self, state: AgentState) -> dict[str, Any]:
        messages: list[dict[str, Any]] = list(state["history"])
        messages.append({"role": "user", "content": state["user_text"]})

        tool_calls_log: list[ToolCall] = []
        final_response: Any = None

        for iteration in range(MAX_ITERATIONS):
            response = await self._claude.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
            final_response = response

            usage = getattr(response, "usage", None)
            if usage is not None:
                logger.info(
                    "claude turn=%d stop=%s in=%s out=%s cache_read=%s cache_create=%s",
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
                try:
                    result = await self._tools.dispatch(name, inputs)
                except VaultError as e:
                    result = f"Tool error: {e}"
                    is_error = True
                except TypeError as e:
                    # bad arguments from model
                    result = f"Tool error: {e}"
                    is_error = True
                except Exception:
                    logger.exception("tool %s raised", name)
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
        return {"response_text": response_text, "tool_calls": tool_calls_log}

    async def _persist_node(self, state: AgentState) -> dict[str, Any]:
        await self._store.persist_turn(
            chat_id=state["chat_id"],
            user_text=state["user_text"],
            assistant_text=state["response_text"],
            tool_calls=state["tool_calls"],
        )
        return {}
