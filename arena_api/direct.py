"""Client for the Arena Next.js Direct Chat streaming frontend.

These routes are undocumented and can change at any time. This adapter accepts
only verification assertions explicitly supplied by the caller.
"""

from __future__ import annotations

import codecs
import html
import json
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from html.parser import HTMLParser
from typing import Any

import httpx

from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_QUEUE_TIMEOUT,
    DEFAULT_TIMEOUT,
    DIRECT_PAGE_PATH,
    DIRECT_STREAM_PATH,
)
from .exceptions import ModelUnavailableError, ProtocolError, QueueTimeoutError
from .models import GenerationConfig, Message, ModelInfo, StreamChunk
from .transport import (
    ArenaTransport,
    AsyncArenaTransport,
    CookieInput,
    SSEParser,
    _raise_if_challenge_page,
)

VerificationToken = str | Callable[[], str]

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_FIELD_QUOTE = r"(?:\\+)?\""
_ID_FIELD_RE = re.compile(
    rf"{_FIELD_QUOTE}(?:id|modelId|model_id){_FIELD_QUOTE}\s*:\s*"
    rf"{_FIELD_QUOTE}(?P<value>{_UUID}){_FIELD_QUOTE}",
    re.IGNORECASE,
)
_NAME_FIELD_RE = re.compile(
    rf"{_FIELD_QUOTE}(?:publicName|public_name|displayName|display_name|name){_FIELD_QUOTE}\s*:\s*"
    rf"{_FIELD_QUOTE}(?P<value>(?:\\.|[^\"\\])*){_FIELD_QUOTE}",
    re.IGNORECASE,
)
_NEXT_FLIGHT_RE = re.compile(
    r"__next_f\.push\(\[\s*\d+\s*,\s*(\"(?:\\.|[^\"\\])*\")\s*\]\)",
    re.DOTALL,
)


def uuid7() -> str:
    """Generate a standards-shaped UUIDv7 on Python versions before 3.14."""
    millis = time.time_ns() // 1_000_000
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


class AIDataStreamParser:
    """Incremental decoder for newline-delimited AI data-stream records.

    Arena has used prefixed records such as ``a0:`` and ``ad:``; the parser
    also accepts the Vercel AI SDK's common ``0:`` text and ``d:`` finish
    records. UTF-8 sequences split between network reads are preserved.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._closed = False

    def feed(self, wire: bytes | str) -> list[tuple[str, Any]]:
        if self._closed:
            raise RuntimeError("cannot feed a closed AIDataStreamParser")
        self._buffer += (
            self._decoder.decode(wire, final=False) if isinstance(wire, bytes) else wire
        )
        events: list[tuple[str, Any]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            record = self._parse_line(line.rstrip("\r"))
            if record is not None:
                events.append(record)
        return events

    def close(self) -> list[tuple[str, Any]]:
        if self._closed:
            return []
        self._buffer += self._decoder.decode(b"", final=True)
        events: list[tuple[str, Any]] = []
        if self._buffer:
            record = self._parse_line(self._buffer.rstrip("\r"))
            if record is not None:
                events.append(record)
        self._buffer = ""
        self._closed = True
        return events

    @staticmethod
    def _parse_line(line: str) -> tuple[str, Any] | None:
        if not line:
            return None
        if line.strip() == "[DONE]":
            return "ad", True
        kind, separator, payload = line.partition(":")
        if not separator:
            return "unknown", line
        try:
            return kind, json.loads(payload)
        except json.JSONDecodeError:
            return kind, payload


class _ScriptCollector(HTMLParser):
    """Collect script element bodies without requiring an HTML dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._current is not None:
            self.scripts.append("".join(self._current))
            self._current = None


