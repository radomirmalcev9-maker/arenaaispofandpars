"""Client for Arena's current Next.js Direct Chat data-stream protocol.

The browser page supplies a short-lived verification assertion for each
generation.  This module accepts that assertion from the caller, but never
attempts to create, replay, or solve one.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

from .constants import DEFAULT_BASE_URL, DEFAULT_QUEUE_TIMEOUT
from .exceptions import ArenaError, ModelUnavailableError, ProtocolError, QueueTimeoutError
from .models import GenerationConfig, Message, ModelInfo, StreamChunk
from .transport import ArenaTransport, AsyncArenaTransport, _raise_for_status

DIRECT_PAGE_PATH = "/text/direct?model_a=max"
DIRECT_STREAM_PATH = "/nextjs-api/stream/create-evaluation"
VerificationToken = str | Callable[[], str]

# The initial model list is rendered as a JSON value inside Next's escaped
# flight payload.  Matching only the stable fields tolerates unrelated UI data.
_MODEL_RE = re.compile(
    r'\\"id\\":\\"(?P<id>[0-9a-f-]{36})\\".*?'
    r'\\"publicName\\":\\"(?P<name>(?:[^"\\\\]|\\\\.)*)\\"',
    re.DOTALL,
)


def uuid7() -> str:
    """Generate a UUIDv7 on Python versions predating ``uuid.uuid7``."""
    millis = int(time.time_ns() // 1_000_000)
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


class AIDataStreamParser:
    """Incremental decoder for Vercel AI data-stream records (``a0:``, ``ad:``).

    Records are newline delimited rather than SSE records; arbitrary byte
    boundaries and malformed nonessential metadata are handled safely.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, wire: bytes | str) -> list[tuple[str, Any]]:
        self._buffer += wire.decode("utf-8", "replace") if isinstance(wire, bytes) else wire
        events: list[tuple[str, Any]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            parsed = self._line(line.rstrip("\r"))
            if parsed is not None:
                events.append(parsed)
        return events

    def close(self) -> list[tuple[str, Any]]:
        line, self._buffer = self._buffer, ""
        parsed = self._line(line.rstrip("\r"))
        return [] if parsed is None else [parsed]

    @staticmethod
    def _line(line: str) -> tuple[str, Any] | None:
        if not line:
            return None
        kind, colon, payload = line.partition(":")
        if not colon:
            return ("unknown", line)
        try:
            return kind, json.loads(payload)
        except json.JSONDecodeError:
            return kind, payload


def _models_from_direct_page(page: str) -> list[ModelInfo]:
    result: dict[str, ModelInfo] = {}
    for match in _MODEL_RE.finditer(page):
        name = json.loads('"' + match.group("name") + '"')
        model_id = match.group("id")
        result[model_id] = ModelInfo(id=model_id, display_name=name, metadata={"public_name": name})
    if not result:
        raise ProtocolError("Could not locate initialModels in Arena's Direct Chat page.")
    return sorted(result.values(), key=lambda item: (item.display_name or item.id).casefold())


class _DirectMixin:
    verification_token: VerificationToken | None
    _models: list[ModelInfo] | None

    def _token(self, supplied: str | None) -> str:
        token = supplied if supplied is not None else self.verification_token
        token = token() if callable(token) else token
        if not isinstance(token, str) or not token.strip():
            raise ProtocolError(
                "Arena Direct Chat requires a fresh browser-provided verification token for this request."
            )
        return token

    @staticmethod
    def _request_body(prompt: str, model_id: str, token: str, conversation_id: str | None) -> dict[str, Any]:
        chat_id = conversation_id or uuid7()
        return {
            "id": chat_id,
            "mode": "direct-battle",
            "modelAId": model_id,
            "userMessageId": uuid7(),
            "modelAMessageId": uuid7(),
            "userMessage": {"content": prompt, "experimental_attachments": [], "metadata": {}},
            "modality": "chat",
            "recaptchaV3Token": token,
        }

    def _resolve_model(self, model: str | None, known: list[ModelInfo]) -> str:
        if model is None:
            raise ModelUnavailableError("Pass a model id or a model public name from get_models().")
        for item in known:
            if model in (item.id, item.display_name) or model.casefold() == (item.display_name or "").casefold():
                return item.id
        raise ModelUnavailableError("Model %r is not present in the current Direct Chat model list." % model)


class DirectArenaClient(_DirectMixin):
    """Blocking client for the current Arena Direct Chat page.

    ``verification_token`` may be a string or a callback returning a new token.
    It is intentionally never persisted by this class.
    """

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, cookies: Any = None,
                 headers: Mapping[str, str] | None = None,
                 verification_token: VerificationToken | None = None,
                 impersonate: str = "chrome124") -> None:
        self.transport = ArenaTransport(base_url, cookies=cookies, headers=headers, impersonate=impersonate)
        self.verification_token = verification_token
        self._models = None
        self.conversation_id: str | None = None

    def __enter__(self) -> "DirectArenaClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def get_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        if refresh or self._models is None:
            self._models = _models_from_direct_page(self.transport.get_text(DIRECT_PAGE_PATH))
        return list(self._models)

    def stream(self, prompt: str, *, model: str | None = None,
               verification_token: str | None = None,
               config: GenerationConfig | None = None) -> Iterator[StreamChunk]:
        config = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        model_id = self._resolve_model(model, self.get_models())
        body = self._request_body(prompt, model_id, self._token(verification_token), self.conversation_id)
        self.conversation_id = body["id"]
        last_activity, text = time.monotonic(), ""
        encoded = json.dumps(body, separators=(",", ":"))
        with self.transport.session.stream(
            "POST", self.transport.url(DIRECT_STREAM_PATH), data=encoded,
            headers={"content-type": "text/plain;charset=UTF-8", "accept": "text/plain, */*"},
            timeout=config.queue_timeout,
        ) as response:
            _raise_for_status(response.status_code, response.text)
            parser = AIDataStreamParser()
            for wire in response.iter_content(chunk_size=1024):
                if time.monotonic() - last_activity > config.queue_timeout:
                    raise QueueTimeoutError("No Direct Chat stream data received before timeout.")
                if not wire:
                    continue
                last_activity = time.monotonic()
                for kind, value in parser.feed(wire):
                    if kind == "a0" and isinstance(value, str):
                        text += value
                        yield StreamChunk(text=text, delta=value, event="text", raw={kind: value})
                    elif kind == "ad":
                        yield StreamChunk(text=text, delta="", event="completed", finished=True, raw={kind: value})
                        return
            for kind, value in parser.close():
                if kind == "a0" and isinstance(value, str):
                    text += value
                    yield StreamChunk(text=text, delta=value, event="text", raw={kind: value})
                elif kind == "ad":
                    yield StreamChunk(text=text, delta="", event="completed", finished=True, raw={kind: value})
                    return
        raise ProtocolError("Arena Direct Chat stream ended without a completion record.")

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return "".join(chunk.delta for chunk in self.stream(prompt, **kwargs))

    def chat(self, *, model: str, config: GenerationConfig | None = None) -> "DirectChatSession":
        return DirectChatSession(self, model=model, config=config)


