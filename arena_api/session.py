"""Stateful multi-turn conversations."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

from .models import GenerationConfig, Message, StreamChunk

if TYPE_CHECKING:
    from .client import ArenaClient, AsyncArenaClient


class ChatSession:
    """A lightweight conversation context persisted client-side.

    The public Arena UI is stateless at the HTTP layer, therefore every turn
    includes an encoded transcript in ``GenerationConfig.extra``.  This avoids
    relying on a particular Gradio ``State`` component id.
    """

    def __init__(self, client: "ArenaClient", *, model: str | None = None,
                 config: GenerationConfig | None = None) -> None:
        self.client = client
        self.model = model
        self.config = config or GenerationConfig()
        self.history: list[Message] = []
        self.turn_id = 0
        self.parent_message_id: str | None = None

    def _turn_config(self) -> GenerationConfig:
        extra = dict(self.config.extra)
        extra.setdefault("history", [{"role": item.role, "content": item.content} for item in self.history])
        return GenerationConfig(
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            top_p=self.config.top_p,
            queue_timeout=self.config.queue_timeout,
            extra=extra,
        )

    def stream_message(self, content: str) -> Iterator[StreamChunk]:
        user = Message("user", content, uuid.uuid4().hex)
        self.history.append(user)
        collected: list[str] = []
        try:
            for chunk in self.client.stream(content, model=self.model, config=self._turn_config()):
                if chunk.delta:
                    collected.append(chunk.delta)
                yield chunk
        except Exception:
            self.history.pop()  # do not retain a turn that never reached Arena
            raise
        answer = "".join(collected)
        assistant = Message("assistant", answer, uuid.uuid4().hex)
        self.history.append(assistant)
        self.turn_id += 1
        self.parent_message_id = assistant.id

    def send_message(self, content: str) -> str:
        return "".join(part.delta for part in self.stream_message(content))

    def clear(self) -> None:
        self.history.clear()
        self.turn_id = 0
        self.parent_message_id = None


class AsyncChatSession:
    """Async counterpart of :class:`ChatSession` with identical state fields."""

    def __init__(self, client: "AsyncArenaClient", *, model: str | None = None,
                 config: GenerationConfig | None = None) -> None:
        self.client = client
        self.model = model
        self.config = config or GenerationConfig()
        self.history: list[Message] = []
        self.turn_id = 0
        self.parent_message_id: str | None = None

    def _turn_config(self) -> GenerationConfig:
        extra = dict(self.config.extra)
        extra.setdefault("history", [{"role": item.role, "content": item.content} for item in self.history])
        return GenerationConfig(self.config.temperature, self.config.max_tokens,
                                self.config.top_p, self.config.queue_timeout, extra)

    async def stream_message(self, content: str) -> AsyncIterator[StreamChunk]:
        user = Message("user", content, uuid.uuid4().hex)
        self.history.append(user)
        collected: list[str] = []
        try:
            async for chunk in self.client.astream(content, model=self.model, config=self._turn_config()):
                if chunk.delta:
                    collected.append(chunk.delta)
                yield chunk
        except Exception:
            self.history.pop()
            raise
        assistant = Message("assistant", "".join(collected), uuid.uuid4().hex)
        self.history.append(assistant)
        self.turn_id += 1
        self.parent_message_id = assistant.id

    async def send_message(self, content: str) -> str:
        return "".join([part.delta async for part in self.stream_message(content)])

    def clear(self) -> None:
        self.history.clear()
        self.turn_id = 0
        self.parent_message_id = None

