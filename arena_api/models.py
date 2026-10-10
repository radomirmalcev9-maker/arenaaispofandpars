"""Typed public value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    display_name: str | None = None
    available: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    role: Literal["user", "assistant", "system"]
    content: str
    id: str | None = None


@dataclass(slots=True)
class GenerationConfig:
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    queue_timeout: float = 180.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StreamChunk:
    text: str
    delta: str
    event: str
    finished: bool = False
    raw: Any = None


@dataclass(frozen=True, slots=True)
class BattleResponse:
    left: str
    right: str
    left_model: str | None = None
    right_model: str | None = None


@dataclass(frozen=True, slots=True)
class QueueEndpoint:
    """A discovered or manually supplied queued Gradio callback."""

    fn_index: int
    api_name: str | None = None
    queue: bool = True

