"""Skill registry primitives.

A `Skill` is a bundle of (1) a stable-ordered tuple of `ToolSpec`s, (2) an
optional system-prompt fragment, and (3) identity metadata. Skills are loaded
once at orchestrator init; the resulting `SkillRegistry` flattens their tools
into a deterministic list for Claude and provides a single dispatch surface
that records the originating skill and scope for each call.

Prompt-cache invariant: skills MUST be loaded in a deterministic order, and
each skill's tool order MUST be stable — the flattened `tool_schemas` list is
sent to Claude as-is on every turn, and a single reorder invalidates the cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from ..capabilities import Scope
from ..tools.vault import VaultError


@dataclass(frozen=True)
class DispatchContext:
    """Per-turn context passed to every tool handler.

    Kept intentionally minimal — add fields when an actual tool needs them, not
    speculatively. `chat_id` is here because attachment tools branch on it
    (pending-attachment lookup, retrieval destination).
    """

    chat_id: int


ToolHandler = Callable[[dict[str, Any], DispatchContext], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    """One tool within a skill.

    `schema` is the raw Claude tool schema (the dict with `name`, `description`,
    `input_schema`). It's stored verbatim so the schema lives next to the
    handler that implements it — one source of truth per tool.
    """

    name: str
    scope: Scope
    schema: dict[str, Any]
    handler: ToolHandler


@dataclass(frozen=True)
class Skill:
    id: str
    version: str
    tools: tuple[ToolSpec, ...]
    system_prompt_fragment: str = ""


class SkillRegistry:
    """Immutable view over a fixed, ordered sequence of skills.

    Construct once at startup and share read-only across the orchestrator.
    `tool_schemas` and `system_prompt_fragments` are computed eagerly so repeat
    reads are free — and, crucially, byte-identical across turns.
    """

    def __init__(self, skills: Sequence[Skill]):
        self._skills: tuple[Skill, ...] = tuple(skills)
        by_tool: dict[str, tuple[Skill, ToolSpec]] = {}
        schemas: list[dict[str, Any]] = []
        for skill in self._skills:
            for tool in skill.tools:
                if tool.name in by_tool:
                    existing_skill = by_tool[tool.name][0]
                    raise ValueError(
                        f"duplicate tool name {tool.name!r} "
                        f"(declared by {existing_skill.id!r} and {skill.id!r})"
                    )
                by_tool[tool.name] = (skill, tool)
                schemas.append(tool.schema)
        self._by_tool = by_tool
        self._tool_schemas: tuple[dict[str, Any], ...] = tuple(schemas)
        self._fragments: tuple[str, ...] = tuple(
            s.system_prompt_fragment for s in self._skills if s.system_prompt_fragment
        )

    @property
    def skills(self) -> tuple[Skill, ...]:
        return self._skills

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        # Return a fresh list each time; the contents are frozen (shared dicts),
        # but callers may mutate the outer list. The inner dicts must not be
        # mutated — that would break prompt caching.
        return list(self._tool_schemas)

    @property
    def system_prompt_fragments(self) -> tuple[str, ...]:
        return self._fragments

    def lookup(self, name: str) -> tuple[Skill, ToolSpec]:
        """Return (skill, tool) for `name` or raise VaultError if unknown.

        Raising VaultError (rather than KeyError) keeps the orchestrator's
        existing exception-handling shape: the model sees a clean tool-error
        message instead of a stacktrace.
        """
        entry = self._by_tool.get(name)
        if entry is None:
            raise VaultError(f"unknown tool: {name}")
        return entry
