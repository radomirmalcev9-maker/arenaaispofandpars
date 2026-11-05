"""File attachments (photos, images, documents) for Arena Direct Chat.

Emits attachments in the Vercel AI SDK shape that Direct Chat already accepts
through ``userMessage.experimental_attachments``::

    {"name", "contentType", "url", "size"}

By default a local file is read and sent as an inline ``data:`` URL (base64),
which works with the AI SDK without any storage endpoint.  The composer popover
the user pasted advertises ``"Up to 15MB per file"``; builders enforce that cap.

If you already upload files to your own storage, pass the public URL through
:func:`from_url` instead of inlining the bytes.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: UI copy from the Arena composer popover: ``"Up to 15MB per file"``.
ATTACHMENT_MAX_SIZE_BYTES = 15 * 1024 * 1024


def _sniff_mime(data: bytes, name: str) -> str:
    """Best-effort MIME type using stdlib only (extension, then magic bytes)."""

    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


@dataclass(frozen=True, slots=True)
class Attachment:
    """One file attached to a user message (Vercel AI SDK shape).

    ``url`` is either an inline ``data:`` URL (default, built from local bytes)
    or a public URL of a file you uploaded yourself.
    """

    name: str
    content_type: str
    url: str
    size: int

    def to_ai_sdk(self) -> dict[str, Any]:
        """Return the JSON object Direct Chat expects for one attachment."""
        return {
            "name": self.name,
            "contentType": self.content_type,
            "url": self.url,
            "size": self.size,
        }


def from_path(path: str | Path) -> Attachment:
    """Read a local file and wrap it as an inline ``data:`` URL attachment.

    Raises :class:`ValueError` when the file exceeds the 15 MB per-file limit.
    """

    data = Path(path).read_bytes()
    return from_bytes(data, Path(path).name)


def from_bytes(
    data: bytes,
    name: str,
    content_type: str | None = None,
) -> Attachment:
    """Wrap raw bytes as an inline ``data:`` URL attachment.

    Raises :class:`ValueError` when the payload exceeds the 15 MB per-file
    limit.
    """

    if content_type is None:
        content_type = _sniff_mime(data, name)
    size = len(data)
    if size > ATTACHMENT_MAX_SIZE_BYTES:
        raise ValueError(
            f"attachment {name!r} is {size} bytes, over the "
            f"{ATTACHMENT_MAX_SIZE_BYTES} byte (15 MB) per-file limit"
        )
    b64 = base64.b64encode(data).decode("ascii")
    url = f"data:{content_type};base64,{b64}"
    return Attachment(name=name, content_type=content_type, url=url, size=size)


def from_url(url: str, name: str, content_type: str, size: int | None = None) -> Attachment:
    """Wrap an already-uploaded file's public URL as an attachment.

    Use this when you upload files to your own storage and only want to
    reference them in Direct Chat.  ``size`` is validated against the 15 MB
    per-file limit when provided.
    """

    if size is not None and size > ATTACHMENT_MAX_SIZE_BYTES:
        raise ValueError(
            f"attachment {name!r} is {size} bytes, over the "
            f"{ATTACHMENT_MAX_SIZE_BYTES} byte (15 MB) per-file limit"
        )
    return Attachment(
        name=name,
        content_type=content_type,
        url=url,
        size=size if size is not None else 0,
    )


def from_base64(b64: str, name: str, content_type: str) -> Attachment:
    """Wrap a base64 payload (without the ``data:`` prefix) as an attachment."""

    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise ValueError(f"attachment {name!r} is not valid base64") from exc
    return from_bytes(data, name, content_type)


__all__ = [
    "ATTACHMENT_MAX_SIZE_BYTES",
    "Attachment",
    "from_base64",
    "from_bytes",
    "from_path",
    "from_url",
]
