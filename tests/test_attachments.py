"""Attachment builders and the Direct Chat request body they land in."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from arena_api import Attachment, DirectArenaClient, from_bytes, from_path

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPEG_HEADER = b"\xff\xd8\xff" + b"\x00" * 100

MODEL_ID = "12345678-1234-1234-1234-123456789abc"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_from_path_sniffs_mime_and_inlines_data_url(tmp_path: Path) -> None:
    p = _write(tmp_path, "cat.png", PNG_HEADER)
    att = from_path(p)
    assert att.name == "cat.png"
    assert att.content_type == "image/png"
    assert att.size == len(PNG_HEADER)
    assert att.url.startswith("data:image/png;base64,")
    assert att.to_ai_sdk() == {
        "name": "cat.png",
        "contentType": "image/png",
        "url": att.url,
        "size": att.size,
    }


def test_from_path_enforces_15mb_limit(tmp_path: Path) -> None:
    p = _write(tmp_path, "big.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (16 * 1024 * 1024))
    with pytest.raises(ValueError, match="15 MB"):
        from_path(p)


def test_from_bytes_override_content_type() -> None:
    att = from_bytes(b"hello", "note.txt", content_type="text/plain")
    assert att.content_type == "text/plain"
    assert att.url == "data:text/plain;base64," + "aGVsbG8=".encode().decode()


def test_from_url_references_existing_upload() -> None:
    from arena_api.attachments import from_url

    a = from_url("https://storage.example/files/cat.png", "cat.png", "image/png", 1234)
    assert a.url == "https://storage.example/files/cat.png"
    assert a.size == 1234
    assert a.to_ai_sdk()["size"] == 1234


def test_from_url_oversize_is_rejected() -> None:
    from arena_api.attachments import from_url

    with pytest.raises(ValueError, match="15 MB"):
        from_url("https://storage.example/files/big.bin", "big.bin", "application/octet-stream", 20 * 1024 * 1024)


def test_from_base64_round_trips() -> None:
    from arena_api.attachments import from_base64

    a = from_base64("aGVsbG8=", "hello.txt", "text/plain")
    assert a.size == 5
    assert a.content_type == "text/plain"
    assert a.url == "data:text/plain;base64,aGVsbG8="


def test_invalid_base64_is_rejected() -> None:
    from arena_api.attachments import from_base64

    with pytest.raises(ValueError, match="not valid base64"):
        from_base64("not@@@base64!!", "bad.txt", "text/plain")


def test_sync_direct_sends_attachments_in_request_body() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nextjs-api/stream/create-evaluation":
            calls.append(json.loads(request.content))
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/plain"},
                content=b'ad:{"finishReason":"stop"}\n',
            )
        raise AssertionError(request.url)

    attachments = [
        Attachment(
            name="photo.png",
            content_type="image/png",
            url="data:image/png;base64,iVBORw0KGgo",
            size=10,
        )
    ]
    client = DirectArenaClient(
        base_url="https://arena.test",
        verification_token="caller-supplied-token",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        client.complete("what is this?", attachments=attachments, model=MODEL_ID)
    finally:
        client.close()
    assert len(calls) == 1
    assert calls[0]["userMessage"]["experimental_attachments"] == [
        {
            "name": "photo.png",
            "contentType": "image/png",
            "url": "data:image/png;base64,iVBORw0KGgo",
            "size": 10,
        }
    ]


def test_sync_direct_defaults_attachments_to_empty_list() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b'ad:{"finishReason":"stop"}\n',
        )

    client = DirectArenaClient(
        base_url="https://arena.test",
        verification_token="caller-supplied-token",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        client.complete("plain text", model=MODEL_ID)
    finally:
        client.close()
    assert calls[0]["userMessage"]["experimental_attachments"] == []
