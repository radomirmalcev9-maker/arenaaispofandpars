"""Run with:

    python -m pip install '.[auth-captcha]'      # audio/image backend + stealth
    python -m pip install '.[e2e,vision]'        # or the local YOLO backend
    python -m playwright install chromium
    export CAPTCHA_TARGET_URL=https://your-staging.example/login
    python example_auth_captcha.py               # open the page and solve it
    python example_auth_captcha.py --refresh     # challenge-only session refresh

Opt-in reCAPTCHA solving inside an ``arena_api.auth`` run.

Point it at a host you own or are explicitly authorised to test: solving a
challenge on someone else's service is a different conversation from monitoring
your own staging zone, where the durable fix is an allowlist rule (see
``arena_api.auth.challenge.REMEDIATION_TEMPLATE``).

What happens here:

1. ``RecaptchaToolkitSolver`` wraps ``recaptcha_toolkit`` -- it picks v2 or v3
   from the page, applies stealth, and solves on the page ``arena_api.auth``
   already opened (no second browser);
2. ``BrowserEnvironmentOptions.with_captcha_solving`` installs a solving
   ``ChallengeHandler`` on every page and drops the synthetic-monitor
   announcements, which would contradict the masking;
3. ``--refresh`` shows the session-manager path: a session that is valid but
   *blocked* is renewed by opening ``captcha_url``, clearing the screen and
   persisting the cookies -- no login flow needed.
"""

from __future__ import annotations

from importlib.util import find_spec
import asyncio
import os
import sys
from pathlib import Path

from arena_api.auth import (
    BrowserEnvironment,
    BrowserEnvironmentOptions,
    BrowserSessionManager,
    ChallengeHandler,
    RecaptchaToolkitSolver,
)
from arena_api.exceptions import ChallengeRequiredError

ARTIFACTS = Path(os.environ.get("CAPTCHA_ARTIFACTS", "./artifacts"))
STATE_PATH = ARTIFACTS / "state" / "cookies.json"


def require_target_url() -> str:
    url = os.environ.get("CAPTCHA_TARGET_URL")
    if not url:
        raise SystemExit(
            "Set CAPTCHA_TARGET_URL to a page you are authorised to test, for "
            "example: export CAPTCHA_TARGET_URL=https://your-staging.example/login"
        )
    return url


def build_solver() -> RecaptchaToolkitSolver:
    """Solver configuration, straight from the environment."""
    return RecaptchaToolkitSolver(
        # "audio" (Google Speech, needs FFmpeg), "image" (CapSolver, needs
        # CAPSOLVER_API_KEY) or "vision_ai" (local YOLO, own browser).
        backend=os.environ.get("CAPTCHA_BACKEND", "audio"),
        # None inspects the page; "v2"/"v3" pins the path.
        version=os.environ.get("CAPTCHA_VERSION") or None,
        apply_stealth=os.environ.get("CAPTCHA_STEALTH", "1") == "1",
        # solve_kinds defaults to reCAPTCHA only. Add allow_mixed_signals=True
        # if the widget sits inside another verification screen and you know
        # clearing it is enough.
    )


def build_environment_options() -> BrowserEnvironmentOptions:
    """One configuration for the run: solving on, announcements off."""
    return BrowserEnvironmentOptions.with_captcha_solving(
        build_solver(),
        headless=os.environ.get("CAPTCHA_HEADED", "") == "",
        locale=os.environ.get("CAPTCHA_LOCALE", "en-US"),
        timezone_id=os.environ.get("CAPTCHA_TZ", "UTC"),
        viewport={"width": 1440, "height": 900},
        artifacts_dir=ARTIFACTS,
        record_trace=os.environ.get("CAPTCHA_TRACE", "1") == "1",
    )


# --------------------------------------------------------------------------- #
# Solve on a page
# --------------------------------------------------------------------------- #


async def solve_on_page(url: str) -> int:
    print(f"target          : {url}")
    print(f"solver          : {build_solver().describe()}")

    async with BrowserEnvironment(build_environment_options()) as environment:
        handler = environment.challenge_handler
        assert isinstance(handler, ChallengeHandler)

        page = await environment.new_page()   # the solving handler is installed here
        await page.goto(url, wait_until="domcontentloaded")

        # Challenges that appear while loading are solved by the listener; this
        # explicit call covers the assertion point and reports what happened.
        observation = await environment.solve_challenge(page, raise_on_failure=False)

        print(f"challenge       : {observation.kind.value}")
        for attempt in handler.solve_attempts:
            print(f"attempt         : {attempt.describe()}")
        if not handler.solve_attempts:
            print("attempt         : (none -- the page carried no reCAPTCHA)")

        if observation.is_challenge:
            print()
            print("The challenge survived. Re-raise with the full report:")
            handler.raise_for(observation, attempts=handler.solve_attempts)
            return 2

        shot = await environment.screenshot(page, "after-solve.png")
        print(f"screenshot      : {shot}")
        print(f"state saved     : {await environment.save_storage_state(page.context, 'state.json')}")
    return 0


# --------------------------------------------------------------------------- #
# Challenge-only session refresh
# --------------------------------------------------------------------------- #


def refresh_blocked_session(url: str) -> int:
    """Renew a session that is valid but blocked by a verification screen."""
    manager = BrowserSessionManager(
        base_url=url,
        storage_path=STATE_PATH,
        captcha_solver=build_solver(),
        # No login_flow: clearing the screen is the whole refresh.
        environment_options=build_environment_options(),
    )
    print(f"target          : {url}")
    print(f"state file      : {STATE_PATH}")

    result = manager.validate()
    print(f"validation      : {result.describe()}")
    if result.ok:
        print("session is already usable; nothing to solve")
        return 0

    cookies = manager.refresh_session()
    print(f"solved          : {', '.join(sorted(cookies or {}))}")
    handler = manager.challenge_handler
    for attempt in handler.solve_attempts if handler else ():
        print(f"attempt         : {attempt.describe()}")
    print(f"re-validated    : {manager.validate().describe()}")
    return 0


def main() -> None:
    url = require_target_url()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    if find_spec("playwright") is None:
        raise SystemExit(
            "Playwright is not installed. Run: python -m pip install "
            "'.[auth-captcha]' && playwright install chromium"
        )

    try:
        if "--refresh" in sys.argv:
            raise SystemExit(refresh_blocked_session(url))
        raise SystemExit(asyncio.run(solve_on_page(url)))
    except ChallengeRequiredError as exc:
        print()
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
