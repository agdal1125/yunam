"""Filevault skill — wraps `AttachmentTools` with scopes + schemas + prompt fragment.

Attachment tools need the chat_id (pending-attachment lookup, Telegram
retrieval destination), so the handlers pull it off `DispatchContext` and
forward to the underlying `AttachmentTools` methods.
"""

from __future__ import annotations

from typing import Any

from ..capabilities import Scope
from ..tools.attachments import AttachmentTools
from .base import DispatchContext, Skill, ToolSpec


SKILL_ID = "files"
SKILL_VERSION = "2"


SYSTEM_PROMPT_FRAGMENT = """\
## File attachments

Beyond the Markdown vault, you also have a **filevault** — a separate directory
for binary attachments (photos, documents, videos, voice notes, etc.) the user
sends through Telegram. It has its own tools:

- `save_attachment` — commits the user's most recent attachment. Use this when
  the user asks to keep a file ("save this", "저장해줘", "keep this for later")
  in natural language. You can optionally rename the file, set a caption, or
  write a richer description — the caption and description are indexed for
  semantic search, so a thoughtful description helps you find the file later.
  Prefer to capture any context the user just gave about the file (e.g. "this
  is the whiteboard from our standup on Tuesday") as the `description`.
  The `/save` Telegram command handles the same thing without asking you.
- `search_files` — semantic search over saved files using Voyage's multimodal
  embeddings. Use this when the user wants to find a file by meaning — "the
  whiteboard photo from standup", "that receipt from last week", "the voice
  note about the trip". Returns paths + metadata, not the file bytes.
- `retrieve_attachment` — send a saved file back to the user through Telegram.
  Use this when the user explicitly asks you to send them a file. The `path`
  argument comes from `search_files` — do not invent paths.

Each saved file also gets a Markdown breadcrumb in the Obsidian vault at
`files/YYYY-MM-DD/<filename>.md` with frontmatter metadata. This means
`vault_search` will also find references to attachments — useful when the user
mixes text and file-based recall.

For albums or plural requests like "save these images/files", use
`save_attachments`, not repeated `save_attachment` calls. If the current user
message lists pending attachment ids or a media_group_id, pass those values so
old pending files are not accidentally saved.

For requests to OCR, transcribe, read, or extract prompts/text from images, use
`extract_attachment_text`. Do not ask for `/save` first; pending Telegram images
can be read directly.

Don't save files the user hasn't explicitly asked you to save. If an attachment
is pending and the user's intent is unclear, ask.
"""


