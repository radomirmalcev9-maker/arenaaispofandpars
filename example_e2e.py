"""Run with:

    python -m pip install '.[e2e]'
    playwright install chromium
    export E2E_BASE_URL=https://your-staging.example
    export E2E_USER=you@example.com
    export E2E_PASS=...
    python example_e2e.py              # full journey (needs a browser)
    python example_e2e.py --check      # fast HTTP validation only, no browser

A synthetic-monitoring tick against a host you own or are authorised to test:

1. validate the stored session with one cheap ``curl_cffi`` request;
2. if it is stale, re-authenticate through a real credentialed login;
3. drive a scripted journey in a normalised, deterministic browser context
   with kinematic pointer input;
4. after every navigation, check whether a verification challenge was served;
   if one was, capture diagnostics and stop the run.

The run identifies itself with the ``X-Synthetic-Monitor`` header and
``window.__arenaSynthetic`` so the site owner can allowlist it. See the
"E2E-инфраструктура" section of the README for the supported alternatives.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from arena_api import ChallengeRequiredError
from arena_api.auth import (
    BrowserEnvironment,
    BrowserEnvironmentOptions,
    BrowserSessionManager,
    ChallengeHandler,
    DeterminismOptions,
    FormLoginFlow,
)

ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", "./artifacts"))
STATE_PATH = ARTIFACTS / "state" / "cookies.json"
CHALLENGE_DIR = ARTIFACTS / "challenges"

DEFAULT_HOVER = os.environ.get("E2E_HOVER_SELECTOR", "nav a")
DEFAULT_CLICK = os.environ.get("E2E_CLICK_SELECTOR", "main a")
DEFAULT_SUCCESS = os.environ.get("E2E_SUCCESS_SELECTOR", "main")


def require_base_url() -> str:
    base_url = os.environ.get("E2E_BASE_URL")
    if not base_url:
        raise SystemExit(
            "Set E2E_BASE_URL to a host you are authorised to test, "
            "for example: export E2E_BASE_URL=https://your-staging.example"
        )
    return base_url


def build_manager(base_url: str) -> BrowserSessionManager:
    """Manager whose only refresh path is an ordinary login with your credentials."""
    return BrowserSessionManager(
        base_url=base_url,
        storage_path=STATE_PATH,
        validate_paths=(
            os.environ.get("E2E_VALIDATE_PATH", "/"),
        ),
        challenge_handler=ChallengeHandler(
            capture_dir=CHALLENGE_DIR, raise_on_detection=False
        ),
        login_flow=FormLoginFlow(
            login_url=os.environ.get("E2E_LOGIN_URL", f"{base_url}/login"),
            username_selector=os.environ.get("E2E_USER_SELECTOR", "input[name=email]"),
            password_selector=os.environ.get("E2E_PASS_SELECTOR", "input[name=password]"),
            submit_selector=os.environ.get("E2E_SUBMIT_SELECTOR", "button[type=submit]"),
            success_url_contains=os.environ.get("E2E_SUCCESS_URL", "/app"),
            username_env="E2E_USER",
            password_env="E2E_PASS",
            human_input=os.environ.get("E2E_HUMAN_INPUT", "1") == "1",
        ),
        environment_options=build_environment_options(),
    )


def build_environment_options() -> BrowserEnvironmentOptions:
    """One normalised, deterministic context configuration for the whole run."""
    return BrowserEnvironmentOptions(
        headless=os.environ.get("E2E_HEADED", "") == "",
        locale=os.environ.get("E2E_LOCALE", "en-US"),
        timezone_id=os.environ.get("E2E_TZ", "UTC"),
        viewport={"width": 1440, "height": 900},
        reduced_motion="reduce",
        determinism=DeterminismOptions(
            random_seed=int(os.environ.get("E2E_SEED", "1337"))
        ),
        artifacts_dir=ARTIFACTS,
        record_trace=os.environ.get("E2E_TRACE", "1") == "1",
        record_har=os.environ.get("E2E_HAR", "") == "1",
        block_resource_types=("media", "font"),
    )


# --------------------------------------------------------------------------- #
# Fast path
# --------------------------------------------------------------------------- #


def check_session(base_url: str) -> int:
    """Validate the stored session over HTTP only. No browser is launched."""
    manager = build_manager(base_url)
    print(f"target          : {base_url}")
    print(f"state file      : {STATE_PATH}")

    if not manager.load_cookies():
        print("stored state    : (none -- the first run will sign in)")
    result = manager.validate()
    print(f"validation      : {result.describe()}")
    print(f"latency         : {result.latency * 1000:.0f} ms")

    if result.challenge is not None and result.challenge.is_challenge:
        print()
        print("A verification challenge was served.")
        print("Fix it at the infrastructure level -- see the remediation steps in")
        print("arena_api.auth.challenge.REMEDIATION_TEMPLATE.")
        return 2
    if not result.ok:
        print()
        print("Session is not usable. Run without --check to sign in with the")
        print("credentials in E2E_USER / E2E_PASS and refresh the stored state.")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Full journey
# --------------------------------------------------------------------------- #


async def run_journey(base_url: str) -> int:
    manager = build_manager(base_url)
    handler = manager.challenge_handler
    assert handler is not None

    print(f"target          : {base_url}")
    result = manager.validate()
    print(f"validation      : {result.describe()}")

    if not result.ok:
        print("session stale   : signing in with the configured credentials")
        cookies = await manager.arefresh_session(reason=None)
        print(f"refreshed       : {', '.join(sorted(cookies))}")
        result = manager.validate()
        print(f"re-validated    : {result.describe()}")
        if not result.ok:
            print("the refreshed session was still rejected; stopping")
            return 1

    options = manager.environment_options or build_environment_options()
    async with BrowserEnvironment(options) as environment:
        context = await environment.new_context()
        page = await environment.new_page(context)
        await handler.install(page)

        await page.goto(base_url, wait_until="domcontentloaded")
        observation = await handler.check(page)
        print(f"landing page    : {observation.kind.value} (clean={not observation.is_challenge})")
        if observation.is_challenge:
            handler.raise_for(observation)

        # Let fonts settle and animation frames drain before asserting layout.
        await page.evaluate("() => window.__arenaTestHooks.drain(3)")
        print(f"determinism     : seeded={await page.evaluate('() => Math.random.__seeded === true')}")
        print(f"identified as   : synthetic={await page.evaluate('() => window.__arenaSynthetic === true')}")

        mouse = environment.mouse(page, seed=int(os.environ.get("E2E_SEED", "1337")))
        try:
            trajectory = await mouse.hover(DEFAULT_HOVER)
            print(
                f"hover           : {len(trajectory)} pointer samples, "
                f"{trajectory.settle_time * 1000:.0f} ms, "
                f"straightness {trajectory.straightness:.3f}"
            )
            await mouse.click(DEFAULT_CLICK)
            print("click           : ok")
        except Exception as exc:  # selector not present on this host
            print(f"journey         : skipped ({type(exc).__name__}: {exc})")

        await page.wait_for_selector(DEFAULT_SUCCESS, timeout=10_000)
        shot = await environment.screenshot(page, "journey.png")
        print(f"screenshot      : {shot}")

        await environment.save_storage_state(context, str(STATE_PATH))
        print(f"state saved     : {STATE_PATH}")

    print()
    print("Done. Artefacts are in", ARTIFACTS)
    return 0


def main() -> None:
    base_url = require_base_url()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    if "--check" in sys.argv:
        raise SystemExit(check_session(base_url))

    try:
        import playwright  # noqa: F401
    except ImportError:
        raise SystemExit(
            "Playwright is not installed. Run: python -m pip install '.[e2e]' "
            "&& playwright install chromium"
        ) from None

    try:
        raise SystemExit(asyncio.run(run_journey(base_url)))
    except ChallengeRequiredError as exc:
        print()
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