def _walk_models(value: Any, models: dict[str, ModelInfo]) -> None:
    if isinstance(value, Mapping):
        model_id = next(
            (
                value.get(key)
                for key in ("id", "modelId", "model_id")
                if value.get(key) is not None
            ),
            None,
        )
        display = next(
            (
                value.get(key)
                for key in (
                    "publicName",
                    "public_name",
                    "displayName",
                    "display_name",
                    "name",
                )
                if value.get(key) is not None
            ),
            None,
        )
        if model_id is not None and display is not None:
            model_id = str(model_id)
            models.setdefault(
                model_id,
                ModelInfo(
                    id=model_id,
                    display_name=str(display),
                    available=bool(value.get("available", value.get("active", True))),
                    metadata=dict(value),
                ),
            )
        for child in value.values():
            _walk_models(child, models)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _walk_models(child, models)


def _decode_json_string(value: str) -> str:
    try:
        return str(json.loads('"' + value + '"'))
    except (json.JSONDecodeError, TypeError):
        return html.unescape(value.replace(r"\"", '"').replace(r"\\", "\\"))


def _models_from_markup(markup: str) -> list[ModelInfo]:
    """Extract model records from Next.js JSON and escaped flight payloads."""
    result: dict[str, ModelInfo] = {}
    collector = _ScriptCollector()
    collector.feed(markup)

    candidates = [markup, html.unescape(markup), *collector.scripts]
    for match in _NEXT_FLIGHT_RE.finditer(markup):
        try:
            candidates.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue

    decoder = json.JSONDecoder()
    for candidate in candidates:
        _walk_models(_try_json(candidate), result)
        # Next.js may serialize model objects inside a string rather than a
        # standalone JSON script; scan object starts without relying on a fixed
        # React/Next payload version.
        position = 0
        while True:
            start = candidate.find("{", position)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(candidate, start)
            except json.JSONDecodeError:
                position = start + 1
            else:
                _walk_models(value, result)
                position = max(end, start + 1)

    # Use regex only as a last-resort fallback; parsing JSON first avoids a
    # loosely matched escaped string overriding a structured model record.
    for candidate in candidates:
        _regex_models(candidate, result)

    return sorted(
        result.values(), key=lambda item: (item.display_name or item.id).casefold()
    )


def _try_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _regex_models(candidate: str, result: dict[str, ModelInfo]) -> None:
    for id_match in _ID_FIELD_RE.finditer(candidate):
        next_id = _ID_FIELD_RE.search(candidate, id_match.end())
        limit = (
            next_id.start()
            if next_id is not None
            else min(len(candidate), id_match.end() + 1200)
        )
        segment = candidate[id_match.end() : limit]
        name_match = _NAME_FIELD_RE.search(segment)
        if name_match is None:
            # Some serializers place the public name before the UUID.
            previous_id = candidate.rfind(
                '"id"', max(0, id_match.start() - 1200), id_match.start()
            )
            before = candidate[
                max(previous_id, id_match.start() - 1200) : id_match.start()
            ]
            name_match = None
            for candidate_name in _NAME_FIELD_RE.finditer(before):
                name_match = candidate_name
        if name_match is None:
            continue
        name = _decode_json_string(name_match.group("value"))
        model_id = id_match.group("value")
        result.setdefault(
            model_id,
            ModelInfo(id=model_id, display_name=name, metadata={"public_name": name}),
        )


def _is_text_frame(kind: str) -> bool:
    return kind in {"0", "a0", "text", "text_delta", "text-delta", "message"}


def _is_done_frame(kind: str, value: Any) -> bool:
    if kind in {
        "ad",
        "d",
        "done",
        "finish",
        "finished",
        "complete",
        "completed",
        "process_completed",
    }:
        return True
    if isinstance(value, Mapping):
        if value.get("finished") is True or value.get("done") is True:
            return True
        return str(value.get("type", "")).casefold() in {
            "finish",
            "finished",
            "completed",
            "message_end",
            "finish_message",
        }
    return False


