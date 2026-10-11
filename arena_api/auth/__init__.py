"""Session extraction and lifetime helpers for the Arena clients.

Two backends, one contract — *the caller's own session, honestly obtained*:

* :mod:`arena_api.auth.firefox_session` reads the signed-in session out of your
  local Firefox profile (``rookiepy``, with a lock-safe SQLite fallback).
* :mod:`arena_api.auth.manager` normalizes any cookie source (Firefox profile,
  Playwright ``storage_state`` file, in-memory mapping, your own callable) into
  a :class:`~arena_api.auth.manager.CookieJar`, persists it, probes whether it
  is still accepted, and refreshes it under an explicit
  :class:`~arena_api.auth.manager.RefreshPolicy`.
* :mod:`arena_api.auth.verify` is the human checkpoint: a real browser window,
  you finish whatever the site asks, the resulting cookies are exported.

This package does not detect, solve, click, or replay CAPTCHA / Cloudflare
Turnstile / DataDome challenges, does not patch browser fingerprints, and does
not synthesize or rotate User-Agents. When a service demands a check, the
clients raise :class:`~arena_api.exceptions.ChallengeRequiredError` and the
manager can wait for *you* — that is the whole story, and it is a deliberate
design constraint, not a missing feature. Automated tests that must not see a
challenge should exempt the test environment by configuration (vendor test
keys / allow-lists), see ``docs/automated-tests-and-captcha.md``.

Imports are stdlib-only so the package stays lightweight; ``rookiepy`` and
``playwright`` are optional, lazily loaded dependencies.
"""

from .firefox_session import (
    DEFAULT_COOKIE_DOMAINS,
    FirefoxSessionError,
    FirefoxSessionManager,
    SessionManager,
)
from .manager import (
    AutoCookieManager,
    AutoSessionManager,
    CallableSource,
    CheckpointContext,
    CookieJar,
    CookieRecord,
    FirefoxSource,
    HttpLivenessProbe,
    ManualCheckpoint,
    PlaywrightStorageSource,
    ProbeOutcome,
    RefreshPolicy,
    SessionCheckpoint,
    SessionSource,
    SessionStats,
    SessionStore,
    SessionStoreError,
    StaticSource,
    looks_like_challenge,
)

__all__ = [
    "DEFAULT_COOKIE_DOMAINS",
    "AutoCookieManager",
    "AutoSessionManager",
    "CallableSource",
    "CheckpointContext",
    "CookieJar",
    "CookieRecord",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "FirefoxSource",
    "HttpLivenessProbe",
    "ManualCheckpoint",
    "PlaywrightStorageSource",
    "ProbeOutcome",
    "RefreshPolicy",
    "SessionCheckpoint",
    "SessionManager",
    "SessionSource",
    "SessionStats",
    "SessionStore",
    "SessionStoreError",
    "StaticSource",
    "looks_like_challenge",
]


def __getattr__(name: str) -> object:
    """Lazily expose the Playwright-dependent helpers from ``arena_api.auth``.

    Keeps ``import arena_api`` free of any browser dependency while still
    allowing ``from arena_api.auth import capture_storage_state``.
    """
    if name in {
        "PlaywrightCheckpoint",
        "PlaywrightNotAvailable",
        "VerificationError",
        "VerificationResult",
        "capture_storage_state",
        "is_allowed_test_host",
        "wait_for_cookie_names",
    }:
        from . import verify

        return getattr(verify, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
