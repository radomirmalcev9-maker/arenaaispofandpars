from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from arena_api import (
    AsyncDirectArenaClient,
    DirectArenaClient,
    GenerationConfig,
    ProtocolError,
)
from arena_api.direct import AIDataStreamParser, _models_from_markup

MODEL_ID = "12345678-1234-1234-1234-123456789abc"
PAGE = (
    '<html><script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {
            "props": {
                "pageProps": {"models": [{"id": MODEL_ID, "publicName": "Model A"}]}
            }
        }
    )
    + "</script></html>"
)


class AsyncMockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self.handler(request)


def test_ai_data_parser_handles_split_utf8_and_final_record() -> None:
    parser = AIDataStreamParser()
    wire = 'a0:"Привет"\na0:" мир"\nad:{"finishReason":"stop"}'
    result = []
    for byte in wire.encode():
        result.extend(parser.feed(bytes([byte])))
    result.extend(parser.close())
    assert result == [
        ("a0", "Привет"),
        ("a0", " мир"),
        ("ad", {"finishReason": "stop"}),
    ]


def test_model_parser_handles_json_scripts_and_escaped_flight_data() -> None:
    assert _models_from_markup(PAGE)[0].display_name == "Model A"
    escaped = (
        r"{\"id\":\"12345678-1234-1234-1234-123456789abc\",\"publicName\":\"Model B\"}"
    )
    models = _models_from_markup(escaped)
    assert models[0].display_name == "Model B"
    flight_payload = json.dumps(json.dumps({"id": MODEL_ID, "publicName": "Model C"}))
    flight_page = f"<script>self.__next_f.push([1,{flight_payload}]);</script>"
    assert _models_from_markup(flight_page)[0].display_name == "Model C"


def test_sync_direct_stream_uses_supplied_token_and_isolates_chat_id() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/direct":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content=PAGE.encode(),
            )
        if request.url.path == "/nextjs-api/stream/create-evaluation":
            payload = json.loads(request.content)
            calls.append(payload)
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/plain"},
                content=b'a0:"Hello"\na0:" there"\nad:{"finishReason":"stop"}\n',
            )
        raise AssertionError(request.url)

    client = DirectArenaClient(
        base_url="https://arena.test",
        verification_token="caller-supplied-token",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        chat_one = client.chat(model="Model A")
        assert chat_one.send_message("Hi") == "Hello there"
        assert chat_one.turn_id == 1
        assert client.chat(model="Model A").conversation_id is None
        assert calls[0]["recaptchaV3Token"] == "caller-supplied-token"
        assert uuid.UUID(calls[0]["id"]).version == 7
    finally:
        client.close()


def test_direct_stream_requires_a_completion_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/direct":
            return httpx.Response(
                200,
                request=request,
                content=PAGE.encode(),
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/nextjs-api/stream/create-evaluation":
            return httpx.Response(
                200,
                request=request,
                content=b'a0:"partial"\n',
                headers={"content-type": "text/plain"},
            )
        raise AssertionError(request.url)

    client = DirectArenaClient(
        base_url="https://arena.test",
        verification_token="provided-token",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        try:
            list(client.stream("Hi", model="Model A"))
        except ProtocolError as exc:
            assert "completion" in str(exc)
        else:
            raise AssertionError("an unterminated stream should fail")
    finally:
        client.close()


def test_async_direct_stream_and_session() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text/direct":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content=PAGE.encode(),
            )
        if request.url.path == "/nextjs-api/stream/create-evaluation":
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/plain"},
                content=b'0:"Async answer"\nd:{"finishReason":"stop"}\n',
            )
        raise AssertionError(request.url)

    async def run() -> None:
        client = AsyncDirectArenaClient(
            base_url="https://arena.test",
            verification_token=lambda: "fresh-caller-value",
            http_transport=AsyncMockTransport(handler),
        )
        async with client:
            chat = client.chat(
                model="Model A", config=GenerationConfig(temperature=0.3)
            )
            assert await chat.send_message("Hi") == "Async answer"
            assert chat.turn_id == 1
            assert len(chat.history) == 2

    asyncio.run(run())
