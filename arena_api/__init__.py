"""Unofficial, cookie-authenticated sync/async clients for Arena frontends.

This package uses caller-provided credentials — the signed-in session of your
local Firefox profile, or a Playwright ``storage_state`` file, both opt-in — and
does not bypass browser verification challenges: :mod:`arena_api.auth` manages
the *lifetime* of a session you obtained yourself (persist, probe, refresh,
hand a human the browser), never its *defeat*. See the project README for
protocol caveats and ``docs/automated-tests-and-captcha.md`` for how to keep
challenges out of your automated tests by configuration.
"""

from .auth import (
    AutoSessionManager,
    CallableSource,
    CheckpointContext,
    CookieJar,
    CookieRecord,
    FirefoxSessionError,
    FirefoxSessionManager,
    FirefoxSource,
    HttpLivenessProbe,
    ManualCheckpoint,
    PlaywrightStorageSource,
    ProbeOutcome,
    RefreshPolicy,
    SessionCheckpoint,
    SessionManager,
    SessionSource,
    SessionStats,
    SessionStore,
    SessionStoreError,
    StaticSource,
)
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
    "AutoSessionManager",
    "BattleResponse",
    "BattleStreamChunk",
    "CallableSource",
    "ChallengeRequiredError",
    "ChatSession",
    "CheckpointContext",
    "Client",
    "CookieJar",
    "CookieRecord",
    "DirectArenaClient",
    "DirectChatSession",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "FirefoxSource",
    "GenerationConfig",
    "HttpLivenessProbe",
    "ManualCheckpoint",
    "Message",
    "ModelInfo",
    "ModelUnavailableError",
    "PlaywrightStorageSource",
    "ProbeOutcome",
    "ProtocolError",
    "QueueEndpoint",
    "QueueTimeoutError",
    "RateLimitError",
    "RefreshPolicy",
    "SSEParser",
    "SessionCheckpoint",
    "SessionManager",
    "SessionSource",
    "SessionStats",
    "SessionStore",
    "SessionStoreError",
    "StaticSource",
    "StreamChunk",
    "normalize_cookies",
    "parse_cookie_header",
]
