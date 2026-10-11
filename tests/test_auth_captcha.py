"""Tests for the opt-in reCAPTCHA bridge (arena_api.auth <-> recaptcha_toolkit).

Everything here runs offline: the toolkit's solver backends are stubbed at the
seam the bridge uses (its bot factory, the stealth applier, the site-key
detector), so no browser, FFmpeg or CapSolver key is required.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

import recaptcha_toolkit
from arena_api.auth.browser import (
    SYNTHETIC_MONITOR_HEADER,
    BrowserEnvironment,
    BrowserEnvironmentOptions,
)
from arena_api.auth.captcha import (
    CaptchaAttempt,
    CaptchaSolver,
    RecaptchaToolkitSolver,
    detect_recaptcha_version,
    detect_recaptcha_version_sync,
    observation_targets_recaptcha,
)
from arena_api.auth.challenge import (
    SOLVABLE_CHALLENGE_KINDS,
    ChallengeHandler,
    ChallengeKind,
    ChallengeObservation,
    ChallengeSignal,
)
from arena_api.auth.environment import build_init_script
from arena_api.auth.session import BrowserSessionManager
from arena_api.exceptions import (
    AuthenticationError,
    CaptchaSolveError,
    ChallengeRequiredError,
)

RECAPTCHA_SCAN = {
    "selectors": [".g-recaptcha"],
    "frames": ["https://www.google.com/recaptcha/api2/anchor?ar=1"],
    "text": "Please complete the security check",
    "title": "Sign in",
}
CLEAN_SCAN = {"selectors": [], "frames": [], "text": "Welcome back", "title": "app"}
TURNSTILE_SCAN = {
    "selectors": [".cf-turnstile"],
    "frames": [],
    "text": "Just a moment",
    "title": "Just a moment",
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeResponse:
    def __init__(self, url: str, status: int, headers: dict, text: str) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._text = text

    async def text(self) -> str:
        return self._text


class FakeContext:
    """Minimal browser context: pages, cookies and storage state."""

    def __init__(self, page: "FakePage | None" = None) -> None:
        self.pages: list[FakePage] = []
        self.timeouts: dict[str, float] = {}
        self.storage_calls: list[str | None] = []
        self.closed = False
        self.cookies_payload = [{"name": "session", "value": "abc", "domain": "example.test"}]
        self.storage_payload = {
            "cookies": [
                {
                    "name": "session",
                    "value": "fresh",
                    "domain": "example.test",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                }
            ],
            "origins": [],
        }
        if page is not None:
            page.context = self
            self.pages.append(page)

    async def new_page(self) -> "FakePage":
        if self.pages:
            return self.pages[0]
        page = FakePage(context=self)
        self.pages.append(page)
        return page

    async def cookies(self) -> list[dict]:
        return list(self.cookies_payload)

    def set_default_timeout(self, value: float) -> None:
        self.timeouts["default"] = value

    def set_default_navigation_timeout(self, value: float) -> None:
        self.timeouts["navigation"] = value

    async def storage_state(self, path: str | None = None) -> dict:
        self.storage_calls.append(path)
        if path:
            Path(path).write_text(json.dumps(self.storage_payload), encoding="utf-8")
        return self.storage_payload

    async def close(self) -> None:
        self.closed = True


class FakePage:
    """Page stand-in that can flip from "challenged" to "clean"."""

    def __init__(
        self,
        scan: dict | None = None,
        *,
        version: str = "v2",
        context: FakeContext | None = None,
        url: str = "https://example.test/login",
        challenges_before_clean: int = 0,
    ) -> None:
        self.scan = dict(scan if scan is not None else RECAPTCHA_SCAN)
        self.version = version
        self.context = context
        self.url = url
        self.frames = [FakeFrame(item) for item in self.scan.get("frames", [])]
        self.listeners: dict[str, list] = {}
        self.init_scripts: list[str] = []
        self.routes: list[tuple[str, object]] = []
        self.gotos: list[dict] = []
        self.evaluations: list[str] = []
        self.version_probes = 0
        self.screenshots: list[str] = []
        #: How many solves must happen before the DOM scan turns clean.
        self.challenges_before_clean = challenges_before_clean

    async def evaluate(self, script: str, arg=None):
        if "data-size" in script or "getBoundingClientRect" in script:
            self.version_probes += 1
            return self.version
        self.evaluations.append(script)
        return dict(self.scan)

    def clear_challenge(self) -> None:
        """Simulate a solved widget: the DOM no longer matches any marker."""
        self.scan = dict(CLEAN_SCAN)
        self.frames = []

    async def goto(self, url: str, **kwargs) -> None:
        self.gotos.append({"url": url, **kwargs})

    async def add_init_script(self, script: str | None = None, path: str | None = None) -> None:
        self.init_scripts.append(script or "")

    async def route(self, url: str, handler) -> None:
        self.routes.append((url, handler))

    async def content(self) -> str:
        return "<html><body>app</body></html>"

    async def screenshot(self, path: str | None = None, **kwargs) -> bytes:
        self.screenshots.append(str(path))
        if path:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return b""

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)


class FakeSolver:
    """In-test solver: clears the page after ``failures`` failed attempts."""

    def __init__(
        self,
        *,
        failures: int = 0,
        kinds: tuple[ChallengeKind, ...] = (ChallengeKind.RECAPTCHA,),
        token: str = "token-abc",
    ) -> None:
        self.failures = failures
        self.kinds = kinds
        self.token = token
        self.pages: list[FakePage] = []
        self.attempts: list[CaptchaAttempt] = []

    def supports(self, observation: ChallengeObservation) -> bool:
        return observation.is_challenge and observation.kind in self.kinds

    async def asolve(
        self, page: FakePage, observation: ChallengeObservation | None = None
    ) -> CaptchaAttempt:
        self.pages.append(page)
        if self.failures > 0:
            self.failures -= 1
            attempt = CaptchaAttempt(
                backend="fake", version="v2", error="RecaptchaTimeoutError: boom",
                observation=observation,
            )
        else:
            page.clear_challenge()
            attempt = CaptchaAttempt(
                token=self.token, backend="fake", version="v2", observation=observation
            )
        self.attempts.append(attempt)
        return attempt

    def solve(
        self, page: FakePage, observation: ChallengeObservation | None = None
    ) -> CaptchaAttempt:
        return asyncio.run(self.asolve(page, observation))


class FakeBot:
    """Stands in for a toolkit bot used as a solver facade."""

    def __init__(self, token: str = "tok-123", error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.v2_calls: list[tuple[object, dict]] = []
        self.v3_calls: list[tuple[object, dict]] = []

    async def solve_recaptcha_v2(self, page, **kwargs) -> str:
        self.v2_calls.append((page, kwargs))
        if self.error is not None:
            raise self.error
        return self.token

    async def solve_recaptcha_v3(self, page, **kwargs) -> str:
        self.v3_calls.append((page, kwargs))
        if self.error is not None:
            raise self.error
        return self.token

    def solve_recaptcha_v2_sync(self, page, **kwargs) -> str:
        self.v2_calls.append((page, kwargs))
        if self.error is not None:
            raise self.error
        return self.token

    def solve_recaptcha_v3_sync(self, page, **kwargs) -> str:
        self.v3_calls.append((page, kwargs))
        if self.error is not None:
            raise self.error
        return self.token


class SyncFakePage:
    """Sync twin of :class:`FakePage` for the blocking solver path."""

    def __init__(self, version: str = "v2", url: str = "https://example.test/login") -> None:
        self.version = version
        self.url = url
        self.context = None
        self.evaluations: list[str] = []

    def evaluate(self, script: str, arg=None):
        if "data-size" in script or "getBoundingClientRect" in script:
            return self.version
        self.evaluations.append(script)
        return dict(RECAPTCHA_SCAN)


class FakeVisionSolver:
    """Stands in for ``recaptcha_toolkit.VisionAISolver``."""

    last: "FakeVisionSolver | None" = None

    def __init__(self, options=None) -> None:
        self.options = options
        self.calls: list[dict] = []
        FakeVisionSolver.last = self

    async def solve_async(self, **kwargs):
        self.calls.append(kwargs)
        return recaptcha_toolkit.vision_ai.VisionAIResult(
            token="yolo-token", captcha_type="dynamic_3x3", elapsed=1.0, attempts=2
        )

    def solve_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return recaptcha_toolkit.vision_ai.VisionAIResult(token="yolo-token")


def use_bot(monkeypatch, solver: RecaptchaToolkitSolver, bot: FakeBot) -> FakeBot:
    """Route the bridge's async bot factory to ``bot``."""
    monkeypatch.setattr(solver, "_async_bot", lambda toolkit, backend: bot)
    return bot


