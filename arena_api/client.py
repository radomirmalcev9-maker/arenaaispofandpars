"""Synchronous and asynchronous Arena clients."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

from .constants import CONFIG_PATH, DEFAULT_BASE_URL, DEFAULT_QUEUE_TIMEOUT, QUEUE_DATA_PATH, QUEUE_JOIN_PATH
from .exceptions import ModelUnavailableError, ProtocolError, QueueTimeoutError
from .models import BattleResponse, GenerationConfig, ModelInfo, QueueEndpoint, StreamChunk
from .transport import ArenaTransport, AsyncArenaTransport


def _session_hash() -> str:
    return secrets.token_hex(16)


def _dependencies(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in config.get("dependencies", []) if isinstance(item, Mapping)]


def _find_fn_index(config: Mapping[str, Any], purpose: str) -> int:
    """Locate a queue function from Gradio's public config, despite version drift."""
    words = {
        "chat": ("add_text", "send_message", "chat"),
        "battle": ("add_text", "send_message", "battle"),
    }[purpose]
    matches: list[tuple[int, Mapping[str, Any]]] = []
    for index, dep in enumerate(_dependencies(config)):
        name = str(dep.get("api_name", "")).lower()
        target = " ".join(map(str, dep.get("targets", []))).lower()
        if any(word in name or word in target for word in words):
            matches.append((index, dep))
    if not matches:
        raise ModelUnavailableError(
            "The current Arena UI does not expose a discoverable %s endpoint. "
            "Pass its fn_index explicitly from /config." % purpose
        )
    # A queued dependency is the one compatible with /queue/join.
    for index, dep in matches:
        if dep.get("queue", True):
            return int(dep.get("fn_index", index))
    return int(matches[0][1].get("fn_index", matches[0][0]))


def _models_from_config(config: Mapping[str, Any]) -> list[ModelInfo]:
    """Read model selectors from a Gradio config without coupling to component ids."""
    models: dict[str, ModelInfo] = {}
    for component in config.get("components", []):
        if not isinstance(component, Mapping):
            continue
        props = component.get("props", {})
        choices = props.get("choices", []) if isinstance(props, Mapping) else []
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if isinstance(choice, list) and choice:
                ident = str(choice[0])
                label = str(choice[1]) if len(choice) > 1 else ident
            else:
                ident = label = str(choice)
            if ident and ident != "None":
                models.setdefault(ident, ModelInfo(id=ident, display_name=label))
    return sorted(models.values(), key=lambda model: model.id.casefold())


def _assistant_text(event: Any) -> str | None:
    """Extract the latest assistant string from common Gradio output shapes."""
    if not isinstance(event, Mapping):
        return event if isinstance(event, str) else None
    if event.get("error"):
        raise ProtocolError("Arena queue error: %s" % event["error"])
    payload = event.get("output", event)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        return None
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, Mapping):
            # Gradio Chatbot's messages format.
            if value.get("role") == "assistant" and isinstance(value.get("content"), str):
                candidates.append(value["content"])
            else:
                for child in value.values():
                    visit(child)
    visit(data)
    return candidates[-1] if candidates else None


def _terminal(event: Any) -> bool:
    return isinstance(event, Mapping) and event.get("msg") in {"process_completed", "process_failed"}


