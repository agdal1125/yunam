"""UsageRecorder — single point through which every paid external call flows.

Wrappers around Anthropic, Voyage, and per-request REST APIs (Jina, Sweet
Tracker, Open-Meteo) call into this recorder after each request. The recorder
schedules an async DB write on a fire-and-forget background task so the hot
path stays unblocked; on shutdown `flush()` waits for the queue to drain.

Why a recorder instead of a wrapper-per-client:
- The SDK clients we depend on (anthropic, voyageai) handle retries internally
  and return rich `usage` objects we don't want to re-wrap.
- Externalizing the bookkeeping keeps it possible to disable cost tracking by
  passing `None` everywhere — useful for the fake-Claude REPL tests.

ContextVars carry the current chat_id + skill_id through async call chains
without threading explicit kwargs everywhere. The orchestrator sets them at
the top of each turn / each tool dispatch; tool primitives don't need to know
about the recorder at all unless they want to attribute calls to a specific
provider:endpoint outside the per-skill default.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from .rates import (
    anthropic_cost_micro,
    rest_cost_micro,
    voyage_cost_micro,
)

logger = logging.getLogger("yunam.usage")


# ContextVars are async-task-local: setting them in one coroutine doesn't leak
# into a sibling coroutine. The orchestrator sets these at turn start so any
# downstream tool/recorder call inherits the values.
_chat_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "yunam_usage_chat_id", default=None
)
_skill_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "yunam_usage_skill_id", default=None
)


def set_turn_context(chat_id: int | None) -> contextvars.Token:
    """Bind the current chat_id for subsequent usage records on this task."""
    return _chat_id_var.set(chat_id)


def set_skill_context(skill_id: str | None) -> contextvars.Token:
    """Bind the active skill_id for the duration of a tool dispatch."""
    return _skill_id_var.set(skill_id)


def reset_skill_context(token: contextvars.Token) -> None:
    _skill_id_var.reset(token)


def current_chat_id() -> int | None:
    return _chat_id_var.get()


def current_skill_id() -> str | None:
    return _skill_id_var.get()


class UsageRecorder:
    """Per-process recorder. One instance shared across orchestrator + tools.

    Every `record_*` method is fire-and-forget: it constructs the row dict
    synchronously, schedules `_store.record_api_usage(...)` on a background
    task, and returns. Failure inside the background task logs a WARNING but
    never bubbles up — usage tracking must not break the live agent.
    """

    def __init__(self, store: Any):
        self._store = store
        # Strong-ref the task set so the event loop doesn't GC pending writes.
        # done-callback cleans up after each task settles.
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ---- public recording API ---------------------------------------------

    def record_anthropic(
        self,
        *,
        model: str,
        usage: Any,
        elapsed_ms: int | None = None,
        status: str = "ok",
        chat_id: int | None = None,
        skill_id: str | None = None,
    ) -> None:
        try:
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            cost_micro = anthropic_cost_micro(
                model, input_tokens, output_tokens, cache_read, cache_create
            )
            self._schedule(
                provider="anthropic",
                model_or_endpoint=model,
                chat_id=chat_id if chat_id is not None else current_chat_id(),
                skill_id=skill_id if skill_id is not None else current_skill_id(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_create_tokens=cache_create,
                units=None,
                cost_usd_micro=cost_micro,
                elapsed_ms=elapsed_ms,
                status=status,
            )
        except Exception:
            logger.warning("record_anthropic failed (model=%s)", model, exc_info=True)

    def record_voyage(
        self,
        *,
        model: str,
        text_tokens: int = 0,
        images: int = 0,
        elapsed_ms: int | None = None,
        status: str = "ok",
        chat_id: int | None = None,
        skill_id: str | None = None,
    ) -> None:
        try:
            cost_micro = voyage_cost_micro(
                model, text_tokens=text_tokens, images=images
            )
            self._schedule(
                provider="voyage",
                model_or_endpoint=model,
                chat_id=chat_id if chat_id is not None else current_chat_id(),
                skill_id=skill_id if skill_id is not None else current_skill_id(),
                input_tokens=text_tokens or None,
                output_tokens=None,
                cache_read_tokens=None,
                cache_create_tokens=None,
                units=images or 1,
                cost_usd_micro=cost_micro,
                elapsed_ms=elapsed_ms,
                status=status,
            )
        except Exception:
            logger.warning("record_voyage failed (model=%s)", model, exc_info=True)

    def record_rest(
        self,
        *,
        provider: str,
        endpoint: str,
        units: int = 1,
        elapsed_ms: int | None = None,
        status: str = "ok",
        chat_id: int | None = None,
        skill_id: str | None = None,
    ) -> None:
        """Record a per-request external HTTP call (Jina, Sweet Tracker, ...).

        `provider` is the human-readable label ('jina', 'sweettracker',
        'open-meteo', 'duckduckgo'); `endpoint` is the path/sub-endpoint
        ('reader', 'search', 'trackingInfo', etc). Combined as
        `<provider>:<endpoint>` for the rate lookup.
        """
        try:
            key = f"{provider}:{endpoint}"
            cost_micro = rest_cost_micro(key, units=units)
            self._schedule(
                provider=provider,
                model_or_endpoint=endpoint,
                chat_id=chat_id if chat_id is not None else current_chat_id(),
                skill_id=skill_id if skill_id is not None else current_skill_id(),
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
                cache_create_tokens=None,
                units=units,
                cost_usd_micro=cost_micro,
                elapsed_ms=elapsed_ms,
                status=status,
            )
        except Exception:
            logger.warning(
                "record_rest failed (provider=%s endpoint=%s)",
                provider,
                endpoint,
                exc_info=True,
            )

    def record_mcp(
        self,
        *,
        server: str,
        tool_name: str,
        elapsed_ms: int | None = None,
        status: str = "ok",
        chat_id: int | None = None,
        skill_id: str | None = None,
    ) -> None:
        """Record an MCP tool invocation. MCP itself is free; we track frequency
        so per-sibling-container usage is visible alongside paid APIs."""
        try:
            self._schedule(
                provider=f"mcp:{server}",
                model_or_endpoint=tool_name,
                chat_id=chat_id if chat_id is not None else current_chat_id(),
                skill_id=skill_id if skill_id is not None else current_skill_id(),
                input_tokens=None,
                output_tokens=None,
                cache_read_tokens=None,
                cache_create_tokens=None,
                units=1,
                cost_usd_micro=0,
                elapsed_ms=elapsed_ms,
                status=status,
            )
        except Exception:
            logger.warning(
                "record_mcp failed (server=%s tool=%s)",
                server,
                tool_name,
                exc_info=True,
            )

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait for in-flight DB writes to settle. Call at gateway shutdown."""
        if not self._bg_tasks:
            return
        pending = list(self._bg_tasks)
        try:
            await asyncio.wait(pending, timeout=timeout)
        except Exception:
            logger.warning("usage flush wait raised", exc_info=True)

    # ---- internals --------------------------------------------------------

    def _schedule(self, **row: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called outside an event loop (shouldn't happen in prod). Drop.
            logger.debug("record called outside event loop; dropping")
            return
        task = loop.create_task(
            self._write_row(row), name="yunam-usage-write"
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _write_row(self, row: dict[str, Any]) -> None:
        try:
            await self._store.record_api_usage(**row)
        except Exception:
            logger.warning("api_usage insert failed: row=%r", row, exc_info=True)


__all__ = [
    "UsageRecorder",
    "set_turn_context",
    "set_skill_context",
    "reset_skill_context",
    "current_chat_id",
    "current_skill_id",
]
