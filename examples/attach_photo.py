"""Attach a photo (or any file) to a Direct Chat prompt.

Usage::

    python examples/attach_photo.py "Что на этой фотографии?" ./photo.jpg

Files larger than 15 MB raise ``ValueError`` (the same cap the Arena composer
advertises: ``"Up to 15MB per file"``). Each attachment is sent inline as a
``data:`` URL in ``userMessage.experimental_attachments``, matching the
Vercel AI SDK shape Direct Chat already accepts.
"""

from __future__ import annotations

import sys
from pathlib import Path

from arena_api import Attachment, DirectArenaClient, GenerationConfig, from_path


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[1])
        return 2
    prompt, *files = argv[1:]
    attachments = [from_path(f) for f in files]
    for attachment in attachments:
        print(
            f"attach {attachment.name} ({attachment.content_type}, "
            f"{attachment.size} bytes) -> {attachment.url[:40]}..."
        )
    client = DirectArenaClient(verification_token="auto")
    try:
        answer = client.complete(
            prompt,
            attachments=attachments,
            config=GenerationConfig(),
        )
        print(answer)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