def use_sync_bot(monkeypatch, solver: RecaptchaToolkitSolver, bot: FakeBot) -> FakeBot:
    monkeypatch.setattr(solver, "_sync_bot", lambda toolkit, backend: bot)
    return bot


def record_stealth(monkeypatch) -> list:
    """Capture stealth applications without needing playwright-stealth."""
    applied: list = []

    async def fake_async(page, options=None):
        applied.append(("async", page, options))
        return page

    def fake_sync(page, options=None):
        applied.append(("sync", page, options))
        return page

    monkeypatch.setattr(recaptcha_toolkit.stealth, "apply_stealth_async", fake_async)
    monkeypatch.setattr(recaptcha_toolkit.stealth, "apply_stealth_sync", fake_sync)
    return applied


def recaptcha_observation(kind: ChallengeKind = ChallengeKind.RECAPTCHA) -> ChallengeObservation:
    return ChallengeObservation(
        url="https://example.test/login",
        kind=kind,
        signals=(ChallengeSignal("dom", "matched .g-recaptcha", ChallengeKind.RECAPTCHA),),
        status=200,
    )


def turnstile_observation() -> ChallengeObservation:
    return ChallengeObservation(
        url="https://example.test/login",
        kind=ChallengeKind.TURNSTILE,
        signals=(ChallengeSignal("dom", "matched .cf-turnstile", ChallengeKind.TURNSTILE),),
        status=403,
    )