def _update_direct_text(
    kind: str, value: Any, previous: str
) -> tuple[str, StreamChunk | None, bool]:
    if kind in {"3", "error"} or (isinstance(value, Mapping) and value.get("error")):
        detail = value.get("error", value) if isinstance(value, Mapping) else value
        raise ProtocolError(f"Arena Direct Chat stream reported an error: {detail}")

    text_value: Any = value
    if isinstance(value, Mapping):
        text_value = next(
            (
                value.get(key)
                for key in ("delta", "text", "content")
                if isinstance(value.get(key), str)
            ),
            None,
        )
        if text_value is None and _is_text_frame(kind):
            text_value = value.get("value")
    if text_value is None and kind == "message" and isinstance(value, str):
        text_value = value

    current = previous
    chunk: StreamChunk | None = None
    if _is_text_frame(kind) and isinstance(text_value, str):
        current = (
            text_value if text_value.startswith(previous) else previous + text_value
        )
        delta = current[len(previous) :]
        if delta:
            chunk = StreamChunk(
                text=current, delta=delta, event="text", raw={kind: value}
            )
    finished = _is_done_frame(kind, value)
    if finished:
        chunk = StreamChunk(
            text=current, delta="", event="completed", finished=True, raw={kind: value}
        )
    return current, chunk, finished


def _records_from_sse_event(event_name: str, data: str) -> list[tuple[str, Any]]:
    if data.strip() == "[DONE]":
        return [("ad", True)]
    try:
        value: Any = json.loads(data)
    except json.JSONDecodeError:
        value = data
    if isinstance(value, Mapping):
        kind = str(value.get("type", event_name or "message"))
        if (
            "delta" in value
            or "text" in value
            or "content" in value
            or value.get("done")
        ):
            return [(kind, value)]
        if value.get("error"):
            return [("error", value)]
    return [(event_name or "message", value)]


def _check_direct_prefix(wire: bytes, response: httpx.Response) -> None:
    prefix = wire[:4096].decode("utf-8", "replace")
    if prefix.lstrip().lower().startswith(("<!doctype html", "<html")):
        _raise_if_challenge_page(prefix, response.headers)
        raise ProtocolError("Direct Chat expected a stream but received HTML.")


def _iter_response_records(response: httpx.Response) -> Iterator[tuple[str, Any]]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        sse_parser = SSEParser()
        first_chunk = True
        for wire in response.iter_bytes():
            if first_chunk:
                _check_direct_prefix(wire, response)
                first_chunk = False
            for event_name, data in sse_parser.feed(wire):
                yield from _records_from_sse_event(event_name, data)
        for event_name, data in sse_parser.close():
            yield from _records_from_sse_event(event_name, data)
        return
    ai_parser = AIDataStreamParser()
    first_chunk = True
    for wire in response.iter_bytes():
        if first_chunk:
            _check_direct_prefix(wire, response)
            first_chunk = False
        yield from ai_parser.feed(wire)
    yield from ai_parser.close()


async def _aiter_response_records(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, Any]]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        sse_parser = SSEParser()
        first_chunk = True
        async for wire in response.aiter_bytes():
            if first_chunk:
                _check_direct_prefix(wire, response)
                first_chunk = False
            for event_name, data in sse_parser.feed(wire):
                for item in _records_from_sse_event(event_name, data):
                    yield item
        for event_name, data in sse_parser.close():
            for item in _records_from_sse_event(event_name, data):
                yield item
        return
    ai_parser = AIDataStreamParser()
    first_chunk = True
    async for wire in response.aiter_bytes():
        if first_chunk:
            _check_direct_prefix(wire, response)
            first_chunk = False
        for item in ai_parser.feed(wire):
            yield item
    for item in ai_parser.close():
        yield item


