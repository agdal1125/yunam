"""Obsidian graph tools — wikilinks, tags, backlinks over the bind-mounted vault.

We read the vault from disk and compute the link graph in-process, rather than
talking to Obsidian's Local REST API plugin — yunam is a long-running agent
on a headless VPS, and the Obsidian app isn't reachable from there. The
filesystem is authoritative; Obsidian Sync keeps the files fresh.

Two dependencies:
- `obsidiantools` for the link graph (wikilinks / backlinks) — handles
  non-ASCII paths and slashed paths correctly.
- A small hand-rolled tag parser, because `obsidiantools.get_tags` drops
  anything after a `/` in a nested tag, and nested tags are a first-class
  feature we rely on.

Tool-facing contract: agent speaks in vault-relative paths (e.g.
`people/alice.md`). Internally we convert to `obsidiantools` note-stems
(`alice`) and back.

The graph is rebuilt per tool call. On jaekeun's current vault this is
~50ms; a caching layer can be added later if call volume demands it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from .vault import VaultError

logger = logging.getLogger("yunam.tools.obsidian_graph")

# Inline tag regex. Matches `#`+one-or-more of: letters (ASCII + Korean +
# Chinese/Japanese for safety), digits, `_`, `-`, `/`. Must be preceded by
# whitespace or start-of-line so `foo#bar` (URL fragments, CSS ids) don't
# match. Excludes things starting with a digit (Obsidian convention —
# `#123` is not a tag).
_TAG_RE = re.compile(
    r"(?:^|\s)#([^\d\s#.,;:!?()\[\]{}`'\"<>][\w가-힣ㄱ-ㅎㅏ-ㅣ\-/]*)",
    re.UNICODE,
)

# Strip fenced code blocks and code spans before tag scanning — tags inside
# ```...``` or `...` are content, not metadata. Comments / HTML tags are left
# alone; Obsidian users don't hide tags there.
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")

# Hard caps — the model shouldn't be able to request massive result sets.
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


class ObsidianGraphTools:
    def __init__(self, vault_root: Path):
        self._root = vault_root.resolve()
        if not self._root.is_dir():
            raise VaultError(f"vault root not a directory: {self._root}")

    # ---- tool-facing methods -------------------------------------------

    async def backlinks(self, path: str) -> str:
        """Notes that link TO `path` via `[[...]]`."""
        if not path or not isinstance(path, str):
            raise VaultError("path must be a non-empty string")
        vault = await self._build_graph()
        stem = self._stem_for(path, vault)
        try:
            names = vault.get_backlinks(stem)
        except Exception as e:
            raise VaultError(f"backlinks failed: {e}") from e
        paths = sorted({self._path_for(n, vault) for n in names if n})
        return self._format_list(paths, empty_msg=f"no notes link to {path}.")

    async def outgoing_links(self, path: str) -> str:
        """Notes `path` links TO via `[[...]]`. Includes links to notes that don't exist yet."""
        if not path or not isinstance(path, str):
            raise VaultError("path must be a non-empty string")
        vault = await self._build_graph()
        stem = self._stem_for(path, vault)
        try:
            links = vault.get_wikilinks(stem)
        except Exception as e:
            raise VaultError(f"outgoing_links failed: {e}") from e
        # Wikilinks are stored as written (`people/alice` or `alice`); keep
        # the agent's-eye view by sorting and dedup'ing.
        uniq = sorted(set(links))
        return self._format_list(uniq, empty_msg=f"no outgoing links from {path}.")

    async def find_by_tag(self, tag: str, limit: int = DEFAULT_LIMIT) -> str:
        """Notes carrying `tag` (or any nested tag under it).

        `tag` is the tag body without the leading `#`. Prefix match: querying
        `여행` matches notes tagged `#여행`, `#여행/이탈리아`, `#여행/파리`.
        """
        tag = _strip_hash(tag)
        if not tag:
            raise VaultError("tag must be a non-empty string")
        limit = _clamp_limit(limit)
        matches = await asyncio.to_thread(self._scan_for_tag, tag, limit)
        if not matches:
            return f"no notes tagged #{tag} (or nested under it)."
        return self._format_list(sorted(matches))

    async def graph_query(
        self,
        folder: str | None = None,
        tag: str | None = None,
        linked_to: str | None = None,
        sort_by: str = "modified",
        limit: int = DEFAULT_LIMIT,
    ) -> str:
        """Composable filter over notes. Any combination of filters is allowed."""
        limit = _clamp_limit(limit)
        if sort_by not in ("modified", "path"):
            raise VaultError("sort_by must be 'modified' or 'path'")

        # Start from all notes; narrow by each filter in turn.
        vault = await self._build_graph()
        all_paths = {v: k for k, v in vault.md_file_index.items()}  # rel_path -> stem
        rel_paths = set(all_paths.keys())

        # Normalize all_paths keys to strings up-front for set operations.
        rel_paths = {str(p) for p in rel_paths}

        if folder:
            folder_norm = folder.strip("/").rstrip("/") + "/"
            rel_paths = {p for p in rel_paths if p.startswith(folder_norm)}

        if linked_to:
            target_stem = self._stem_for(linked_to, vault)
            try:
                linkers = set(vault.get_backlinks(target_stem))
            except Exception as e:
                raise VaultError(f"linked_to lookup failed: {e}") from e
            linker_paths = {str(vault.md_file_index[stem]) for stem in linkers
                            if stem in vault.md_file_index}
            rel_paths &= linker_paths

        if tag:
            tag_body = _strip_hash(tag)
            tag_matches = await asyncio.to_thread(self._scan_for_tag, tag_body, MAX_LIMIT * 4)
            rel_paths &= set(tag_matches)

        if not rel_paths:
            return "no notes match."

        # Sort
        rows = []
        for rp in rel_paths:
            abs_p = self._root / rp
            try:
                mtime = abs_p.stat().st_mtime
            except OSError:
                mtime = 0.0
            rows.append((rp, mtime))
        if sort_by == "modified":
            rows.sort(key=lambda r: r[1], reverse=True)
        else:
            rows.sort(key=lambda r: r[0])
        rows = rows[:limit]

        lines = []
        for rp, mtime in rows:
            lines.append(rp)
        return self._format_list(lines)

    # ---- internals ------------------------------------------------------

    async def _build_graph(self) -> Any:
        """Connect + gather the obsidiantools Vault. Off the event loop."""
        from obsidiantools.api import Vault  # lazy: ~pandas/networkx

        def _build() -> Any:
            v = Vault(self._root).connect().gather()
            return v

        return await asyncio.to_thread(_build)

    def _stem_for(self, path: str, vault: Any) -> str:
        """Convert a vault-relative path to the obsidiantools note stem."""
        # obsidiantools keys by stem (basename without extension). We accept
        # either a real path ("people/alice.md") or a bare stem ("alice").
        stem = Path(path).stem if path.endswith(".md") or "/" in path else path
        if stem not in vault.md_file_index:
            raise VaultError(f"note not found in vault: {path}")
        return stem

    def _path_for(self, stem: str, vault: Any) -> str:
        # md_file_index returns PosixPath objects; stringify for the agent.
        p = vault.md_file_index.get(stem)
        return str(p) if p is not None else stem + ".md"

    def _scan_for_tag(self, tag_body: str, limit: int) -> list[str]:
        """Walk the vault, return rel_paths of notes carrying `tag_body` as a
        tag or as a prefix of a nested tag. Runs off the event loop."""
        matches: list[str] = []
        # Normalize: case-insensitive compare isn't right for Korean; stick
        # with exact matching but strip trailing slash.
        needle = tag_body.rstrip("/")
        for md_path in self._root.rglob("*.md"):
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for found in _extract_tags(text):
                if found == needle or found.startswith(needle + "/"):
                    rel = str(md_path.relative_to(self._root))
                    matches.append(rel)
                    break
            if len(matches) >= limit:
                break
        return matches

    def _format_list(self, items: list[str], empty_msg: str = "no results.") -> str:
        if not items:
            return empty_msg
        return "\n".join(items)


def _extract_tags(text: str) -> list[str]:
    """Pull inline `#tag` bodies out of markdown text, skipping code regions."""
    clean = _FENCED_BLOCK_RE.sub("", text)
    clean = _CODE_SPAN_RE.sub("", clean)
    return [m.group(1) for m in _TAG_RE.finditer(clean)]


def _strip_hash(tag: str) -> str:
    return (tag or "").lstrip("#").strip()


def _clamp_limit(n: int) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, n))
