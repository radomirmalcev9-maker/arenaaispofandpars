"""Session extraction helpers for the Arena clients.

The only backend currently implemented is the local Firefox profile; see
:mod:`arena_api.auth.firefox_session`. Imports are stdlib-only so this
package stays lightweight; ``rookiepy`` is an optional, lazily loaded
dependency.
"""

from .firefox_session import (
    DEFAULT_COOKIE_DOMAINS,
    FirefoxSessionError,
    FirefoxSessionManager,
    SessionManager,
)

__all__ = [
    "DEFAULT_COOKIE_DOMAINS",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "SessionManager",
]
