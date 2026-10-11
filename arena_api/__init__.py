"""Unofficial, cookie-authenticated sync/async clients for Arena frontends.

This package uses caller-provided credentials — or, opt-in via
``session_source="firefox"``, the signed-in session of your local Firefox
profile. See the project README for protocol caveats.

``arena_api.auth`` additionally ships E2E/synthetic-monitoring tooling
(Playwright wrapper, pointer kinematics, determinism init scripts, session
lifecycle management and a challenge *detector*); see
``arena_api.auth.environment`` and ``arena_api.auth.challenge``.
"""

from .auth import (
    BrowserEnvironment,
    BrowserEnvironmentOptions,
    BrowserSessionManager,
    CaptchaAttempt,
    CaptchaSolver,
    ChallengeHandler,
    ChallengeKind,
    ChallengeObservation,
    DeterminismOptions,
    FirefoxSessionError,
    FirefoxSessionManager,
    FormLoginFlow,
    KinematicMouse,
    KinematicMouseSync,
    RecaptchaToolkitSolver,
    SessionManager,
    SessionValidation,
    TrajectoryPlanner,
    build_init_script,
    inspect_page,
    inspect_response,
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
    CaptchaSolveError,
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

# Imported last: the bootstrap facade builds on the clients above.
from .auto import (  # noqa: E402
    AutoOptions,
    AutoSessionManager,
    DirectVerificationToken,
    DiscoveredSession,
    ProbeResult,
    auto_async_client,
    auto_async_direct_client,
    auto_client,
    auto_direct_client,
    discover_session,
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
    "AutoOptions",
    "AutoSessionManager",
    "BattleResponse",
    "BattleStreamChunk",
    "CaptchaAttempt",
    "CaptchaSolveError",
    "CaptchaSolver",
    "ChallengeRequiredError",
    "ChatSession",
    "Client",
    "DirectArenaClient",
    "DirectChatSession",
    "DirectVerificationToken",
    "DiscoveredSession",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "GenerationConfig",
    "Message",
    "ModelInfo",
    "ModelUnavailableError",
    "ProbeResult",
    "ProtocolError",
    "QueueEndpoint",
    "QueueTimeoutError",
    "RateLimitError",
    "RecaptchaToolkitSolver",
    "SSEParser",
    "SessionManager",
    "StreamChunk",
    "auto_async_client",
    "auto_async_direct_client",
    "auto_client",
    "auto_direct_client",
    "discover_session",
    "normalize_cookies",
    "parse_cookie_header",
]
