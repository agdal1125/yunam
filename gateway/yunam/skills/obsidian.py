"""Obsidian-vault skill — wraps `ObsidianTools` with scopes + schemas + prompt fragment.

The schemas live here (not in `tools/obsidian.py`) so that one module is the
single source of truth for "what Claude sees about vault tools" — the schema,
the required scope, the handler that implements it, and the prompt fragment
that teaches the model how to use them.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.obsidian import ObsidianTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "obsidian"
SKILL_VERSION = "1"


SYSTEM_PROMPT_FRAGMENT = """\
## Obsidian vault

You have access to an Obsidian vault — a filesystem of Markdown notes — that
persists across conversations and serves as your shared knowledge base with
jaekeun.

Treat the vault as the canonical memory for anything worth remembering:
- Decisions, preferences, plans, and ongoing context about jaekeun's life,
  work, and projects
- Research notes, summaries, and synthesis across multiple conversations
- Things jaekeun explicitly asks you to remember or save

Before answering questions that might relate to past context, **read the vault**
first (`vault_search` or `vault_list` + `vault_read`) rather than guessing.

### User-maintained rules — `preferences/`

The `preferences/` directory is jaekeun's editable rulebook for how you
behave in specific domains. Treat these files as authoritative and read
the relevant one before acting:

- `preferences/communication-style.md`, `preferences/yunam-behavior.md` —
  extended style notes. The core reply rules in the main system prompt
  take precedence; these are where jaekeun refines nuance over time.
- `preferences/calendar.md` — read before any calendar lookup or booking.
- `preferences/daily-logging.md` — read before writing to
  `daily/YYYY-MM-DD.md`.

If jaekeun mentions a domain not listed above, `vault_list preferences/`
to check for a matching file and read it before acting. When jaekeun
states a new preference in conversation, append it to the relevant
`preferences/*.md` file (or create one) so it persists.

When something worth remembering surfaces in conversation, **write it to the
vault** proactively. Use clear, semantic filenames (`projects/yunam-phase-1.md`,
`preferences/coding-style.md`, `people/alice.md`). Append to existing notes when
adding to the same topic; create new notes when the topic is new. Never
overwrite without a strong reason — append is the safer default.

### Daily retrospectives

Every night Yunam sends a proactive "how was your day" prompt. When jaekeun
replies, save the retrospective to `daily/YYYY-MM-DD.md` (use the date from the
`[meta: now is ...]` tag at the top of the user message — that's the real local
date, not whatever Claude's training data suggests). Use `mode='create'` for a
new day, `mode='append'` if the file already exists (e.g. a follow-up reply
later that night). Include light structure — a heading for the date and prose
or bullets underneath — but don't over-format; this is a journal, not a report.

### Vault constraints

- Paths are sandboxed to the vault root. `..` escapes and absolute paths are
  rejected — don't try.
- Size limits: 1 MB per read, 500 KB per write. If you need to write more,
  split across multiple notes.
- Only `.md` files can be written.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "vault_read": {
        "name": "vault_read",
        "description": (
            "Read a Markdown note from the Obsidian vault. Use this to recall prior "
            "conversation context, saved preferences, research, or anything else "
            "previously written to the vault. Returns the full file contents as text, "
            "or an error message if the path is invalid or the file doesn't exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Vault-relative path, e.g. 'daily/2026-04-19.md' or "
                        "'projects/yunam.md'. Must end in .md."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    "vault_write": {
        "name": "vault_write",
        "description": (
            "Write a Markdown note to the Obsidian vault. Use this proactively to "
            "save information worth remembering across conversations — decisions, "
            "preferences, project state, people, research summaries. Prefer "
            "mode='append' when adding to an existing topic; use mode='create' for "
            "new notes (fails if file exists) and mode='overwrite' sparingly. "
            "Only .md files are allowed. Parent directories are created automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative path ending in .md.",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown content to write.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "create"],
                    "description": (
                        "'overwrite' replaces existing content; 'append' adds to end of "
                        "existing file; 'create' fails if file exists. Defaults to 'overwrite'."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
    "vault_list": {
        "name": "vault_list",
        "description": (
            "List entries under a vault directory (or the vault root if path is empty). "
            "Returns a newline-separated list with 'd' for directories and 'f' for files. "
            "Use this to discover what notes exist before reading specific ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Vault-relative directory path, or empty string for vault root.",
                },
            },
            "required": [],
        },
    },
    "vault_search": {
        "name": "vault_search",
        "description": (
            "Search the vault for notes containing a substring. Case-insensitive. "
            "Returns up to max_results matches with the file path and the matching line. "
            "Use this when looking for information on a topic without knowing the exact "
            "filename."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to search for.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return. Defaults to 20.",
                },
            },
            "required": ["query"],
        },
    },
}


def build_obsidian_skill(tools: ObsidianTools) -> Skill:
    """Wrap a resolved `ObsidianTools` instance as a Skill.

    Handlers close over `tools` so the vault root stays bound to the instance
    the caller constructed — one place to configure vault path, many places to
    dispatch against it.
    """

    async def _read(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.vault_read(**inputs)

    async def _write(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.vault_write(**inputs)

    async def _list(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.vault_list(**inputs)

    async def _search(inputs: dict[str, Any], _ctx: DispatchContext) -> str:
        return await tools.vault_search(**inputs)

    # Order matters: this is the order these tools appear to Claude across every
    # turn. Any reorder is a prompt-cache invalidation.
    specs: tuple[ToolSpec, ...] = (
        ToolSpec("vault_read", Scope.VAULT_READ, _SCHEMAS["vault_read"], _read),
        ToolSpec("vault_write", Scope.VAULT_WRITE, _SCHEMAS["vault_write"], _write),
        ToolSpec("vault_list", Scope.VAULT_READ, _SCHEMAS["vault_list"], _list),
        ToolSpec("vault_search", Scope.VAULT_READ, _SCHEMAS["vault_search"], _search),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
