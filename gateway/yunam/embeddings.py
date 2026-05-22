"""Voyage AI multimodal embeddings for saved files.

`voyage-multimodal-3` embeds a mix of images and text into a single 1024-dim
vector per document. We build per-file inputs based on kind + mime:

- Images (jpg/png/webp/gif/heic) → PIL Image + caption/description text
- Small text files (.md/.txt/.json/code) → file content + caption/description
- Everything else (video, voice, opaque binaries) → metadata-only text embedding
  (filename + caption + description). The file itself isn't embedded — we treat
  it as an unseen blob retrievable by what you said about it.

Embeddings go into the `file_embeddings` vec0 table in the session SQLite.
Keeping the store co-located means one connection, one transaction, one backup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .usage import UsageRecorder

# PIL is imported lazily inside `_build_multimodal_input` so the module can be
# inspected in environments (CI, local sanity checks) that don't have Pillow
# installed. Pillow is a prod-only dep of the embedding pipeline.

logger = logging.getLogger("yunam.embeddings")

EMBED_MODEL = "voyage-multimodal-3"

# Voyage's multimodal model input limits (as of early 2026): per-input caps of
# ~1 image + text. We downsize large images before upload to keep requests snappy
# and stay under the payload cap.
MAX_IMAGE_DIM = 1024
MAX_TEXT_CHARS = 8_000  # trim long text files before embedding — far below token limit

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic"}
_TEXTLIKE_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/x-python",
    "application/json",
    "application/xml",
    "text/html",
}
_TEXTLIKE_EXTS = {
    ".md", ".txt", ".json", ".csv", ".xml", ".html", ".py", ".js", ".ts",
    ".go", ".rs", ".c", ".cpp", ".h", ".java", ".sh", ".yaml", ".yml", ".toml",
}


class EmbeddingError(Exception):
    """Raised when an embedding call fails in a way the caller should surface."""


class VoyageEmbedder:
    """Async wrapper around `voyageai.AsyncClient` with per-file-type prep."""

    def __init__(self, api_key: str, *, usage_recorder: UsageRecorder | None = None):
        # Import lazily so unit tests and the REPL don't require voyageai to be installed.
        import voyageai

        self._client = voyageai.AsyncClient(api_key=api_key)
        self._usage = usage_recorder

    def _record(
        self,
        *,
        result: Any,
        images: int,
        elapsed_ms: int,
        status: str,
    ) -> None:
        if self._usage is None:
            return
        # voyageai's result object exposes `total_tokens` on a `.usage` attr
        # for text models; multimodal results expose `.text_tokens` /
        # `.image_pixels`. Tolerate either shape — missing → 0.
        token_count = 0
        usage_obj = getattr(result, "usage", None)
        if usage_obj is not None:
            token_count = (
                getattr(usage_obj, "total_tokens", None)
                or getattr(usage_obj, "text_tokens", None)
                or 0
            )
        else:
            token_count = (
                getattr(result, "total_tokens", None)
                or getattr(result, "text_tokens", None)
                or 0
            )
        self._usage.record_voyage(
            model=EMBED_MODEL,
            text_tokens=int(token_count or 0),
            images=images,
            elapsed_ms=elapsed_ms,
            status=status,
        )

    async def embed_document(
        self,
        *,
        file_path: Path,
        kind: str,
        mime_type: str | None,
        caption: str | None,
        description: str | None,
    ) -> list[float]:
        """Build a per-file multimodal input, embed it, return a 1024-dim vector.

        Never raises for non-critical problems (oversized image, unreadable text)
        — falls back to a metadata-only text embedding so every saved file ends
        up in the vector index.
        """
        text_parts = _text_parts(file_path.name, caption, description)
        inputs = await asyncio.to_thread(
            _build_multimodal_input, file_path, kind, mime_type, text_parts
        )
        image_count = sum(
            1 for item in inputs if not isinstance(item, (str, bytes))
        )

        t0 = time.monotonic()
        try:
            result = await self._client.multimodal_embed(
                inputs=[inputs],
                model=EMBED_MODEL,
                input_type="document",
            )
        except Exception as e:
            self._record(
                result=None,
                images=image_count,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                status="error",
            )
            raise EmbeddingError(f"voyage embed failed: {e}") from e

        self._record(
            result=result,
            images=image_count,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            status="ok",
        )
        vectors = getattr(result, "embeddings", None)
        if not vectors:
            raise EmbeddingError("voyage returned no embeddings")
        return list(vectors[0])

    async def embed_query(self, text: str) -> list[float]:
        t0 = time.monotonic()
        try:
            result = await self._client.multimodal_embed(
                inputs=[[text]],
                model=EMBED_MODEL,
                input_type="query",
            )
        except Exception as e:
            self._record(
                result=None,
                images=0,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                status="error",
            )
            raise EmbeddingError(f"voyage query embed failed: {e}") from e
        self._record(
            result=result,
            images=0,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            status="ok",
        )
        vectors = getattr(result, "embeddings", None)
        if not vectors:
            raise EmbeddingError("voyage returned no embeddings")
        return list(vectors[0])

    async def embed_text_document(self, text: str) -> list[float]:
        """Embed arbitrary text with `input_type='document'` (for indexing).

        Symmetric partner to `embed_query`. Use this for conversation turns,
        vault notes, or any text we're storing in a vec0 table for later KNN.
        """
        t0 = time.monotonic()
        try:
            result = await self._client.multimodal_embed(
                inputs=[[text]],
                model=EMBED_MODEL,
                input_type="document",
            )
        except Exception as e:
            self._record(
                result=None,
                images=0,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                status="error",
            )
            raise EmbeddingError(f"voyage text-document embed failed: {e}") from e
        self._record(
            result=result,
            images=0,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            status="ok",
        )
        vectors = getattr(result, "embeddings", None)
        if not vectors:
            raise EmbeddingError("voyage returned no embeddings")
        return list(vectors[0])


def _text_parts(filename: str, caption: str | None, description: str | None) -> str:
    """Build the text portion of a multimodal input from file metadata."""
    pieces = [f"filename: {filename}"]
    if caption:
        pieces.append(f"caption: {caption}")
    if description:
        pieces.append(f"description: {description}")
    return " · ".join(pieces)


def _build_multimodal_input(
    file_path: Path, kind: str, mime_type: str | None, text: str
) -> list:
    """Synchronous input assembly — runs in a worker thread via `to_thread`.

    Opens images with PIL (downsized), reads short text files directly, and
    falls back to a text-only input for everything else.
    """
    ext = file_path.suffix.lower()
    is_image = (mime_type in _IMAGE_MIMES) or (kind == "photo") or ext in {
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic",
    }
    is_textlike = (mime_type in _TEXTLIKE_MIMES) or (ext in _TEXTLIKE_EXTS)

    if is_image:
        try:
            from PIL import Image  # lazy: Pillow is a heavy prod dep

            img = Image.open(file_path)
            img.load()
            # Downsize in place; voyage accepts up to several MP but smaller is faster.
            img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
            # Normalize mode — voyage handles RGB; convert RGBA/P to RGB to avoid surprises.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            return [img, text]
        except Exception as e:
            logger.warning("image open failed for %s (%s); falling back to text-only", file_path, e)

    if is_textlike:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > MAX_TEXT_CHARS:
                content = content[:MAX_TEXT_CHARS]
            return [f"{text}\n\n---\n{content}"]
        except Exception as e:
            logger.warning("text read failed for %s (%s); falling back to metadata-only", file_path, e)

    # Binary blob with no native embedding path: just the metadata text.
    return [text]


__all__ = ["VoyageEmbedder", "EmbeddingError", "EMBED_MODEL"]