class _DirectMixin:
    verification_token: VerificationToken | None
    _models: list[ModelInfo] | None
    conversation_id: str | None
    direct_page_path: str
    direct_stream_path: str

    def _token(self, supplied: str | None) -> str:
        token: VerificationToken | None = (
            supplied if supplied is not None else self.verification_token
        )
        if callable(token):
            token = token()
        if not isinstance(token, str) or not token.strip():
            raise ProtocolError(
                "Arena Direct Chat requires a fresh verification assertion supplied by the caller."
            )
        return token

    @staticmethod
    def _request_body(
        prompt: str,
        model_id: str,
        token: str,
        conversation_id: str,
        config: GenerationConfig,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": conversation_id,
            "mode": "direct-battle",
            "modelAId": model_id,
            "userMessageId": uuid7(),
            "modelAMessageId": uuid7(),
            "userMessage": {
                "content": prompt,
                "experimental_attachments": [],
                "metadata": {},
            },
            "modality": "chat",
            "recaptchaV3Token": token,
        }
        parameters = config.model_parameters()
        if parameters:
            body["generationConfig"] = parameters
        return body

    @staticmethod
    def _resolve_model(model: str | None, known: list[ModelInfo]) -> str:
        if model is None:
            raise ModelUnavailableError(
                "Pass a model id or display name returned by get_models()."
            )
        folded = model.casefold()
        for item in known:
            if (
                model == item.id
                or model == item.display_name
                or folded == (item.display_name or "").casefold()
            ):
                if not item.available:
                    raise ModelUnavailableError(
                        f"Model {model!r} is currently unavailable."
                    )
                return item.id
        raise ModelUnavailableError(
            f"Model {model!r} was not found in the current Direct Chat model list."
        )