# --------------------------------------------------------------------------- #
# Capability
# --------------------------------------------------------------------------- #


def test_solvable_kinds_cover_recaptcha_only() -> None:
    assert SOLVABLE_CHALLENGE_KINDS == frozenset({ChallengeKind.RECAPTCHA})


def test_solver_supports_recaptcha_and_nothing_else() -> None:
    solver = RecaptchaToolkitSolver()
    assert solver.supports(recaptcha_observation()) is True
    assert solver.supports(turnstile_observation()) is False
    clean = ChallengeObservation(url="https://example.test/", kind=ChallengeKind.NONE, signals=())
    assert solver.supports(clean) is False


def test_mixed_signals_need_an_explicit_opt_in() -> None:
    """A reCAPTCHA inside a managed challenge is not worth solving by default."""
    mixed = ChallengeObservation(
        url="https://example.test/",
        kind=ChallengeKind.CLOUDFLARE_MANAGED,
        signals=(
            ChallengeSignal("header", "cf-mitigated: challenge", ChallengeKind.CLOUDFLARE_MANAGED),
            ChallengeSignal("dom", "matched .g-recaptcha", ChallengeKind.RECAPTCHA),
        ),
    )
    assert observation_targets_recaptcha(mixed) is True
    assert RecaptchaToolkitSolver().supports(mixed) is False
    assert RecaptchaToolkitSolver(allow_mixed_signals=True).supports(mixed) is True


def test_solve_kinds_are_validated() -> None:
    with pytest.raises(ValueError):
        RecaptchaToolkitSolver(solve_kinds=())
    with pytest.raises(TypeError):
        RecaptchaToolkitSolver(solve_kinds=("recaptcha",))  # type: ignore[arg-type]


def test_solver_protocol_is_satisfied() -> None:
    assert isinstance(RecaptchaToolkitSolver(), CaptchaSolver)
    assert isinstance(FakeSolver(), CaptchaSolver)


def test_describe_reports_backend_and_posture() -> None:
    assert RecaptchaToolkitSolver().describe() == "recaptcha_toolkit[audio/auto/stealth]"
    stealthless = RecaptchaToolkitSolver(backend="vision_ai", apply_stealth=False)
    assert stealthless.describe() == "recaptcha_toolkit[vision_ai/auto/no-stealth]"


# --------------------------------------------------------------------------- #
# Attempt object
# --------------------------------------------------------------------------- #


def test_attempt_reports_success_and_failure() -> None:
    ok = CaptchaAttempt(token="abc", backend="audio", version="v2", duration=1.5)
    assert ok.ok is True
    assert "solved" in ok.describe()
    assert ok.raise_for_status() == "abc"

    failed = CaptchaAttempt(backend="audio", version="v2", error="RecaptchaTimeoutError: x")
    assert failed.ok is False
    assert "failed" in failed.describe()
    with pytest.raises(CaptchaSolveError):
        failed.raise_for_status()


def test_captcha_solve_error_is_a_challenge_error() -> None:
    """Existing `except ChallengeRequiredError` handlers keep working."""
    assert issubclass(CaptchaSolveError, ChallengeRequiredError)


# --------------------------------------------------------------------------- #
# Version detection
# --------------------------------------------------------------------------- #


def test_version_detection_reads_the_page() -> None:
    page = FakePage(version="v3")
    version = asyncio.run(detect_recaptcha_version(page))
    assert version is recaptcha_toolkit.RecaptchaVersion.V3
    assert page.version_probes == 1


def test_version_detection_defaults_to_v2_on_garbage() -> None:
    class Broken(FakePage):
        async def evaluate(self, script, arg=None):
            raise RuntimeError("page closed")

    version = asyncio.run(detect_recaptcha_version(Broken()))
    assert version is recaptcha_toolkit.RecaptchaVersion.V2


def test_version_override_skips_the_page_probe() -> None:
    page = FakePage(version="v3")
    version = asyncio.run(detect_recaptcha_version(page, override="v2"))
    assert version is recaptcha_toolkit.RecaptchaVersion.V2
    assert page.version_probes == 0


def test_sync_version_detection_mirrors_async() -> None:
    page = SyncFakePage(version="v3")
    assert (
        detect_recaptcha_version_sync(page)
        is recaptcha_toolkit.RecaptchaVersion.V3
    )


# --------------------------------------------------------------------------- #
# Solver bridge
# --------------------------------------------------------------------------- #


