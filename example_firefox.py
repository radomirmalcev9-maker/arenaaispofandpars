"""Run with: python example_firefox.py

Requires a local Firefox profile that is signed in to arena.ai. The
recommended cookie backend is rookiepy (python -m pip install '.[firefox]');
without it the built-in lock-safe cookies.sqlite reader is used.

```python -m pip install '.[impersonate]'``` additionally sends the requests
with Firefox's own TLS/HTTP2 fingerprint, which is what a cookie-aware edge
(Cloudflare and the cf_clearance cookie) also looks at: a session copied out of
Firefox but presented from Python's TLS stack can be answered with a challenge
page even though every cookie is correct. The client picks it up by itself once
the extra is installed; ``impersonate=False`` turns it off.
"""

from __future__ import annotations

import sys

from arena_api import ArenaClient, GenerationConfig
from arena_api.auth import FirefoxSessionManager


def main() -> None:
    manager = FirefoxSessionManager()
    try:
        profile = manager.profile_path()
    except Exception as exc:  # FirefoxSessionError, but the example stays loose
        raise SystemExit(f"Firefox profile not found: {exc}") from exc

    print("Firefox profile:", profile)
    cookies = manager.load_cookies()
    if not cookies:
        raise SystemExit(
            "No arena.ai cookies in this Firefox profile. "
            "Open https://arena.ai in Firefox, sign in, and rerun."
        )
    print("Session cookies:", ", ".join(sorted(cookies)))
    print("User-Agent:", manager.user_agent())

    # The client loads the session itself; session_source accepts the manager
    # object too, e.g. FirefoxSessionManager(interactive=False) for headless runs.
    with ArenaClient(session_source="firefox") as arena:
        print("TLS fingerprint:", arena.transport.impersonate or "httpx (no impersonation)")
        models = arena.get_models()
        print("Known model choices:", ", ".join(model.id for model in models[:10]))
        print("Chat queue endpoint:", arena.get_endpoint("chat").fn_index)

        print("Streaming: ", end="", flush=True)
        for chunk in arena.stream(
            "Count from one to three, separated by commas.",
            config=GenerationConfig(queue_timeout=120),
        ):
            sys.stdout.write(chunk.delta)
            sys.stdout.flush()
        print()


if __name__ == "__main__":
    main()
