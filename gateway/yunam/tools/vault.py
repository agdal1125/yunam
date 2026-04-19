"""Vault path safety + low-level file primitives.

Every access to the Obsidian vault goes through `safe_join()`. That's the only
barrier between the model and the host filesystem, so it MUST reject escapes.
`Path.resolve().is_relative_to(root)` handles `..`, symlinks, and absolute paths
in one check.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

MAX_READ_SIZE = 1_000_000  # 1 MB
MAX_WRITE_SIZE = 500_000  # 0.5 MB


class VaultError(Exception):
    """Raised by vault primitives for anything the tool should surface to the model."""


def safe_join(root: Path, user_path: str) -> Path:
    """Resolve `user_path` under `root`, rejecting any escape.

    `root` must already be an absolute, resolved path (from config loading).
    `user_path` is a vault-relative string provided by the model.
    """
    if user_path is None or user_path == "":
        raise VaultError("path is required")
    if user_path.startswith("/"):
        raise VaultError("path must be vault-relative (no leading '/')")
    candidate = (root / user_path).resolve()
    if not candidate.is_relative_to(root):
        raise VaultError(f"path escapes vault root: {user_path!r}")
    return candidate


def enforce_md(path: Path) -> None:
    if path.suffix.lower() != ".md":
        raise VaultError(f"only .md files can be written; got {path.suffix!r}")


def read_text_capped(path: Path) -> str:
    if not path.exists():
        raise VaultError(f"file not found: {path.relative_to(path.anchor)}")
    if not path.is_file():
        raise VaultError("path is not a file")
    size = path.stat().st_size
    if size > MAX_READ_SIZE:
        raise VaultError(f"file too large ({size} bytes; max {MAX_READ_SIZE})")
    return path.read_text(encoding="utf-8")


def write_text_atomic(path: Path, content: str) -> int:
    """Write `content` to `path` atomically. Returns byte count written."""
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_SIZE:
        raise VaultError(f"content too large ({len(data)} bytes; max {MAX_WRITE_SIZE})")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tempfile in same dir + rename. Survives a crash mid-write.
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return len(data)


def append_text(path: Path, content: str) -> int:
    if not path.exists():
        raise VaultError(f"file not found: {path.name}")
    existing = read_text_capped(path)
    joined = existing + ("" if existing.endswith("\n") else "\n") + content
    return write_text_atomic(path, joined)
