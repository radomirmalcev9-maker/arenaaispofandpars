"""Unofficial, cookie-authenticated sync/async clients for Arena frontends.

This package uses caller-provided credentials — or, opt-in via
``session_source="firefox"``, the signed-in session of your local Firefox
profile — and does not bypass browser verification challenges. See the
project README for protocol caveats.
"""

from .auth import FirefoxSessionError, FirefoxSessionManager, SessionManager
from .client import ArenaClient, AsyncArenaClient
from .direct import (
    AsyncDirectArenaClient,
    AsyncDirectChatSession,
    DirectArenaClient,
    DirectChatSession,
)
from .exceptions import (
    ArenaError,
    AuthenticationError,
    ChallengeRequiredError,
    ModelUnavailableError,
    ProtocolError,
    QueueTimeoutError,
    RateLimitError,
)
from .models import (
    BattleResponse,
    BattleStreamChunk,
    GenerationConfig,
    Message,
    ModelInfo,
    QueueEndpoint,
    StreamChunk,
)
from .session import AsyncChatSession, ChatSession
from .transport import (
    ArenaTransport,
    AsyncArenaTransport,
    SSEParser,
    normalize_cookies,
    parse_cookie_header,
)

# Short aliases for applications that do not need to distinguish the backend.
Client = ArenaClient
AsyncClient = AsyncArenaClient

__all__ = [
    "ArenaClient",
    "ArenaError",
    "ArenaTransport",
    "AsyncArenaClient",
    "AsyncArenaTransport",
    "AsyncChatSession",
    "AsyncClient",
    "AsyncDirectArenaClient",
    "AsyncDirectChatSession",
    "AuthenticationError",
    "BattleResponse",
    "BattleStreamChunk",
    "ChallengeRequiredError",
    "ChatSession",
    "Client",
    "DirectArenaClient",
    "DirectChatSession",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "GenerationConfig",
    "Message",
    "ModelInfo",
    "ModelUnavailableError",
    "ProtocolError",
    "QueueEndpoint",
    "QueueTimeoutError",
    "RateLimitError",
    "SSEParser",
    "SessionManager",
    "StreamChunk",
    "normalize_cookies",
    "parse_cookie_header",
]
