"""Obsidian graph skill — wikilinks, tags, backlinks, filtered queries.

Sits alongside the existing `obsidian` skill (CRUD). This one is pure read,
structural: what links to what, which notes share a tag, which notes live
under a folder. When jaekeun writes notes following `preferences/vault-writing.md`
(wikilinks + tags), these tools make that structure queryable.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.obsidian_graph import DEFAULT_LIMIT, MAX_LIMIT, ObsidianGraphTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "obsidian_graph"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Vault graph (wikilinks, tags, backlinks)

Separate from the CRUD tools (`vault_read/write/list/search`), these four
tools query the *structure* of the vault — how notes link to each other,
which notes share tags, what lives in a given folder. Use them when a
question is about relationships, not about the content of one specific
note.

- `vault_backlinks(path)` — notes that link TO this note via `[[wikilink]]`.
  Use when jaekeun asks "누가 이 노트 참조해?" or "어디서 이 사람/프로젝트
  언급했지?". More reliable than `vault_search` on the basename.
- `vault_outgoing_links(path)` — notes this note links TO. Includes links
  to notes that don't exist yet (placeholders jaekeun hasn't filled in).
- `vault_find_by_tag(tag)` — notes tagged with `#tag` OR any nested tag
  under it. Querying `여행` matches `#여행`, `#여행/이탈리아`, `#여행/파리`.
  Pass the body without `#`. Much cleaner than `vault_search` which also
  matches the `#` character in prose.
- `vault_graph_query(folder?, tag?, linked_to?, sort_by?, limit?)` —
  composable filter. Any combination. `sort_by` is `"modified"` (default)
  or `"path"`. `folder` is a prefix match on the vault-relative path
  (e.g. `"daily"` matches `daily/2026-04-21.md`).

### When to pick graph tools vs `vault_search`

- Topic or keyword in note body → `vault_search` (substring, fast, noisy).
- Tag — even "what's tagged X" — → `vault_find_by_tag` (precise, nested).
- "Who links to this?" / "what does X reference?" → `backlinks` /
  `outgoing_links`, not substring search on the filename.
- Multi-filter ("daily notes in April with tag 운동") → `vault_graph_query`.

### Caveats

- Graph tools return paths only, not content. Chain with `vault_read` if
  jaekeun wants the actual text.
- If the vault has no wikilinks or tags yet, these tools return empty
  results — that's expected, not a failure. Jaekeun has started adopting
  the convention (`preferences/vault-writing.md`); structure will grow.
- Paths are vault-relative with `.md` extension
  (e.g. `people/alice.md`). Accept bare note names too if jaekeun gives
  them that way, but return full paths.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "vault_backlinks": {
        "name": "vault_backlinks",
        "description": (
            "List all notes that link TO the given note via [[wikilink]]. "
            "More reliable than substring-searching on the filename. Returns "
            "vault-relative paths, one per line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Vault-relative path of the target note, e.g. "
                        "'people/alice.md'. Bare stems ('alice') also accepted."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    "vault_outgoing_links": {
        "name": "vault_outgoing_links",
        "description": (
            "List all notes this note links TO via [[wikilink]], including "
            "links to notes that don't exist yet (placeholders)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path of the source note.",
                },
            },
            "required": ["path"],
        },
    },
    "vault_find_by_tag": {
        "name": "vault_find_by_tag",
        "description": (
            "List notes tagged with #tag, OR any nested tag under it. "
            "Querying 'trip' matches #trip, #trip/italy, #trip/paris. "
            "Pass the body without the leading '#'. Handles non-ASCII (Korean) "
            "tags. Much cleaner than vault_search for tag queries — won't "
            "match the '#' character appearing in prose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": (
                        "Tag body without leading '#', e.g. '여행/이탈리아' "
                        "or 'projects'. Nested (prefix) match."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max results (1-{MAX_LIMIT}, default {DEFAULT_LIMIT})."
                    ),
                },
            },
            "required": ["tag"],
        },
    },
    "vault_graph_query": {
        "name": "vault_graph_query",
        "description": (
            "Composable filter over notes. Any combination of folder (path "
            "prefix), tag, linked_to (backlink target). Returns paths sorted "
            "by modified-time (default) or path. Use for multi-dimensional "
            "queries like 'daily notes this month tagged #운동'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": (
                        "Vault-relative folder prefix, e.g. 'daily' or "
                        "'projects/yunam'. Matches any path starting with this."
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "Tag body (no '#'). Nested match like vault_find_by_tag."
                    ),
                },
                "linked_to": {
                    "type": "string",
                    "description": (
                        "Vault-relative path. Restricts results to notes that "
                        "link to this one via [[wikilink]]."
                    ),
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["modified", "path"],
                    "description": (
                        "'modified' (default, newest first) or 'path' (alpha)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max results (1-{MAX_LIMIT}, default {DEFAULT_LIMIT})."
                    ),
                },
            },
            "required": [],
        },
    },
}


def build_obsidian_graph_skill(tools: ObsidianGraphTools) -> Skill:
    async def _backlinks(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.backlinks(path=inputs.get("path", ""))

    async def _outgoing(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.outgoing_links(path=inputs.get("path", ""))

    async def _find_by_tag(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.find_by_tag(
            tag=inputs.get("tag", ""),
            limit=inputs.get("limit", DEFAULT_LIMIT),
        )

    async def _graph_query(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.graph_query(
            folder=inputs.get("folder"),
            tag=inputs.get("tag"),
            linked_to=inputs.get("linked_to"),
            sort_by=inputs.get("sort_by", "modified"),
            limit=inputs.get("limit", DEFAULT_LIMIT),
        )

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("vault_backlinks", Scope.VAULT_GRAPH, _SCHEMAS["vault_backlinks"], _backlinks),
        ToolSpec("vault_outgoing_links", Scope.VAULT_GRAPH, _SCHEMAS["vault_outgoing_links"], _outgoing),
        ToolSpec("vault_find_by_tag", Scope.VAULT_GRAPH, _SCHEMAS["vault_find_by_tag"], _find_by_tag),
        ToolSpec("vault_graph_query", Scope.VAULT_GRAPH, _SCHEMAS["vault_graph_query"], _graph_query),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