class DirectChatSession:
    """Client-side multi-turn state for Direct Chat.

    Arena owns the opaque conversation id; this class additionally maintains a
    typed local transcript suitable for applications and retries.
    """

    def __init__(self, client: DirectArenaClient, *, model: str,
                 config: GenerationConfig | None = None) -> None:
        self.client = client
        self.model = model
        self.config = config or GenerationConfig()
        self.history: list[Message] = []
        self.turn_id = 0
        self.parent_message_id: str | None = None

    def stream_message(self, content: str, *, verification_token: str | None = None) -> Iterator[StreamChunk]:
        user = Message("user", content, uuid7())
        self.history.append(user)
        collected: list[str] = []
        try:
            for chunk in self.client.stream(content, model=self.model,
                                            verification_token=verification_token, config=self.config):
                if chunk.delta:
                    collected.append(chunk.delta)
                yield chunk
        except Exception:
            self.history.pop()
            raise
        assistant = Message("assistant", "".join(collected), uuid7())
        self.history.append(assistant)
        self.turn_id += 1
        self.parent_message_id = assistant.id

    def send_message(self, content: str, *, verification_token: str | None = None) -> str:
        return "".join(chunk.delta for chunk in self.stream_message(content, verification_token=verification_token))

    def clear(self) -> None:
        self.history.clear()
        self.turn_id = 0
        self.parent_message_id = None
        self.client.conversation_id = None


class AsyncDirectArenaClient(_DirectMixin):
    """Async equivalent of :class:`DirectArenaClient`."""

    def __init__(self, **kwargs: Any) -> None:
        base_url = kwargs.pop("base_url", DEFAULT_BASE_URL)
        self.verification_token = kwargs.pop("verification_token", None)
        self.transport = AsyncArenaTransport(base_url, **kwargs)
        self._models = None
        self.conversation_id: str | None = None

    async def __aenter__(self) -> "AsyncDirectArenaClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.transport.close()

    async def get_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        if refresh or self._models is None:
            self._models = _models_from_direct_page(await self.transport.get_text(DIRECT_PAGE_PATH))
        return list(self._models)

    async def astream(self, prompt: str, *, model: str | None = None,
                      verification_token: str | None = None,
                      config: GenerationConfig | None = None) -> AsyncIterator[StreamChunk]:
        config = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        model_id = self._resolve_model(model, await self.get_models())
        body = self._request_body(prompt, model_id, self._token(verification_token), self.conversation_id)
        self.conversation_id = body["id"]
        parser, text, last_activity = AIDataStreamParser(), "", time.monotonic()
        encoded = json.dumps(body, separators=(",", ":"))
        async with self.transport.session.stream(
            "POST", self.transport.url(DIRECT_STREAM_PATH), data=encoded,
            headers={"content-type": "text/plain;charset=UTF-8", "accept": "text/plain, */*"},
            timeout=config.queue_timeout,
        ) as response:
            _raise_for_status(response.status_code, response.text)
            async for wire in response.aiter_content(chunk_size=1024):
                if time.monotonic() - last_activity > config.queue_timeout:
                    raise QueueTimeoutError("No Direct Chat stream data received before timeout.")
                if not wire:
                    continue
                last_activity = time.monotonic()
                for kind, value in parser.feed(wire):
                    if kind == "a0" and isinstance(value, str):
                        text += value
                        yield StreamChunk(text=text, delta=value, event="text", raw={kind: value})
                    elif kind == "ad":
                        yield StreamChunk(text=text, delta="", event="completed", finished=True, raw={kind: value})
                        return
        raise ProtocolError("Arena Direct Chat stream ended without a completion record.")

    async def acomplete(self, prompt: str, **kwargs: Any) -> str:
        return "".join([part.delta async for part in self.astream(prompt, **kwargs)])
