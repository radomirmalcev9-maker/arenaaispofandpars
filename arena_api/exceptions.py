"""Exceptions exposed by :mod:`arena_api`."""

from __future__ import annotations


class ArenaError(Exception):
    """Base exception for client, transport, and protocol failures."""


class AuthenticationError(ArenaError):
    """The supplied browser session is absent, expired, or rejected."""


class ChallengeRequiredError(AuthenticationError):
    """The service returned a browser verification or anti-bot challenge.

    Refresh or verify the session in a normal browser, then provide the
    resulting credentials through the documented client inputs.
    """


class QueueTimeoutError(ArenaError):
    """The queue or stream did not complete before its configured timeout."""


class ModelUnavailableError(ArenaError):
    """A model, callback, or model-list endpoint is not currently available."""


class RateLimitError(ArenaError):
    """The service rejected a request because of a rate limit."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProtocolError(ArenaError):
    """A response did not match the expected Arena/Gradio protocol."""
