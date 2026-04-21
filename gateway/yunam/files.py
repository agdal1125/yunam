"""Filevault path safety + low-level primitives for binary attachments.

The filevault is the on-disk store for photos/docs/etc. sent through Telegram.
This module is the *only* path that writes binaries from user input — it must
reject filename escapes before anything reaches the filesystem.

Parallels `vault.py` (for the Obsidian vault) but supports arbitrary binary
payloads rather than just `.md` text.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

# Telegram's bot API caps file downloads at 20 MB — our enforced ceiling.
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
MAX_FILENAME_LEN = 180  # well under ext4's 255-byte limit; leaves room for suffixes

# Characters forbidden in sanitized filenames. Path separators and control chars
# only — we allow unicode since the vault is user-facing and may have Korean names.
_FORBIDDEN = re.compile(r"[\x00-\x1f/\\]")


class FilevaultError(Exception):
    """Raised by filevault primitives for anything the tool should surface to the model."""


def sanitize_filename(name: str | None, fallback_stem: str, fallback_ext: str = ".bin") -> str:
    """Return a safe basename. Never touches the filesystem.

    - Strips control chars and path separators.
    - Strips leading dots (no hidden files or `..` paths).
    - Collapses whitespace runs to a single space.
    - Truncates to MAX_FILENAME_LEN while preserving the extension.
    - Uses `fallback_stem + fallback_ext` if the result is empty.
    """
    raw = (name or "").strip()
    # If the input looks path-like (has slashes or traversal), keep only the
    # final component before applying the rest of sanitization. Defense in depth
    # against a pathological Telegram filename or a model hallucinating a path.
    if "/" in raw or "\\" in raw:
        raw = re.split(r"[/\\]", raw)[-1]
    raw = _FORBIDDEN.sub("", raw)
    raw = re.sub(r"\s+", " ", raw)
    raw = raw.lstrip(".").strip()

    if not raw:
        return f"{fallback_stem}{fallback_ext}"

    # Preserve extension on truncation: split off the last dot, truncate stem only.
    if "." in raw and not raw.endswith("."):
        stem, _, ext = raw.rpartition(".")
        ext = "." + ext
        if len(ext) > 16:  # extension longer than ~15 chars is almost certainly not an extension
            stem, ext = raw, ""
    else:
        stem, ext = raw, ""

    budget = MAX_FILENAME_LEN - len(ext)
    if budget < 1:
        stem, ext = raw[:MAX_FILENAME_LEN], ""
    elif len(stem) > budget:
        stem = stem[:budget]

    return stem + ext


def safe_join(root: Path, relpath: str) -> Path:
    """Resolve `relpath` under `root`, rejecting any escape.

    `root` must already be an absolute, resolved path.
    """
    if not relpath:
        raise FilevaultError("path is required")
    if relpath.startswith("/"):
        raise FilevaultError("path must be filevault-relative (no leading '/')")
    candidate = (root / relpath).resolve()
    if not candidate.is_relative_to(root):
        raise FilevaultError(f"path escapes filevault root: {relpath!r}")
    return candidate


def unique_target(directory: Path, filename: str) -> Path:
    """Return a non-colliding path under `directory`. Appends `-1`, `-2`, … on clash."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise FilevaultError("too many filename collisions")


def write_bytes_atomic(path: Path, data: bytes) -> int:
    """Atomic write for binary blobs. Returns byte count written.

    Enforces MAX_ATTACHMENT_SIZE. Uses a sibling tempfile + rename — survives a
    mid-write crash without leaving a half-written file under the real name.
    """
    if len(data) > MAX_ATTACHMENT_SIZE:
        raise FilevaultError(
            f"attachment too large ({len(data)} bytes; max {MAX_ATTACHMENT_SIZE})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        import os

        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:
            import os

            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return len(data)


def extension_for(kind: str, mime_type: str | None, file_name: str | None) -> str:
    """Best-guess file extension for a Telegram attachment kind.

    Order: original filename's extension, then mime map, then kind default.
    """
    if file_name and "." in file_name:
        ext = "." + file_name.rsplit(".", 1)[-1].lower()
        if 2 <= len(ext) <= 8 and ext.isascii():
            return ext

    mime_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/heic": ".heic",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/json": ".json",
    }
    if mime_type and mime_type in mime_map:
        return mime_map[mime_type]

    kind_defaults = {
        "photo": ".jpg",
        "voice": ".ogg",
        "audio": ".mp3",
        "video": ".mp4",
        "animation": ".mp4",
        "document": ".bin",
    }
    return kind_defaults.get(kind, ".bin")