_SCHEMAS: dict[str, dict[str, Any]] = {
    "save_attachment": {
        "name": "save_attachment",
        "description": (
            "Commit the most recently received attachment (photo, document, video, "
            "voice note, audio, or animation) to the filevault. Use this when the "
            "user asks to save/keep a file they just sent without using the /save "
            "command — e.g. 'save this', '저장해줘', 'keep this for later'. "
            "You can optionally rename the file and attach a caption/description; "
            "both are searchable later via `search_files`. Fails if no recent "
            "attachment is pending for this chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination_name": {
                    "type": "string",
                    "description": (
                        "Optional new filename (with or without extension). If "
                        "omitted, uses Telegram's original filename or a date-stamped "
                        "name for captures with no name (photos, voice notes)."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "Short caption for the file — displayed in the Obsidian "
                        "breadcrumb and used as a signal in semantic search."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Longer freeform description. Include context the user "
                        "gave about the file — what it's for, when it was taken, "
                        "why it matters. Improves semantic retrieval."
                    ),
                },
            },
            "required": [],
        },
    },
    "save_attachments": {
        "name": "save_attachments",
        "description": (
            "Commit multiple pending attachments to the filevault. Use this for "
            "Telegram albums or plural requests like 'save these images/files'. "
            "When the user message lists pending_ids or media_group_id, pass them "
            "to keep the operation scoped to the current upload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pending_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Pending attachment ids from the current user message. "
                        "Optional, but preferred when provided."
                    ),
                },
                "media_group_id": {
                    "type": "string",
                    "description": "Telegram media_group_id for the current album.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional short caption to store on every saved file.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional context/notes to store on every saved file.",
                },
            },
            "required": [],
        },
    },
    "extract_attachment_text": {
        "name": "extract_attachment_text",
        "description": (
            "Extract visible text from pending or saved image attachments using "
            "vision. Use when the user asks to OCR, read, transcribe, or extract "
            "text/prompts from images. Pending Telegram images do not need to be "
            "saved first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pending_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Pending image ids from the current upload. Optional, "
                        "but preferred when the user message lists them."
                    ),
                },
                "media_group_id": {
                    "type": "string",
                    "description": "Telegram media_group_id for the current album.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Saved filevault-relative image paths to read.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The user's OCR/extraction instruction.",
                },
            },
            "required": [],
        },
    },
    "search_files": {
        "name": "search_files",
        "description": (
            "Semantic search over saved files (photos, documents, audio, etc.) "
            "in the filevault. Embeds the query with Voyage's multimodal model "
            "and returns the top-k closest files by cosine distance. Use this "
            "when the user asks to find a file by meaning — 'the whiteboard photo "
            "from standup', 'that PDF about taxes', 'the voice note from last week'. "
            "Returns file path, metadata, caption, and description — NOT the file "
            "itself. Use `retrieve_attachment` to send a matched file back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language description of what to find.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches (default 5, max 20).",
                },
            },
            "required": ["query"],
        },
    },
    "retrieve_attachment": {
        "name": "retrieve_attachment",
        "description": (
            "Send a saved file from the filevault back to the user as a Telegram "
            "document. Use this after `search_files` (or when the user references "
            "a specific saved file by name/path) and they've asked you to actually "
            "send it. The `path` argument is the filevault-relative path — e.g. "
            "'2026-04-21/whiteboard.jpg' — exactly as returned by `search_files`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Filevault-relative path of the file to send. Get this "
                        "from `search_files` results — do not guess."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "Optional caption for the Telegram message attached to "
                        "the file."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}


def build_files_skill(tools: AttachmentTools) -> Skill:
    """Wrap an `AttachmentTools` instance as a Skill.

    Handlers forward `chat_id` from the DispatchContext to the underlying
    methods; the rest of the inputs flow through unchanged.
    """

    async def _save(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.save_attachment(chat_id=ctx.chat_id, **inputs)

    async def _save_many(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.save_attachments(chat_id=ctx.chat_id, **inputs)

    async def _extract_text(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.extract_attachment_text(chat_id=ctx.chat_id, **inputs)

    async def _search(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.search_files(chat_id=ctx.chat_id, **inputs)

    async def _retrieve(inputs: dict[str, Any], ctx: DispatchContext) -> str:
        return await tools.retrieve_attachment(chat_id=ctx.chat_id, **inputs)

    specs: tuple[ToolSpec, ...] = (
        ToolSpec("save_attachment", Scope.FILEVAULT_WRITE, _SCHEMAS["save_attachment"], _save),
        ToolSpec("save_attachments", Scope.FILEVAULT_WRITE, _SCHEMAS["save_attachments"], _save_many),
        ToolSpec(
            "extract_attachment_text",
            Scope.FILEVAULT_READ,
            _SCHEMAS["extract_attachment_text"],
            _extract_text,
        ),
        ToolSpec("search_files", Scope.FILEVAULT_READ, _SCHEMAS["search_files"], _search),
        ToolSpec(
            "retrieve_attachment",
            Scope.FILEVAULT_SEND,
            _SCHEMAS["retrieve_attachment"],
            _retrieve,
        ),
    )
    return Skill(
        id=SKILL_ID,
        version=SKILL_VERSION,
        tools=specs,
        system_prompt_fragment=SYSTEM_PROMPT_FRAGMENT,
    )
