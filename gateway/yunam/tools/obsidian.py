"""Obsidian vault tools exposed to Claude.

Four async tool functions + a module-level `OBSIDIAN_TOOL_SCHEMAS` list (stable order — don't
rebuild dynamically; prompt caching depends on byte-identical prefixes across turns).

All I/O wrapped in `asyncio.to_thread` so file reads/writes don't block the event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .vault import (
    VaultError,
    append_text,
    enforce_md,
    read_text_capped,
    safe_join,
    write_text_atomic,
)

MAX_LIST_ENTRIES = 500
MAX_SEARCH_FILE_SIZE = 1_000_000  # skip giant files during search


OBSIDIAN_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
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
    {
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
    {
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
    {
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
]


def _sync_list(root: Path, subpath: str) -> str:
    target = safe_join(root, subpath) if subpath else root
    if not target.exists():
        raise VaultError("path not found")
    if target.is_file():
        rel = target.relative_to(root)
        size = target.stat().st_size
        return f"f {rel} ({size} bytes)"
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = []
    for entry in entries[:MAX_LIST_ENTRIES]:
        rel = entry.relative_to(root)
        tag = "d" if entry.is_dir() else "f"
        lines.append(f"{tag} {rel}")
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"... ({len(entries) - MAX_LIST_ENTRIES} more entries omitted)")
    return "\n".join(lines) if lines else "(empty directory)"


def _sync_search(root: Path, query: str, max_results: int) -> str:
    if not query:
        raise VaultError("query is required")
    needle = query.lower()
    hits: list[str] = []
    for md_file in root.rglob("*.md"):
        try:
            if md_file.stat().st_size > MAX_SEARCH_FILE_SIZE:
                continue
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                rel = md_file.relative_to(root)
                snippet = line.strip()[:200]
                hits.append(f"{rel}:{line_no}: {snippet}")
                if len(hits) >= max_results:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(no matches)"


class ObsidianTools:
    """Bound to a resolved vault root. Pass the instance into the orchestrator."""

    def __init__(self, vault_root: Path):
        # Caller is responsible for passing an already-resolved path.
        self.root = vault_root

    async def vault_read(self, path: str) -> str:
        def _read() -> str:
            target = safe_join(self.root, path)
            return read_text_capped(target)

        return await asyncio.to_thread(_read)

    async def vault_write(
        self, path: str, content: str, mode: str = "overwrite"
    ) -> str:
        def _write() -> str:
            target = safe_join(self.root, path)
            enforce_md(target)
            if mode == "create":
                if target.exists():
                    raise VaultError(f"file already exists: {path}")
                n = write_text_atomic(target, content)
                return f"created {path} ({n} bytes)"
            if mode == "append":
                if not target.exists():
                    # Match read behaviour: append-to-missing is an error.
                    raise VaultError(f"file not found: {path}")
                n = append_text(target, content)
                return f"appended to {path} ({n} bytes total)"
            if mode == "overwrite":
                n = write_text_atomic(target, content)
                return f"wrote {path} ({n} bytes)"
            raise VaultError(f"unknown mode: {mode!r}")

        return await asyncio.to_thread(_write)

    async def vault_list(self, path: str = "") -> str:
        return await asyncio.to_thread(_sync_list, self.root, path)

    async def vault_search(self, query: str, max_results: int = 20) -> str:
        return await asyncio.to_thread(_sync_search, self.root, query, max_results)

    async def dispatch(self, name: str, inputs: dict[str, Any]) -> str:
        fn = getattr(self, name, None)
        if fn is None or name not in {
            "vault_read",
            "vault_write",
            "vault_list",
            "vault_search",
        }:
            raise VaultError(f"unknown tool: {name}")
        return await fn(**inputs)
