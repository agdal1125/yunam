"""Small helpers for sending Telegram image attachments to Claude vision."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps


SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MODEL_IMAGE_DIMENSION = 1568
MODEL_IMAGE_BYTES = 4_500_000


def is_inline_image(kind: str | None, mime_type: str | None) -> bool:
    """Return True when an attachment can reasonably be sent as a vision block."""
    if kind == "photo":
        return True
    return bool(mime_type and mime_type.lower() in SUPPORTED_IMAGE_MIMES)


def image_content_block(data: bytes, mime_type: str | None) -> dict[str, Any]:
    """Build an Anthropic image content block, resizing/re-encoding if needed."""
    media_type, payload = _prepare_image(data, mime_type)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(payload).decode("ascii"),
        },
    }


def extract_text_block(content: list[Any]) -> str:
    """Extract the first text block from an Anthropic response-like content list."""
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "text":
            continue
        text = getattr(block, "text", None) or (
            block.get("text") if isinstance(block, dict) else None
        )
        if text:
            return text
    return "(no text returned)"


def _prepare_image(data: bytes, mime_type: str | None) -> tuple[str, bytes]:
    normalized = (mime_type or "").lower()
    if normalized in SUPPORTED_IMAGE_MIMES and len(data) <= MODEL_IMAGE_BYTES:
        return normalized, data

    with Image.open(BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((MODEL_IMAGE_DIMENSION, MODEL_IMAGE_DIMENSION))

        has_alpha = img.mode in {"RGBA", "LA"} or (
            img.mode == "P" and "transparency" in img.info
        )
        buf = BytesIO()
        if has_alpha:
            img.convert("RGBA").save(buf, format="PNG", optimize=True)
            return "image/png", buf.getvalue()

        img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
        return "image/jpeg", buf.getvalue()

