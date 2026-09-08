"""Route tool-produced file attachments to the response, never to the LLM.

Tools return base64 files on an `attachments` list (ToolAttachment contract).
Feeding that b64 back to the model bloats context for no benefit, so:
- strip_attachments (after_tool_callback): validate, stash FilePart copies in
  session state (ATTACHMENTS_STATE_KEY), return a cleaned response where b64 is
  swapped for a per-file summary (model still sees which files, never bytes).
- collect_attachments: read accumulated files back out post-run; to_artifact
  packages them as one "Generated Reports" A2A artifact for build_response.

mediaType derived from filename ext (tool's contentType label, e.g. "Excel", is
not a valid A2A media type).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid7

from google.adk.tools import BaseTool, ToolContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Session-state key under which stripped attachments accumulate across tool calls.
ATTACHMENTS_STATE_KEY = "generated_attachments"

# The field on a tool response that carries the base64 files.
_ATTACHMENTS_FIELD = "attachments"

# File extension -> A2A short mediaType (the supported set from the contract).
_EXT_TO_MEDIA_TYPE = {
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".csv": "csv",
    ".txt": "txt",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".xml": "xml",
    ".zip": "zip",
}


class ToolAttachment(BaseModel):
    """Input contract: one file a tool emits on its `attachments` list.

    contentType is advisory; mediaType is derived from the ext. extra="ignore"
    so a richer tool object still validates.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    file_name: str = Field(alias="fileName", min_length=1)
    content: str = Field(min_length=1)  # base64-encoded bytes
    content_type: str = Field(default="", alias="contentType")


class FilePart(BaseModel):
    """Output contract: one A2A FilePart on the outgoing artifact.

    Dump with model_dump(by_alias=True) for wire keys {raw, filename, mediaType}.
    """

    model_config = ConfigDict(populate_by_name=True)

    raw: str  # base64-encoded bytes
    filename: str
    media_type: str = Field(alias="mediaType")


def _media_type(filename: str) -> str:
    """Derive the A2A short mediaType from a filename's extension."""
    ext = os.path.splitext(filename)[1].lower()
    return _EXT_TO_MEDIA_TYPE.get(ext, ext.lstrip(".") or "txt")


def _normalise(item: Any) -> dict[str, str] | None:
    """Validate one raw attachment -> FilePart dict; None (skip) if malformed."""
    try:
        att = ToolAttachment.model_validate(item)
    except ValidationError:
        return None
    part = FilePart.model_validate(
        {"raw": att.content, "filename": att.file_name, "mediaType": _media_type(att.file_name)}
    )
    return part.model_dump(by_alias=True)


def strip_attachments(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: dict
) -> dict | None:
    """after_tool_callback: move b64 files out of the model's context.

    Stashes files in session state; returns a response with each b64 swapped for
    a {fileName, mediaType, status} summary. None (no change) if no attachments.
    """
    if not isinstance(tool_response, dict) or _ATTACHMENTS_FIELD not in tool_response:
        return None

    files = [
        f for f in (_normalise(i) for i in (tool_response.get(_ATTACHMENTS_FIELD) or [])) if f
    ]
    cleaned = dict(tool_response)

    if files:
        # Reassign (rather than mutate in place) so the State delta is recorded.
        stored = list(tool_context.state.get(ATTACHMENTS_STATE_KEY, []))
        stored.extend(files)
        tool_context.state[ATTACHMENTS_STATE_KEY] = stored

    # Always drop the base64 from what the model sees; leave a light summary.
    cleaned[_ATTACHMENTS_FIELD] = [
        {
            "fileName": f["filename"],
            "mediaType": f["mediaType"],
            "status": "stored_for_delivery",
        }
        for f in files
    ]
    return cleaned


def collect_attachments(state: dict[str, Any]) -> list[dict[str, str]]:
    """Return the FilePart dicts accumulated in a finished session's state."""
    return list(state.get(ATTACHMENTS_STATE_KEY, []))


def to_artifact(files: list[dict[str, str]]) -> dict[str, Any] | None:
    """Package collected files as one "Generated Reports" artifact, or ``None``."""
    parts = [
        FilePart.model_validate(f).model_dump(by_alias=True)
        for f in files
        if f.get("raw") and f.get("filename")
    ]
    if not parts:
        return None
    return {"artifactId": str(uuid7()), "name": "Generated Reports", "parts": parts}