def test_v2_solve_returns_the_token(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(apply_stealth=False)
    bot = use_bot(monkeypatch, solver, FakeBot(token="tok-v2"))
    page = FakePage(version="v2")

    attempt = asyncio.run(solver.asolve(page))

    assert attempt.ok is True
    assert attempt.token == "tok-v2"
    assert attempt.backend == "audio"
    assert attempt.version == "v2"
    assert bot.v2_calls and bot.v2_calls[0][0] is page
    assert bot.v3_calls == []


def test_v3_solve_attaches_the_listener_before_a_reload(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(apply_stealth=False)
    bot = use_bot(monkeypatch, solver, FakeBot(token="tok-v3"))
    page = FakePage(version="v3")

    attempt = asyncio.run(solver.asolve(page))

    assert attempt.token == "tok-v3"
    assert attempt.version == "v3"
    assert bot.v3_calls[0][1] == {"reload": True}


def test_v3_reload_can_be_disabled(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(apply_stealth=False, reload_for_v3=False)
    bot = use_bot(monkeypatch, solver, FakeBot())

    asyncio.run(solver.asolve(FakePage(version="v3")))

    assert bot.v3_calls[0][1] == {"reload": False}


def test_stealth_is_applied_before_solving(monkeypatch) -> None:
    applied = record_stealth(monkeypatch)
    solver = RecaptchaToolkitSolver()
    use_bot(monkeypatch, solver, FakeBot())
    page = FakePage()

    attempt = asyncio.run(solver.asolve(page))

    assert attempt.stealth_applied is True
    assert applied[0][0] == "async" and applied[0][1] is page


def test_stealth_can_be_turned_off(monkeypatch) -> None:
    applied = record_stealth(monkeypatch)
    solver = RecaptchaToolkitSolver(apply_stealth=False)
    use_bot(monkeypatch, solver, FakeBot())

    attempt = asyncio.run(solver.asolve(FakePage()))

    assert attempt.stealth_applied is False
    assert applied == []


def test_solver_never_launches_its_own_browser(monkeypatch) -> None:
    """The bot is a facade over our page: entering it would start a 2nd Chromium."""
    solver = RecaptchaToolkitSolver(apply_stealth=False)
    built: list = []
    real_factory = solver._async_bot

    def factory(toolkit, backend):
        bot = real_factory(toolkit, backend)
        built.append(bot)

        async def fake_solve(page, **kwargs):
            return "tok"

        bot.solve_recaptcha_v2 = fake_solve
        return bot

    monkeypatch.setattr(solver, "_async_bot", factory)

    attempt = asyncio.run(solver.asolve(FakePage(version="v2")))

    assert attempt.ok is True
    bot = built[0]
    assert isinstance(bot, recaptcha_toolkit.AsyncPlaywrightBot)
    assert bot.context is None
    assert bot.browser is None
    assert bot.playwright is None


def test_solver_dependency_failure_is_reported_not_raised(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(apply_stealth=False)
    error = recaptcha_toolkit.SolverDependencyError("no ffmpeg", package="ffmpeg")
    use_bot(monkeypatch, solver, FakeBot(error=error))

    attempt = asyncio.run(solver.asolve(FakePage()))

    assert attempt.ok is False
    assert "SolverDependencyError" in (attempt.error or "")
    assert "ffmpeg" in (attempt.error or "")


def test_solver_passes_the_observation_through(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(apply_stealth=False)
    use_bot(monkeypatch, solver, FakeBot())
    observation = recaptcha_observation()

    attempt = asyncio.run(solver.asolve(FakePage(), observation))

    assert attempt.observation is observation


def test_vision_ai_backend_uses_the_site_key_and_shares_cookies(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(backend="vision_ai", apply_stealth=True)
    applied = record_stealth(monkeypatch)

    async def fake_detect(page):
        return "6Le-site-key"

    monkeypatch.setattr(recaptcha_toolkit.playwright_bot, "detect_site_key_async", fake_detect)
    monkeypatch.setattr(recaptcha_toolkit, "VisionAISolver", FakeVisionSolver)

    context = FakeContext()
    page = FakePage(version="v3", context=context)

    attempt = asyncio.run(solver.asolve(page))

    assert attempt.ok is True
    assert attempt.token == "yolo-token"
    assert attempt.site_key == "6Le-site-key"
    call = FakeVisionSolver.last.calls[0]  # type: ignore[union-attr]
    assert call["website_key"] == "6Le-site-key"
    assert call["website_url"] == page.url
    assert call["is_invisible"] is True
    assert call["cookies"] == context.cookies_payload
    # The YOLO solver runs in its own browser; patching our page is pointless.
    assert attempt.stealth_applied is False
    assert applied == []


def test_vision_ai_can_keep_cookies_local(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(backend="vision_ai", share_cookies_with_vision_ai=False)

    async def fake_detect(page):
        return "6Le-site-key"

    monkeypatch.setattr(recaptcha_toolkit.playwright_bot, "detect_site_key_async", fake_detect)
    monkeypatch.setattr(recaptcha_toolkit, "VisionAISolver", FakeVisionSolver)

    asyncio.run(solver.asolve(FakePage(context=FakeContext())))

    assert FakeVisionSolver.last.calls[0]["cookies"] is None  # type: ignore[union-attr]


def test_vision_ai_needs_a_page_url(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(backend="vision_ai")

    async def fake_detect(page):
        return "6Le-site-key"

    monkeypatch.setattr(recaptcha_toolkit.playwright_bot, "detect_site_key_async", fake_detect)
    monkeypatch.setattr(recaptcha_toolkit, "VisionAISolver", FakeVisionSolver)

    attempt = asyncio.run(solver.asolve(FakePage(url="")))

    assert attempt.ok is False
    assert "SiteKeyDetectionError" in (attempt.error or "")


def test_sync_solve_mirrors_the_async_path(monkeypatch) -> None:
    solver = RecaptchaToolkitSolver(apply_stealth=True)
    applied = record_stealth(monkeypatch)

    class SyncBot(FakeBot):
        def solve_recaptcha_v2(self, page, **kwargs) -> str:
            self.v2_calls.append((page, kwargs))
            return self.token

    bot = use_sync_bot(monkeypatch, solver, SyncBot(token="tok-sync"))
    page = SyncFakePage(version="v2")

    attempt = solver.solve(page)

    assert attempt.ok is True
    assert attempt.token == "tok-sync"
    assert bot.v2_calls[0][0] is page
    assert applied[0][0] == "sync"


# --------------------------------------------------------------------------- #
# ChallengeHandler integration
# --------------------------------------------------------------------------- #


def test_solving_is_off_by_default() -> None:
    """The documented posture stays observational unless asked otherwise."""
    solver = FakeSolver()
    handler = ChallengeHandler(captcha_solver=solver)
    page = FakePage()

    with pytest.raises(ChallengeRequiredError):
        asyncio.run(handler.check(page))

    assert solver.pages == []
    assert handler.solve_attempts == ()


def test_handler_without_a_solver_cannot_solve() -> None:
    handler = ChallengeHandler(solve_on_detection=True)
    assert handler.can_solve(recaptcha_observation()) is False
    with pytest.raises(ValueError, match="captcha_solver"):
        asyncio.run(handler.solve_page(FakePage()))


def test_check_solves_a_recaptcha_and_rescans() -> None:
    solver = FakeSolver()
    handler = ChallengeHandler(captcha_solver=solver, solve_on_detection=True)
    page = FakePage()

    observation = asyncio.run(handler.check(page))

    assert observation.kind is ChallengeKind.NONE
    assert len(solver.pages) == 1
    assert handler.last_attempt.ok is True
    assert handler.last_attempt.token == "token-abc"
    # The challenge is recorded even though it was cleared; note() only keeps
    # actual challenges, so the clean re-scan does not add an entry.
    assert [item.kind for item in handler.observations] == [ChallengeKind.RECAPTCHA]
    assert observation.kind is ChallengeKind.NONE


def test_a_surviving_challenge_raises_with_every_attempt() -> None:
    solver = FakeSolver(failures=5)
    handler = ChallengeHandler(
        captcha_solver=solver, solve_on_detection=True, max_solve_attempts=2
    )
    page = FakePage()

    with pytest.raises(CaptchaSolveError) as caught:
        asyncio.run(handler.check(page))

    message = str(caught.value)
    assert "Solver attempts" in message
    assert "RecaptchaTimeoutError: boom" in message
    assert len(handler.solve_attempts) == 2
    assert all(attempt.ok is False for attempt in handler.solve_attempts)


def test_a_surviving_challenge_is_returned_when_not_raising() -> None:
    solver = FakeSolver(failures=5)
    handler = ChallengeHandler(
        captcha_solver=solver,
        solve_on_detection=True,
        raise_on_detection=False,
    )

    observation = asyncio.run(handler.check(FakePage()))

    assert observation.kind is ChallengeKind.RECAPTCHA
    assert len(handler.solve_attempts) == 1


def test_unsolvable_kinds_keep_the_report_path() -> None:
    solver = FakeSolver()
    handler = ChallengeHandler(captcha_solver=solver, solve_on_detection=True)
    page = FakePage(scan=TURNSTILE_SCAN)

    with pytest.raises(ChallengeRequiredError) as caught:
        asyncio.run(handler.check(page))

    assert not isinstance(caught.value, CaptchaSolveError)
    assert solver.pages == []


def test_solve_page_returns_immediately_on_a_clean_page() -> None:
    solver = FakeSolver()
    handler = ChallengeHandler(captcha_solver=solver, solve_on_detection=True)

    observation = asyncio.run(handler.solve_page(FakePage(scan=CLEAN_SCAN)))

    assert observation.kind is ChallengeKind.NONE
    assert solver.pages == []


def test_explicit_attempts_override_the_handler_default() -> None:
    solver = FakeSolver(failures=10)
    handler = ChallengeHandler(
        captcha_solver=solver,
        solve_on_detection=True,
        max_solve_attempts=1,
        raise_on_detection=False,
    )

    asyncio.run(handler.solve_page(FakePage(), attempts=3))

    assert len(handler.solve_attempts) == 3


def test_explicit_attempts_run_even_after_the_budget_is_spent() -> None:
    """The per-page budget throttles the passive listener, not an explicit call."""
    solver = FakeSolver(failures=10)
    handler = ChallengeHandler(
        captcha_solver=solver,
        solve_on_detection=True,
        max_solve_attempts=1,
        raise_on_detection=False,
    )
    page = FakePage()

    asyncio.run(handler.solve_page(page))
    assert len(handler.solve_attempts) == 1

    # A further default call is throttled by the exhausted budget...
    asyncio.run(handler.solve_page(page))
    assert len(handler.solve_attempts) == 1

    # ...while an explicit attempt count always runs.
    asyncio.run(handler.solve_page(page, attempts=2))
    assert len(handler.solve_attempts) == 3


def test_installed_listener_solves_a_dom_challenge() -> None:
    solver = FakeSolver()
    handler = ChallengeHandler(
        captcha_solver=solver, solve_on_detection=True, raise_on_detection=False
    )
    page = FakePage()

    async def scenario() -> None:
        await handler.install(page)
        page.listeners["response"][0](
            FakeResponse(
                "https://example.test/login",
                200,
                {"content-type": "text/html"},
                "<html><body><div class='g-recaptcha'>grecaptcha</div></body></html>",
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert len(solver.pages) == 1
    assert handler.last_attempt.ok is True


def test_installed_listener_ignores_a_challenge_absent_from_the_dom() -> None:
    """A widget reference in a subresource body is not a challenge on the page."""
    solver = FakeSolver()
    handler = ChallengeHandler(
        captcha_solver=solver, solve_on_detection=True, raise_on_detection=False
    )
    page = FakePage(scan=CLEAN_SCAN)

    async def scenario() -> None:
        await handler.install(page)
        page.listeners["response"][0](
            FakeResponse(
                "https://example.test/widget.js",
                200,
                {"content-type": "text/html"},
                "<html><body>grecaptcha</body></html>",
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert solver.pages == []
    assert len(handler.observations) == 1


def test_auto_solve_is_capped_per_page() -> None:
    """The v3 backend reloads the page, which fires the listener again."""
    solver = FakeSolver(failures=10)
    handler = ChallengeHandler(
        captcha_solver=solver,
        solve_on_detection=True,
        raise_on_detection=False,
        max_solve_attempts=1,
    )
    page = FakePage()

    async def scenario() -> None:
        await handler.install(page)
        listener = page.listeners["response"][0]
        for _ in range(4):
            listener(
                FakeResponse(
                    "https://example.test/login",
                    200,
                    {"content-type": "text/html"},
                    "<html><body>grecaptcha</body></html>",
                )
            )
        await asyncio.sleep(0.1)

    asyncio.run(scenario())

    assert len(handler.solve_attempts) == 1


def test_clear_resets_solve_state() -> None:
    solver = FakeSolver()
    handler = ChallengeHandler(captcha_solver=solver, solve_on_detection=True)
    asyncio.run(handler.check(FakePage()))
    assert handler.solve_attempts

    handler.clear()

    assert handler.solve_attempts == ()
    assert handler.observations == ()
    assert handler.last_attempt is None
    assert handler._page_solve_budget == {}


# --------------------------------------------------------------------------- #
# BrowserEnvironment integration
# --------------------------------------------------------------------------- #


def test_no_challenge_handler_by_default() -> None:
    environment = BrowserEnvironment(BrowserEnvironmentOptions())
    assert environment.challenge_handler is None


def test_solve_challenges_without_a_solver_stays_observational() -> None:
    environment = BrowserEnvironment(BrowserEnvironmentOptions(solve_challenges=True))
    assert environment.challenge_handler is None


def test_challenge_handler_is_built_once_from_the_options(tmp_path) -> None:
    options = BrowserEnvironmentOptions(
        solve_challenges=True,
        captcha_solver=FakeSolver(),
        artifacts_dir=tmp_path,
    )
    environment = BrowserEnvironment(options)

    handler = environment.challenge_handler
    assert isinstance(handler, ChallengeHandler)
    assert handler.solve_on_detection is True
    assert handler.raise_on_detection is False
    assert handler.capture_dir == tmp_path / "challenges"
    assert environment.challenge_handler is handler


def test_a_supplied_handler_is_reused_and_gets_the_solver() -> None:
    solver = FakeSolver()
    supplied = ChallengeHandler(capture_dir=None, max_solve_attempts=3)
    options = BrowserEnvironmentOptions(
        solve_challenges=True, captcha_solver=solver, challenge_handler=supplied
    )
    environment = BrowserEnvironment(options)

    assert environment.challenge_handler is supplied
    assert supplied.captcha_solver is solver
    assert supplied.solve_on_detection is True
    assert supplied.max_solve_attempts == 3


def test_new_page_installs_the_solving_handler() -> None:
    options = BrowserEnvironmentOptions(
        solve_challenges=True, captcha_solver=FakeSolver()
    )
    environment = BrowserEnvironment(options)
    page = FakePage()
    context = FakeContext(page)

    created = asyncio.run(environment.new_page(context))

    assert created is page
    assert "response" in page.listeners
    assert page.init_scripts, "the determinism script is still installed"


def test_new_page_without_solving_installs_no_listener() -> None:
    environment = BrowserEnvironment(BrowserEnvironmentOptions())
    page = FakePage()

    asyncio.run(environment.new_page(FakeContext(page)))

    assert page.listeners == {}


def test_solve_challenge_requires_configuration() -> None:
    environment = BrowserEnvironment(BrowserEnvironmentOptions())
    with pytest.raises(ValueError, match="solve_challenges"):
        asyncio.run(environment.solve_challenge(FakePage()))


def test_solve_challenge_solves_and_returns_the_rescan() -> None:
    options = BrowserEnvironmentOptions(
        solve_challenges=True, captcha_solver=FakeSolver()
    )
    environment = BrowserEnvironment(options)

    observation = asyncio.run(environment.solve_challenge(FakePage()))

    assert observation.kind is ChallengeKind.NONE


def test_solve_challenge_raises_when_the_challenge_survives() -> None:
    options = BrowserEnvironmentOptions(
        solve_challenges=True, captcha_solver=FakeSolver(failures=3)
    )
    environment = BrowserEnvironment(options)

    with pytest.raises(CaptchaSolveError):
        asyncio.run(environment.solve_challenge(FakePage()))

    # ...and can be asked not to.
    environment.challenge_handler.clear()
    observation = asyncio.run(
        environment.solve_challenge(FakePage(), raise_on_failure=False)
    )
    assert observation.kind is ChallengeKind.RECAPTCHA


def test_with_captcha_solving_drops_the_synthetic_announcement() -> None:
    """Masking automation while announcing it would defeat both."""
    options = BrowserEnvironmentOptions.with_captcha_solving(FakeSolver())

    assert options.solve_challenges is True
    assert SYNTHETIC_MONITOR_HEADER not in options.context_kwargs().get(
        "extra_http_headers", {}
    )
    assert options.determinism is not None
    assert options.determinism.identify_automation is False
    assert "__arenaSynthetic" not in build_init_script(options.determinism)


def test_with_captcha_solving_can_keep_the_announcement() -> None:
    options = BrowserEnvironmentOptions.with_captcha_solving(
        FakeSolver(), identify_automation=True
    )

    headers = options.context_kwargs()["extra_http_headers"]
    assert headers[SYNTHETIC_MONITOR_HEADER].startswith("arena-api/")
    assert options.determinism.identify_automation is True


def test_with_captcha_solving_forwards_overrides(tmp_path) -> None:
    options = BrowserEnvironmentOptions.with_captcha_solving(
        FakeSolver(), locale="de-DE", artifacts_dir=tmp_path
    )

    assert options.locale == "de-DE"
    assert options.artifacts_dir == tmp_path


# --------------------------------------------------------------------------- #
# BrowserSessionManager integration
# --------------------------------------------------------------------------- #


class FakeEnvironment:
    """Stands in for BrowserEnvironment during a refresh."""

    instances: list["FakeEnvironment"] = []

    def __init__(self, options, page: FakePage) -> None:
        self.options = options
        self.page = page
        self.context = FakeContext(page)
        self.entered = False
        self.exited = False
        FakeEnvironment.instances.append(self)

    async def __aenter__(self) -> "FakeEnvironment":
        self.entered = True
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.exited = True

    async def new_context(self, **kwargs) -> FakeContext:
        return self.context

    async def new_page(self, context=None, **kwargs) -> FakePage:
        return self.page


@pytest.fixture
def patch_environment(monkeypatch):
    FakeEnvironment.instances.clear()
    from arena_api.auth import session as session_module

    page = FakePage()
    monkeypatch.setattr(
        session_module, "BrowserEnvironment", lambda options: FakeEnvironment(options, page)
    )
    return page


def make_manager(**kwargs) -> BrowserSessionManager:
    kwargs.setdefault("base_url", "https://example.test")
    return BrowserSessionManager(**kwargs)


def test_manager_wires_the_solver_into_its_handler() -> None:
    solver = FakeSolver()
    manager = make_manager(captcha_solver=solver)

    assert manager.challenge_handler is not None
    assert manager.challenge_handler.captcha_solver is solver
    assert manager.challenge_handler.solve_on_detection is True


def test_manager_keeps_an_explicit_handler_solver() -> None:
    own = FakeSolver()
    handler = ChallengeHandler(captcha_solver=own, solve_on_detection=False)
    manager = make_manager(captcha_solver=FakeSolver(), challenge_handler=handler)

    assert manager.challenge_handler is handler
    # The handler keeps the solver it was given...
    assert handler.captcha_solver is own
    # ...but configuring a solver on the manager means the manager solves.
    assert handler.solve_on_detection is True


def test_refresh_solves_a_challenge_and_persists_state(patch_environment, tmp_path) -> None:
    solver = FakeSolver()
    manager = make_manager(
        storage_path=tmp_path / "state.json", captcha_solver=solver
    )

    cookies = asyncio.run(manager.arefresh_session())

    assert cookies == {"session": "fresh"}
    assert len(solver.pages) == 1
    assert (tmp_path / "state.json").is_file()
    assert FakeEnvironment.instances[0].exited is True


def test_challenge_only_refresh_without_a_login_flow(patch_environment, tmp_path) -> None:
    """A blocked-but-valid session needs the screen cleared, not a new login."""
    solver = FakeSolver()
    manager = make_manager(
        storage_path=tmp_path / "state.json",
        captcha_solver=solver,
        captcha_url="https://example.test/verify",
    )

    cookies = asyncio.run(manager.arefresh_session())

    assert cookies == {"session": "fresh"}
    assert patch_environment.gotos == [
        {"url": "https://example.test/verify", "wait_until": "domcontentloaded"}
    ]


def test_challenge_only_refresh_defaults_to_the_base_url(patch_environment) -> None:
    manager = make_manager(captcha_solver=FakeSolver())

    asyncio.run(manager.arefresh_session())

    assert patch_environment.gotos[0]["url"] == "https://example.test"


def test_refresh_without_a_solver_still_reports_an_unsolvable_challenge(
    patch_environment, tmp_path
) -> None:
    patch_environment.scan = dict(TURNSTILE_SCAN)
    patch_environment.frames = []
    manager = make_manager(
        storage_path=tmp_path / "state.json", captcha_solver=FakeSolver()
    )

    with pytest.raises(ChallengeRequiredError) as caught:
        asyncio.run(manager.arefresh_session())

    assert not isinstance(caught.value, CaptchaSolveError)
    assert not (tmp_path / "state.json").exists()


def test_refresh_without_flow_or_solver_explains_itself(patch_environment) -> None:
    manager = make_manager()
    with pytest.raises(AuthenticationError) as caught:
        asyncio.run(manager.arefresh_session())
    assert "login_flow" in str(caught.value)
    assert FakeEnvironment.instances == [], "no browser is launched for a config error"


def test_ensure_valid_can_refresh_through_a_solver_alone() -> None:
    """A solver is a refresh path: the retry loop must not give up without one."""
    from arena_api.auth.session import RawResponse

    class Probe:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, url, *, cookies, headers, timeout) -> RawResponse:
            self.calls += 1
            status = 403 if self.calls == 1 else 200
            return RawResponse(status, {"content-type": "text/html"}, "Welcome back")

    probe = Probe()
    manager = make_manager(probe=probe, captcha_solver=FakeSolver(), max_refresh_attempts=1)
    manager.import_cookies({"session": "abc"})

    # The browser half is stubbed out; only the loop's decision matters here.
    async def fake_refresh(reason=None):
        manager.import_cookies({"session": "renewed"})
        return {"session": "renewed"}

    manager.arefresh_session = fake_refresh  # type: ignore[method-assign]

    assert asyncio.run(manager.aensure_valid()) == {"session": "renewed"}
    assert probe.calls == 2


# --------------------------------------------------------------------------- #
# Playwright API conformance (no browser binary needed)
# --------------------------------------------------------------------------- #


def _bind(function, *args, **kwargs):
    """Bind ``args``/``kwargs`` against the real Playwright signature."""
    return inspect.signature(function).bind(None, *args, **kwargs)


def test_challenge_only_refresh_goes_to_a_real_page_signature() -> None:
    """``arefresh_session`` navigates with kwargs the real API accepts."""
    from playwright.async_api import Page

    _bind(Page.goto, "https://example.test/login", wait_until="domcontentloaded")


def test_stealth_appliers_take_a_page_and_options() -> None:
    """The bridge calls the toolkit's stealth appliers as ``applier(page, options)``."""
    stealth_module = recaptcha_toolkit.stealth
    for name in ("apply_stealth_async", "apply_stealth_sync"):
        parameters = inspect.signature(getattr(stealth_module, name)).parameters
        assert list(parameters) == ["page_or_context", "options"], name


def test_recaptcha_solver_signatures_accept_an_external_page() -> None:
    """The facade trick relies on ``page`` being the first positional arg."""
    from recaptcha_toolkit import AsyncPlaywrightBot, PlaywrightBot

    for bot_class in (AsyncPlaywrightBot, PlaywrightBot):
        for name in ("solve_recaptcha_v2", "solve_recaptcha_v3"):
            parameters = inspect.signature(getattr(bot_class, name)).parameters
            assert list(parameters)[1] == "page", f"{bot_class.__name__}.{name}"

