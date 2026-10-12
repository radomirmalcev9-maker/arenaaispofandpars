"""Run with:

    python -m playwright install chromium       # the only dependency
    export CAPTCHA_TARGET_URL=https://your-staging.example/login
    python example_challenge_wait.py            # wait the screen out headless
    CAPTCHA_HEADED=1 python example_challenge_wait.py   # solve it yourself, in a window

Clearing the verification screens that have **no token to produce**.

``example_auth_captcha.py`` covers reCAPTCHA, where a real
``g-recaptcha-response`` token can be produced. Half of the classified kinds
have no such token, and they do not all need one:

* **Turnstile** and a **managed Cloudflare interstitial** usually pass on their
  own once an ordinary browser sits on them for a few seconds -- the right move
  is to keep re-scanning the page until it stops looking like a challenge;
* **hCaptcha**, **DataDome** and anything unrecognised need a person. With
  ``CAPTCHA_HEADED=1`` this script opens a real window, tells you what is on
  screen, and polls while you click; when the time runs out it offers another
  window in exchange for an Enter press.

``ChallengeWaitSolver`` needs no third-party package at all -- only Playwright
and the page it is given. ``CompositeSolver`` dispatches by kind, so the same run
also solves a reCAPTCHA if ``example_auth_captcha.py``'s dependencies happen to
be installed.

Point it at a host you own or are explicitly authorised to test; the durable fix
for a monitoring pipeline is still an allowlist rule, see
``arena_api.auth.challenge.REMEDIATION_TEMPLATE``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from importlib.util import find_spec
from pathlib import Path

from arena_api.auth import (
    BrowserEnvironment,
    BrowserEnvironmentOptions,
    ChallengeWaitSolver,
    CompositeSolver,
    RecaptchaToolkitSolver,
    inspect_page,
)
from arena_api.exceptions import ChallengeRequiredError

ARTIFACTS = Path(os.environ.get("CAPTCHA_ARTIFACTS", "./artifacts"))


def require_target_url() -> str:
    url = os.environ.get("CAPTCHA_TARGET_URL")
    if not url:
        raise SystemExit(
            "Set CAPTCHA_TARGET_URL to a page you are authorised to test, for "
            "example: export CAPTCHA_TARGET_URL=https://your-staging.example/login"
        )
    return url


def toolkit_available() -> bool:
    """Whether a token can be produced at all (audio/image backend installed)."""
    return find_spec("playwright_recaptcha") is not None and (
        find_spec("playwright_stealth") is not None
    )


def build_solver(headed: bool) -> CompositeSolver:
    """One solver per capability, in dispatch order.

    Order matters: the first member whose ``supports()`` accepts the observation
    wins, so the token bridge is tried before patience.
    """
    solvers: list = []
    if toolkit_available():
        solvers.append(RecaptchaToolkitSolver(backend=os.environ.get("CAPTCHA_BACKEND", "audio")))
    solvers.append(
        ChallengeWaitSolver(
            # Seconds to poll before reporting the challenge as unsolved. A
            # managed interstitial that is going to pass does so in a few.
            timeout=float(os.environ.get("CAPTCHA_WAIT_TIMEOUT", "45")),
            poll=1.0,
            # Headed: announce what is on screen and let a human extend the wait.
            interactive=headed,
            rounds=3,
        )
    )
    return CompositeSolver(*solvers)


async def wait_out(url: str, headed: bool) -> int:
    """Open the page, report what is blocking it, and try to get past it."""
    options = BrowserEnvironmentOptions.with_captcha_solving(
        build_solver(headed),
        headless=not headed,
        artifacts_dir=ARTIFACTS,
    )
    async with BrowserEnvironment(options) as environment:
        page = await environment.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        observation = await inspect_page(page, capture_dir=ARTIFACTS)
        print(f"detected        : {observation.summary()}")
        if not observation.is_challenge:
            print("nothing to wait out")
            return 0

        handler = environment.challenge_handler
        assert handler is not None  # with_captcha_solving installed one
        print(f"will solve      : {handler.can_solve(observation)}")

        final = await handler.solve_page(page)
        for attempt in handler.solve_attempts:
            # A wait reports cleared=True with no token; a token bridge reports
            # the backend and version it used.
            print(f"attempt         : {attempt.describe()}")
        print(f"after           : {final.summary()}")
        print(f"cookies         : {', '.join(sorted(c['name'] for c in await page.context.cookies()))}")
        return 0 if not final.is_challenge else 2


def main() -> None:
    url = require_target_url()
    if find_spec("playwright") is None:
        raise SystemExit(
            "Playwright is not installed. Run: python -m playwright install chromium"
        )
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    headed = os.environ.get("CAPTCHA_HEADED", "") != ""

    try:
        raise SystemExit(asyncio.run(wait_out(url, headed)))
    except ChallengeRequiredError as exc:
        # Raised when the challenge survived every attempt: the message carries
        # each attempt's reason plus the remediation guide.
        print()
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