class ArenaClient:
    """Blocking client for a Gradio Arena deployment.

    ``chat_fn_index`` and ``battle_fn_index`` are optional overrides. They are
    intentionally public because Arena's UI deployment can change independently
    of this package; inspect ``GET /config`` to update them when necessary.
    """

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, cookies: Any = None,
                 headers: Mapping[str, str] | None = None, chat_fn_index: int | None = None,
                 battle_fn_index: int | None = None, impersonate: str = "chrome124") -> None:
        self.transport = ArenaTransport(base_url, cookies=cookies, headers=headers, impersonate=impersonate)
        self.chat_fn_index, self.battle_fn_index = chat_fn_index, battle_fn_index
        self._config: Mapping[str, Any] | None = None

    def __enter__(self) -> "ArenaClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.transport.close()

    def config(self, *, refresh: bool = False) -> Mapping[str, Any]:
        if refresh or self._config is None:
            value = self.transport.get_json(CONFIG_PATH)
            if not isinstance(value, Mapping):
                raise ProtocolError("/config did not return a JSON object")
            self._config = value
        return self._config

    def get_models(self) -> list[ModelInfo]:
        # Deployments increasingly provide an explicit endpoint; fall back to
        # config scanning so old public Gradio spaces remain usable.
        for path in ("/api/v1/models", "/models"):
            try:
                payload = self.transport.get_json(path)
            except Exception:
                continue
            raw = payload.get("data", payload.get("models", [])) if isinstance(payload, Mapping) else payload
            if isinstance(raw, list):
                return [ModelInfo(id=str(x.get("id", x.get("name"))), display_name=x.get("display_name", x.get("name")), metadata=dict(x)) if isinstance(x, Mapping) else ModelInfo(id=str(x)) for x in raw]
        return _models_from_config(self.config())

    def get_endpoint(self, purpose: str = "chat") -> QueueEndpoint:
        """Return the selected queue endpoint for diagnostics or custom calls."""
        return QueueEndpoint(fn_index=self._fn_index(purpose))

    def _fn_index(self, purpose: str) -> int:
        explicit = self.chat_fn_index if purpose == "chat" else self.battle_fn_index
        return explicit if explicit is not None else _find_fn_index(self.config(), purpose)

    def _queue(self, fn_index: int, data: list[Any], timeout: float) -> Iterator[tuple[str, Any]]:
        session_hash = _session_hash()
        joined = self.transport.post_json(QUEUE_JOIN_PATH, {"data": data, "fn_index": fn_index, "session_hash": session_hash})
        if isinstance(joined, Mapping) and joined.get("error"):
            raise ProtocolError("Arena queue rejected request: %s" % joined["error"])
        yielded = False
        for item in self.transport.sse(QUEUE_DATA_PATH, params={"session_hash": session_hash}, timeout=timeout):
            yielded = True
            yield item
        if not yielded:
            raise QueueTimeoutError("Queue stream closed without events.")

    @staticmethod
    def _chat_payload(prompt: str, model: str | None, config: GenerationConfig) -> list[Any]:
        # This is the standard Gradio chat call shape. `extra` lets callers
        # supply deployment-specific dropdown/state fields without forked code.
        data: list[Any] = [prompt]
        if model is not None:
            data.append(model)
        data.append(None)  # multimodal attachment slot used by current Gradio UI
        data.append(config.extra)
        return data

    def stream(self, prompt: str, *, model: str | None = None, config: GenerationConfig | None = None) -> Iterator[StreamChunk]:
        config = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        previous = ""
        for event_name, event in self._queue(self._fn_index("chat"), self._chat_payload(prompt, model, config), config.queue_timeout):
            text = _assistant_text(event)
            if text is None:
                if _terminal(event):
                    yield StreamChunk(text=previous, delta="", event="completed", finished=True, raw=event)
                    return
                continue
            # Some queue versions send the full response; a few send deltas.
            full = text if text.startswith(previous) else previous + text
            delta = full[len(previous):]
            previous = full
            if delta:
                yield StreamChunk(text=full, delta=delta, event=event_name, raw=event)
            if _terminal(event):
                yield StreamChunk(text=full, delta="", event="completed", finished=True, raw=event)
                return

    def stream_queue(self, data: list[Any], *, fn_index: int, timeout: float = DEFAULT_QUEUE_TIMEOUT) -> Iterator[tuple[str, Any]]:
        """Run a deployment-specific callback without discarding raw Gradio events.

        Use this escape hatch when an Arena UI revision changes its callback
        argument order. It remains fully queue/SSE based and exposes no private
        browser state beyond the session explicitly supplied to this client.
        """
        yield from self._queue(fn_index, data, timeout)

    def complete(self, prompt: str, *, model: str | None = None, config: GenerationConfig | None = None) -> str:
        return "".join(chunk.delta for chunk in self.stream(prompt, model=model, config=config))

    def chat(self, *, model: str | None = None, config: GenerationConfig | None = None) -> "ChatSession":
        from .session import ChatSession
        return ChatSession(self, model=model, config=config)

    def battle(self, prompt: str, *, left_model: str | None = None, right_model: str | None = None,
               config: GenerationConfig | None = None) -> BattleResponse:
        config = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        texts: list[str] = []
        data = [prompt, left_model, right_model, None, config.extra]
        for _, event in self._queue(self._fn_index("battle"), data, config.queue_timeout):
            payload = event.get("output", event) if isinstance(event, Mapping) else event
            values = payload.get("data", []) if isinstance(payload, Mapping) else []
            if isinstance(values, list):
                found: list[str] = []
                for value in values:
                    text = _assistant_text({"output": {"data": [value]}})
                    if text:
                        found.append(text)
                if len(found) >= 2:
                    texts = found[-2:]
        if len(texts) < 2:
            raise ProtocolError("Battle queue did not yield two responses.")
        return BattleResponse(texts[0], texts[1], left_model, right_model)