class DirectArenaClient(_DirectMixin):
    """Blocking client for the current Arena Direct Chat frontend."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cookies: CookieInput = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http2: bool = True,
        verify: bool | str = True,
        verification_token: VerificationToken | None = None,
        direct_page_path: str = DIRECT_PAGE_PATH,
        direct_stream_path: str = DIRECT_STREAM_PATH,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.transport = ArenaTransport(
            base_url,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            http2=http2,
            verify=verify,
            http_transport=http_transport,
        )
        self.verification_token = verification_token
        self.direct_page_path = direct_page_path
        self.direct_stream_path = direct_stream_path
        self._models = None
        self.conversation_id: str | None = None

    def __enter__(self) -> DirectArenaClient:  # noqa: PYI034 - Self is not in typing until Python 3.11
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def get_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        if refresh or self._models is None:
            self._models = _models_from_markup(
                self.transport.get_text(self.direct_page_path)
            )
            if not self._models:
                raise ProtocolError(
                    "Could not find model metadata in Arena's Direct Chat page."
                )
        return list(self._models)

    def stream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        verification_token: str | None = None,
        config: GenerationConfig | None = None,
        conversation_id: str | None = None,
    ) -> Iterator[StreamChunk]:
        generation = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        model_id = self._resolve_model(model, self.get_models())
        token = self._token(verification_token)
        chat_id = conversation_id or self.conversation_id or uuid7()
        if conversation_id is None:
            self.conversation_id = chat_id
        body = self._request_body(prompt, model_id, token, chat_id, generation)
        finished = False
        text = ""
        deadline = time.monotonic() + generation.queue_timeout
        with self.transport.stream_request(
            "POST",
            self.direct_stream_path,
            headers={
                "content-type": "text/plain;charset=UTF-8",
                "accept": "text/plain, text/event-stream, */*",
            },
            content=json.dumps(body, separators=(",", ":")),
            timeout=httpx.Timeout(generation.queue_timeout),
        ) as response:
            for kind, value in _iter_response_records(response):
                if time.monotonic() >= deadline:
                    raise QueueTimeoutError(
                        "Direct Chat stream exceeded its configured timeout."
                    )
                text, chunk, done = _update_direct_text(kind, value, text)
                if chunk is not None:
                    yield chunk
                if done:
                    finished = True
                    return
        if not finished:
            raise ProtocolError(
                "Arena Direct Chat stream ended without a completion record."
            )

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        verification_token: str | None = None,
        config: GenerationConfig | None = None,
        conversation_id: str | None = None,
    ) -> str:
        return "".join(
            chunk.delta
            for chunk in self.stream(
                prompt,
                model=model,
                verification_token=verification_token,
                config=config,
                conversation_id=conversation_id,
            )
        )

    def chat(
        self,
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> DirectChatSession:
        return DirectChatSession(self, model=model, config=config)


def _copy_messages(history: Sequence[Message] | None) -> list[Message]:
    if not history:
        return []
    return [
        Message(role=item.role, content=item.content, id=item.id) for item in history
    ]


class DirectChatSession:
    """Multi-turn Direct Chat state; each session owns its conversation UUID."""

    def __init__(
        self,
        client: DirectArenaClient,
        *,
        model: str,
        config: GenerationConfig | None = None,
        history: Sequence[Message] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.config = config or GenerationConfig()
        self.history = _copy_messages(history)
        self.turn_id = sum(item.role == "assistant" for item in self.history)
        self.parent_message_id = next(
            (item.id for item in reversed(self.history) if item.role == "assistant"),
            None,
        )
        self.conversation_id: str | None = None

    def stream_message(
        self, content: str, *, verification_token: str | None = None
    ) -> Iterator[StreamChunk]:
        user = Message("user", content, uuid.uuid4().hex)
        self.history.append(user)
        prior_conversation_id = self.conversation_id
        self.conversation_id = self.conversation_id or uuid7()
        collected: list[str] = []
        committed = False
        try:
            for chunk in self.client.stream(
                content,
                model=self.model,
                verification_token=verification_token,
                config=self.config,
                conversation_id=self.conversation_id,
            ):
                if chunk.delta:
                    collected.append(chunk.delta)
                if chunk.finished:
                    assistant = Message(
                        "assistant", "".join(collected), uuid.uuid4().hex
                    )
                    self.history.append(assistant)
                    self.turn_id += 1
                    self.parent_message_id = assistant.id
                    committed = True
                yield chunk
            if not committed:
                raise ProtocolError(
                    "Direct Chat stream ended without a completion chunk."
                )
        finally:
            if not committed:
                for index in range(len(self.history) - 1, -1, -1):
                    if self.history[index] is user:
                        del self.history[index]
                        break
                self.conversation_id = prior_conversation_id

    def send_message(
        self, content: str, *, verification_token: str | None = None
    ) -> str:
        return "".join(
            chunk.delta
            for chunk in self.stream_message(
                content, verification_token=verification_token
            )
        )

    def clear(self) -> None:
        self.history.clear()
        self.turn_id = 0
        self.parent_message_id = None
        self.conversation_id = None


class AsyncDirectArenaClient(_DirectMixin):
    """Async client for the same Direct Chat frontend."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cookies: CookieInput = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http2: bool = True,
        verify: bool | str = True,
        verification_token: VerificationToken | None = None,
        direct_page_path: str = DIRECT_PAGE_PATH,
        direct_stream_path: str = DIRECT_STREAM_PATH,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.transport = AsyncArenaTransport(
            base_url,
            cookies=cookies,
            headers=headers,
            timeout=timeout,
            http2=http2,
            verify=verify,
            http_transport=http_transport,
        )
        self.verification_token = verification_token
        self.direct_page_path = direct_page_path
        self.direct_stream_path = direct_stream_path
        self._models = None
        self.conversation_id: str | None = None

    async def __aenter__(self) -> AsyncDirectArenaClient:  # noqa: PYI034 - Self is not in typing until Python 3.11
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.transport.close()

    async def get_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        if refresh or self._models is None:
            self._models = _models_from_markup(
                await self.transport.get_text(self.direct_page_path)
            )
            if not self._models:
                raise ProtocolError(
                    "Could not find model metadata in Arena's Direct Chat page."
                )
        return list(self._models)

    async def astream(
        self,
        prompt: str,
        *,
        model: str | None = None,
        verification_token: str | None = None,
        config: GenerationConfig | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        generation = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        model_id = self._resolve_model(model, await self.get_models())
        token = self._token(verification_token)
        chat_id = conversation_id or self.conversation_id or uuid7()
        if conversation_id is None:
            self.conversation_id = chat_id
        body = self._request_body(prompt, model_id, token, chat_id, generation)
        text = ""
        deadline = time.monotonic() + generation.queue_timeout
        async with self.transport.stream_request(
            "POST",
            self.direct_stream_path,
            headers={
                "content-type": "text/plain;charset=UTF-8",
                "accept": "text/plain, text/event-stream, */*",
            },
            content=json.dumps(body, separators=(",", ":")),
            timeout=httpx.Timeout(generation.queue_timeout),
        ) as response:
            async for kind, value in _aiter_response_records(response):
                if time.monotonic() >= deadline:
                    raise QueueTimeoutError(
                        "Direct Chat stream exceeded its configured timeout."
                    )
                text, chunk, done = _update_direct_text(kind, value, text)
                if chunk is not None:
                    yield chunk
                if done:
                    return
        raise ProtocolError(
            "Arena Direct Chat stream ended without a completion record."
        )

    async def acomplete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        verification_token: str | None = None,
        config: GenerationConfig | None = None,
        conversation_id: str | None = None,
    ) -> str:
        return "".join(
            [
                chunk.delta
                async for chunk in self.astream(
                    prompt,
                    model=model,
                    verification_token=verification_token,
                    config=config,
                    conversation_id=conversation_id,
                )
            ]
        )

    def chat(
        self,
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> AsyncDirectChatSession:
        return AsyncDirectChatSession(self, model=model, config=config)


class AsyncDirectChatSession:
    """Async multi-turn Direct Chat state."""

    def __init__(
        self,
        client: AsyncDirectArenaClient,
        *,
        model: str,
        config: GenerationConfig | None = None,
        history: Sequence[Message] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.config = config or GenerationConfig()
        self.history = _copy_messages(history)
        self.turn_id = sum(item.role == "assistant" for item in self.history)
        self.parent_message_id = next(
            (item.id for item in reversed(self.history) if item.role == "assistant"),
            None,
        )
        self.conversation_id: str | None = None

    async def stream_message(
        self,
        content: str,
        *,
        verification_token: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        user = Message("user", content, uuid.uuid4().hex)
        self.history.append(user)
        prior_conversation_id = self.conversation_id
        self.conversation_id = self.conversation_id or uuid7()
        collected: list[str] = []
        committed = False
        try:
            async for chunk in self.client.astream(
                content,
                model=self.model,
                verification_token=verification_token,
                config=self.config,
                conversation_id=self.conversation_id,
            ):
                if chunk.delta:
                    collected.append(chunk.delta)
                if chunk.finished:
                    assistant = Message(
                        "assistant", "".join(collected), uuid.uuid4().hex
                    )
                    self.history.append(assistant)
                    self.turn_id += 1
                    self.parent_message_id = assistant.id
                    committed = True
                yield chunk
            if not committed:
                raise ProtocolError(
                    "Direct Chat stream ended without a completion chunk."
                )
        finally:
            if not committed:
                for index in range(len(self.history) - 1, -1, -1):
                    if self.history[index] is user:
                        del self.history[index]
                        break
                self.conversation_id = prior_conversation_id

    async def send_message(
        self, content: str, *, verification_token: str | None = None
    ) -> str:
        return "".join(
            [
                chunk.delta
                async for chunk in self.stream_message(
                    content,
                    verification_token=verification_token,
                )
            ]
        )

    def clear(self) -> None:
        self.history.clear()
        self.turn_id = 0
        self.parent_message_id = None
        self.conversation_id = None
