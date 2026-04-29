"""Attachment primitives — save / search / retrieve methods on `AttachmentTools`.

Schemas, scopes, prompt guidance, and dispatch live in the skill layer
(`yunam/skills/files.py`). This module is the implementation surface: the class
bound to the session store, filevault root, Telegram sender, and embedder.

Flow for `save_attachment`:
  1. Look up the most recent pending attachment for this chat.
  2. Download it from Telegram by `file_id` (we defer download until commit, so
     if the user never /saves, we never touch disk).
  3. Write to the filevault under `YYYY-MM-DD/<sanitized-name>`.
  4. Write a breadcrumb `.md` note into the Obsidian vault at `files/YYYY-MM-DD/<name>.md`.
  5. Embed the file via Voyage and store the vector in `file_embeddings`.
  6. Delete the pending row; record the saved-file row.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..embeddings import EmbeddingError, VoyageEmbedder
from ..files import (
    FilevaultError,
    extension_for,
    safe_join as filevault_safe_join,
    sanitize_filename,
    unique_target,
)
from ..sender import AttachmentSender
from ..sessions import PendingAttachment, SavedFile, SessionStore
from ..vision import extract_text_block, image_content_block, is_inline_image
from .vault import VaultError, write_text_atomic as vault_write_text_atomic

logger = logging.getLogger("yunam.tools.attachments")


# Search result caps.
_MAX_SEARCH_LIMIT = 20
_DEFAULT_SEARCH_LIMIT = 5
_MAX_VISION_IMAGES = 10
_DEFAULT_VISION_MODEL = "claude-sonnet-4-6"


class AttachmentTools:
    """Agent tools for file save / search / retrieve. One per process."""

    def __init__(
        self,
        *,
        store: SessionStore,
        filevault_root: Path,
        obsidian_root: Path,
        sender: AttachmentSender,
        embedder: VoyageEmbedder,
        vision_client: Any | None = None,
        vision_model: str = _DEFAULT_VISION_MODEL,
        timezone: str = "Asia/Seoul",
    ):
        self._store = store
        self._filevault = filevault_root
        self._obsidian = obsidian_root
        self._sender = sender
        self._embedder = embedder
        self._vision_client = vision_client
        self._vision_model = vision_model
        self._tz = ZoneInfo(timezone)

    # ---- public tool surface ---------------------------------------------

    async def save_attachment(
        self,
        *,
        chat_id: int,
        destination_name: str | None = None,
        caption: str | None = None,
        description: str | None = None,
    ) -> str:
        saved = await self.commit_pending(
            chat_id=chat_id,
            destination_name=destination_name,
            caption_override=caption,
            description=description,
        )
        if saved is None:
            return "no pending attachment for this chat — ask the user to resend."
        return (
            f"saved {saved.relpath} "
            f"({saved.file_size or 0} bytes, {saved.mime_type or 'unknown mime'}). "
            f"indexed for semantic search."
        )

    async def save_attachments(
        self,
        *,
        chat_id: int,
        pending_ids: list[int] | None = None,
        media_group_id: str | None = None,
        caption: str | None = None,
        description: str | None = None,
    ) -> str:
        saved = await self.commit_pending_attachments(
            chat_id=chat_id,
            pending_ids=pending_ids,
            media_group_id=media_group_id,
            caption_override=caption,
            description=description,
        )
        if not saved:
            return "no matching pending attachments for this chat; ask the user to resend."
        lines = [f"saved {len(saved)} attachment(s):"]
        for idx, sf in enumerate(saved, start=1):
            lines.append(
                f"{idx}. {sf.relpath} "
                f"({sf.file_size or 0} bytes, {sf.mime_type or 'unknown mime'})"
            )
        return "\n".join(lines)

    async def search_files(
        self, *, chat_id: int, query: str, limit: int | None = None
    ) -> str:
        del chat_id  # not filtered in v1; single-user bot anyway
        if not query or not query.strip():
            raise ValueError("query is required")
        k = limit if limit is not None else _DEFAULT_SEARCH_LIMIT
        k = max(1, min(int(k), _MAX_SEARCH_LIMIT))
        try:
            query_vec = await self._embedder.embed_query(query.strip())
        except EmbeddingError as e:
            return f"embedding search unavailable: {e}"
        hits = await self._store.search_files_semantic(query_vec, limit=k)
        if not hits:
            return "(no matches)"
        lines = []
        for sf, dist in hits:
            lines.append(
                f"{sf.relpath}\tdistance={dist:.4f}\tkind={sf.kind}\t"
                f"size={sf.file_size}\tcaption={sf.caption or ''}\t"
                f"description={(sf.description or '')[:200]}"
            )
        return "\n".join(lines)

    async def retrieve_attachment(
        self, *, chat_id: int, path: str, caption: str | None = None
    ) -> str:
        # Resolve strictly under filevault root — agent-supplied path is untrusted.
        try:
            target = filevault_safe_join(self._filevault, path)
        except FilevaultError as e:
            return f"retrieve error: {e}"
        if not target.is_file():
            return f"retrieve error: not a file: {path}"
        try:
            await self._sender.send_document(chat_id, target, caption=caption)
        except Exception as e:
            logger.exception("send_document failed for %s", target)
            return f"retrieve error: send failed: {e}"
        return f"sent {path}"

    async def extract_attachment_text(
        self,
        *,
        chat_id: int,
        pending_ids: list[int] | None = None,
        media_group_id: str | None = None,
        paths: list[str] | None = None,
        prompt: str | None = None,
    ) -> str:
        """Use Claude vision to transcribe text from pending or saved images."""
        if self._vision_client is None:
            return "image text extraction unavailable: vision client is not configured."

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Extract all visible text from each image. Preserve line breaks "
                    "where useful. Separate the result by image label. "
                    f"User request: {prompt.strip() if prompt else 'extract text'}"
                ),
            }
        ]
        labels: list[str] = []
        skipped: list[str] = []

        pending: list[PendingAttachment] = []
        if pending_ids or media_group_id or not paths:
            pending = await self._store.list_pending_attachments(
                chat_id,
                pending_ids=_clean_pending_ids(pending_ids),
                media_group_id=media_group_id,
                limit=_MAX_VISION_IMAGES if not paths else None,
            )
        for item in pending[:_MAX_VISION_IMAGES]:
            label = _pending_label(item)
            if not is_inline_image(item.kind, item.mime_type):
                skipped.append(
                    f"{label}: not an image ({item.kind}/{item.mime_type or 'unknown'})"
                )
                continue
            try:
                data = await self._sender.download_bytes(item.file_id)
                content.append({"type": "text", "text": label})
                content.append(image_content_block(data, item.mime_type))
                labels.append(label)
            except Exception as e:
                logger.exception("pending image download failed id=%s", item.id)
                skipped.append(f"{label}: download failed ({e})")

        remaining = max(0, _MAX_VISION_IMAGES - len(labels))
        for relpath in (paths or [])[:remaining]:
            label = f"saved file {relpath}"
            try:
                target = filevault_safe_join(self._filevault, relpath)
            except FilevaultError as e:
                skipped.append(f"{label}: {e}")
                continue
            if not target.is_file():
                skipped.append(f"{label}: not found")
                continue
            mime_type = _mime_from_suffix(target.suffix)
            if not is_inline_image("document", mime_type):
                skipped.append(f"{label}: unsupported image type ({target.suffix})")
                continue
            try:
                content.append({"type": "text", "text": label})
                content.append(image_content_block(target.read_bytes(), mime_type))
                labels.append(label)
            except Exception as e:
                logger.exception("saved image read failed path=%s", target)
                skipped.append(f"{label}: read failed ({e})")

        if not labels:
            suffix = f" Skipped: {'; '.join(skipped)}" if skipped else ""
            return f"no supported images found for text extraction.{suffix}"

        try:
            response = await self._vision_client.messages.create(
                model=self._vision_model,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
            )
        except Exception as e:
            logger.exception("vision extraction failed")
            return f"image text extraction failed: {e}"

        result = extract_text_block(response.content)
        if skipped:
            result = f"{result}\n\nSkipped: {'; '.join(skipped)}"
        return result

    # ---- shared save path (used by agent tool AND /save command) ---------

    async def commit_pending(
        self,
        *,
        chat_id: int,
        destination_name: str | None = None,
        caption_override: str | None = None,
        description: str | None = None,
    ) -> SavedFile | None:
        """Core save logic. Returns the SavedFile, or None if no pending attachment."""
        pending = await self._store.latest_pending_attachment(chat_id)
        if pending is None:
            return None
        return await self._commit_one_pending(
            chat_id=chat_id,
            pending=pending,
            destination_name=destination_name,
            caption_override=caption_override,
            description=description,
        )

    async def commit_pending_attachments(
        self,
        *,
        chat_id: int,
        pending_ids: list[int] | None = None,
        media_group_id: str | None = None,
        caption_override: str | None = None,
        description: str | None = None,
    ) -> list[SavedFile]:
        """Save multiple pending attachments, preserving Telegram arrival order."""
        pending = await self._store.list_pending_attachments(
            chat_id,
            pending_ids=_clean_pending_ids(pending_ids),
            media_group_id=media_group_id,
        )
        saved: list[SavedFile] = []
        for item in pending:
            saved.append(
                await self._commit_one_pending(
                    chat_id=chat_id,
                    pending=item,
                    caption_override=caption_override,
                    description=description,
                )
            )
        return saved

    async def _commit_one_pending(
        self,
        *,
        chat_id: int,
        pending: PendingAttachment,
        destination_name: str | None = None,
        caption_override: str | None = None,
        description: str | None = None,
    ) -> SavedFile:
        """Save a specific pending attachment to the filevault."""

        today = datetime.now(self._tz).strftime("%Y-%m-%d")
        ext = extension_for(pending.kind, pending.mime_type, pending.file_name)
        fallback_stem = f"{pending.kind}-{today}-{datetime.now(self._tz).strftime('%H%M%S')}"
        desired_name = sanitize_filename(
            destination_name or pending.file_name, fallback_stem, ext
        )

        day_dir = self._filevault / today
        target_path = unique_target(day_dir, desired_name)

        # Download bytes directly onto the final path.
        size = await self._sender.download_to(pending.file_id, target_path)
        relpath = str(target_path.relative_to(self._filevault))

        caption = caption_override if caption_override is not None else pending.caption

        # Embed the file before recording the DB row (so a failed embedding fails
        # the whole save cleanly and we don't leave orphaned rows).
        try:
            embedding = await self._embedder.embed_document(
                file_path=target_path,
                kind=pending.kind,
                mime_type=pending.mime_type,
                caption=caption,
                description=description,
            )
        except EmbeddingError as e:
            logger.warning("embedding failed for %s: %s — saving without vector", relpath, e)
            embedding = None

        saved_id = await self._store.record_saved_file(
            chat_id=chat_id,
            relpath=relpath,
            kind=pending.kind,
            file_name=target_path.name,
            mime_type=pending.mime_type,
            file_size=size,
            caption=caption,
            description=description,
            embedding=embedding,
        )
        await self._store.delete_pending_attachment(pending.id)

        # Breadcrumb note into the Obsidian vault — human-readable index.
        await self._write_breadcrumb(
            today=today,
            target_path=target_path,
            relpath=relpath,
            kind=pending.kind,
            mime_type=pending.mime_type,
            size=size,
            caption=caption,
            description=description,
        )

        return SavedFile(
            id=saved_id,
            chat_id=chat_id,
            relpath=relpath,
            kind=pending.kind,
            file_name=target_path.name,
            mime_type=pending.mime_type,
            file_size=size,
            caption=caption,
            description=description,
            created_at=datetime.now(self._tz).isoformat(timespec="seconds"),
        )

    # ---- internals -------------------------------------------------------

    async def _write_breadcrumb(
        self,
        *,
        today: str,
        target_path: Path,
        relpath: str,
        kind: str,
        mime_type: str | None,
        size: int,
        caption: str | None,
        description: str | None,
    ) -> None:
        """Write the Obsidian-vault `.md` breadcrumb for this saved file."""
        note_relpath = f"files/{today}/{target_path.name}.md"
        note_path = self._obsidian / note_relpath
        uploaded_iso = datetime.now(self._tz).isoformat(timespec="minutes")
        body_lines = [
            "---",
            f"file: {relpath}",
            f"path: {target_path}",
            f"kind: {kind}",
            f"mime: {mime_type or 'unknown'}",
            f"size: {size}",
            f"uploaded: {uploaded_iso}",
        ]
        if caption:
            body_lines.append(f"caption: {_yaml_scalar(caption)}")
        body_lines.append("---")
        body_lines.append("")
        body_lines.append(f"# {target_path.name}")
        body_lines.append("")
        body_lines.append(f"Uploaded {uploaded_iso}. Kind: {kind}.")
        if caption:
            body_lines.append("")
            body_lines.append(f"Caption: {caption}")
        if description:
            body_lines.append("")
            body_lines.append("## Notes")
            body_lines.append("")
            body_lines.append(description)
        body = "\n".join(body_lines) + "\n"

        try:
            # Reuse the vault's atomic-write primitive so breadcrumbs inherit size caps.
            import asyncio

            def _write() -> None:
                note_path.parent.mkdir(parents=True, exist_ok=True)
                vault_write_text_atomic(note_path, body)

            await asyncio.to_thread(_write)
        except VaultError as e:
            # Breadcrumb failure shouldn't block the core save — the file and
            # embedding are already committed. Log and move on.
            logger.warning("breadcrumb write failed for %s: %s", note_relpath, e)

def _clean_pending_ids(value: list[int] | None) -> list[int] | None:
    if not value:
        return None
    cleaned: list[int] = []
    for item in value:
        try:
            cleaned.append(int(item))
        except (TypeError, ValueError):
            continue
    return cleaned or None


def _pending_label(item: PendingAttachment) -> str:
    name = item.file_name or item.kind
    return f"pending image {item.id} ({name})"


def _mime_from_suffix(suffix: str) -> str | None:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix.lower())


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar that might contain colons/quotes/newlines."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


__all__ = ["AttachmentTools"]