class AsyncArenaClient(ArenaClient):
    """Async API with the same semantics as :class:`ArenaClient`."""
    # Keep the public API explicit instead of silently returning blocking values.
    def __init__(self, **kwargs: Any) -> None:
        base_url = kwargs.pop("base_url", DEFAULT_BASE_URL)
        cookies, headers = kwargs.pop("cookies", None), kwargs.pop("headers", None)
        self.chat_fn_index = kwargs.pop("chat_fn_index", None)
        self.battle_fn_index = kwargs.pop("battle_fn_index", None)
        self.transport = AsyncArenaTransport(base_url, cookies=cookies, headers=headers, impersonate=kwargs.pop("impersonate", "chrome124"))
        self._config = None

    async def aclose(self) -> None:
        await self.transport.close()

    async def __aenter__(self) -> "AsyncArenaClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def config(self, *, refresh: bool = False) -> Mapping[str, Any]:
        if refresh or self._config is None:
            value = await self.transport.get_json(CONFIG_PATH)
            if not isinstance(value, Mapping):
                raise ProtocolError("/config did not return a JSON object")
            self._config = value
        return self._config

    async def get_models(self) -> list[ModelInfo]:
        # Keep the async method non-blocking; config is the stable public source
        # available across Gradio deployments.
        return _models_from_config(await self.config())

    async def get_endpoint(self, purpose: str = "chat") -> QueueEndpoint:
        explicit = self.chat_fn_index if purpose == "chat" else self.battle_fn_index
        return QueueEndpoint(fn_index=explicit if explicit is not None else _find_fn_index(await self.config(), purpose))

    async def astream(self, prompt: str, *, model: str | None = None, config: GenerationConfig | None = None) -> AsyncIterator[StreamChunk]:
        config = config or GenerationConfig(queue_timeout=DEFAULT_QUEUE_TIMEOUT)
        index = self.chat_fn_index if self.chat_fn_index is not None else _find_fn_index(await self.config(), "chat")
        session_hash = _session_hash()
        joined = await self.transport.post_json(QUEUE_JOIN_PATH, {"data": self._chat_payload(prompt, model, config), "fn_index": index, "session_hash": session_hash})
        if isinstance(joined, Mapping) and joined.get("error"):
            raise ProtocolError("Arena queue rejected request: %s" % joined["error"])
        previous = ""
        async for event_name, event in self.transport.sse(QUEUE_DATA_PATH, params={"session_hash": session_hash}, timeout=config.queue_timeout):
            text = _assistant_text(event)
            if text is not None:
                full = text if text.startswith(previous) else previous + text
                delta, previous = full[len(previous):], full
                if delta:
                    yield StreamChunk(text=full, delta=delta, event=event_name, raw=event)
            if _terminal(event):
                yield StreamChunk(text=previous, delta="", event="completed", finished=True, raw=event)
                return

    async def acomplete(self, prompt: str, *, model: str | None = None, config: GenerationConfig | None = None) -> str:
        return "".join([chunk.delta async for chunk in self.astream(prompt, model=model, config=config)])

    def chat(self, *, model: str | None = None, config: GenerationConfig | None = None) -> "AsyncChatSession":
        from .session import AsyncChatSession
        return AsyncChatSession(self, model=model, config=config)
