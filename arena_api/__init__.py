"""Unofficial, cookie-authenticated client for Gradio-based Arena frontends."""

from .client import ArenaClient, AsyncArenaClient
from .direct import AsyncDirectArenaClient, DirectArenaClient, DirectChatSession
from .exceptions import ArenaError, AuthenticationError, ChallengeRequiredError, ModelUnavailableError, ProtocolError, QueueTimeoutError, RateLimitError
from .models import BattleResponse, GenerationConfig, Message, ModelInfo, QueueEndpoint, StreamChunk
from .session import AsyncChatSession, ChatSession

__all__ = [
    "ArenaClient", "AsyncArenaClient", "DirectArenaClient", "AsyncDirectArenaClient", "DirectChatSession", "ChatSession", "AsyncChatSession", "ArenaError",
    "AuthenticationError", "ChallengeRequiredError", "QueueTimeoutError", "ModelUnavailableError",
    "RateLimitError", "ProtocolError", "ModelInfo", "Message",
    "GenerationConfig", "StreamChunk", "BattleResponse", "QueueEndpoint",
]

