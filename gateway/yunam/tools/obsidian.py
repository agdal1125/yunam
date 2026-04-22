"""Obsidian vault tool primitives — the async methods a skill wraps.

Kept deliberately narrow: one class bound to a vault root, four async methods
(read/write/list/search). Schemas, scopes, prompt guidance, and dispatch live in
the skill layer (`yunam/skills/obsidian.py`). This module is agnostic of the
model and the governance layer.

All I/O is wrapped in `asyncio.to_thread` so file reads/writes don't block the
event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
