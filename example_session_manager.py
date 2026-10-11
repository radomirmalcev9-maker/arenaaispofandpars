"""Run with: python example_session_manager.py

Shows the session-lifecycle half of ``arena_api.auth``: a normalized cookie jar,
a private on-disk store, a liveness probe, and a refresh policy. The recovery
path when a site asks for a human verification is a *checkpoint* — a browser
window that waits for you — never a solver.

Optional extras:

    python -m pip install '.[firefox]'   # read the local Firefox profile
    python -m pip install '.[browser]'   # capture a Playwright storage_state
    python -m playwright install chromium
"""

from __future__ import annotations

import sys
from pathlib import Path

from arena_api import ArenaClient
from arena_api.auth import (
    AutoSessionManager,
    CookieJar,
    FirefoxSource,
    HttpLivenessProbe,
    ManualCheckpoint,
    PlaywrightStorageSource,
    RefreshPolicy,
    SessionStore,
    SessionStoreError,
)
from arena_api.exceptions import AuthenticationError, ChallengeRequiredError

SESSION_FILE = Path.home() / ".cache" / "arena-api" / "session.json"
DOMAINS = ("arena.ai", "lmarena.ai")


def build_manager() -> AutoSessionManager:
    """Prefer the local Firefox profile; fall back to a Playwright state file.

    Both sources are *yours*: the profile you are signed into, or a state file
    written after you logged in yourself. ``ManualCheckpoint`` is what runs when
    the service wants a person -- it prints a prompt, waits for Enter, and
    re-reads the source.
    """
    try:
        from arena_api.auth import FirefoxSessionManager

        source: object = FirefoxSource(FirefoxSessionManager(domains=DOMAINS))
        print("source: local Firefox profile")
    except ImportError:
        state = SESSION_FILE.with_name("storage_state.json")
        if state.is_file():
            source = PlaywrightStorageSource(state)
            print(f"source: Playwright storage_state at {state}")
        else:
            raise SystemExit(
                "No session source available. Either `pip install '.[firefox]'` "
                f"and sign in to {DOMAINS[0]} in Firefox, or capture a storage "
                "state with arena_api.auth.verify.capture_storage_state()."
            ) from None

    try:
        store: SessionStore | None = SessionStore(SESSION_FILE)
        store.load()  # surface a corrupt file now, not inside a retry loop
    except SessionStoreError as exc:
        print(f"warning: ignoring unusable session file: {exc}")
        store = None

    return AutoSessionManager(
        source,  # type: ignore[arg-type]
        domains=DOMAINS,
        store=store,
        policy=RefreshPolicy(
            cookie_ttl=900.0,  # re-read the profile after 15 minutes
            min_interval=60.0,  # never hammer the profile/auth endpoints
            max_attempts=3,
            retry_backoff=0.5,
            liveness_interval=900.0,
            expiry_margin=45.0,
        ),
        probe=HttpLivenessProbe(f"https://{DOMAINS[0]}/config"),
        checkpoint=ManualCheckpoint(domains=DOMAINS, url_template="https://{host}/"),
    )


def main() -> int:
    manager = build_manager()

    with manager:  # close() stops the refresher and persists the jar
        try:
            jar: CookieJar = manager.ensure()
        except (AuthenticationError, ChallengeRequiredError) as exc:
            print(f"no usable session yet: {exc}")
            return 1

        print(f"cookies: {', '.join(sorted(jar.names)) or '(none)'}")
        if jar.earliest_expiry():
            print(f"earliest expiry: {jar.earliest_expiry():.0f} (epoch seconds)")
        print(f"Cookie header: {jar.cookie_header(DOMAINS[0])[:70]}...")

        # A real client consumes the same manager through the session_source
        # protocol: load_cookies(), firefox_headers(), refresh_session().
        try:
            with ArenaClient(session_source=manager) as client:
                models = client.get_models()
                print("models:", ", ".join(model.id for model in models[:6]))
        except ChallengeRequiredError as exc:
            # The client asked the checkpoint (a human), the check happened, and
            # the retry still failed -- so the session really is not usable.
            print(f"manual verification required: {exc}")
            print("sign in at https://arena.ai in Firefox/Playwright and rerun.")
            return 1

        print("stats:", manager.stats.as_dict())
        manager.save_to_store(metadata={"written_by": "example_session_manager.py"})
        print("stored at:", SESSION_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
