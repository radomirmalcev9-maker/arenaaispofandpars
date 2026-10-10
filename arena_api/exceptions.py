"""Exceptions exposed by :mod:`arena_api`."""

from __future__ import annotations


class ArenaError(Exception):
    """Base exception for this package."""


class AuthenticationError(ArenaError):
    """The supplied browser session is absent, expired, or rejected."""


class ChallengeRequiredError(AuthenticationError):
    """The server returned an interactive browser challenge.

    This package intentionally does not attempt to solve CAPTCHAs or browser
    challenges.  Refresh the session in a normal browser and supply its
    exported cookies instead.
    """


class QueueTimeoutError(ArenaError):
    """The Gradio queue did not produce a terminal event in time."""


class ModelUnavailableError(ArenaError):
    """The requested model or UI endpoint is not currently available."""


class RateLimitError(ArenaError):
    """Arena rejected the request because of a rate limit."""


class ProtocolError(ArenaError):
    """A server response did not conform to the expected Gradio protocol."""

