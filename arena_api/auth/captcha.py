"""Opt-in bridge from ``arena_api.auth`` to :mod:`recaptcha_toolkit`.

Posture
-------
Everywhere else ``arena_api.auth`` is deliberately *observational*:
:mod:`arena_api.auth.challenge` detects a verification screen, captures
diagnostics and reports it, and :class:`~arena_api.auth.browser.BrowserEnvironment`
announces itself with ``X-Synthetic-Monitor`` instead of camouflaging the run.
This module is the single place where that stance is relaxed, and it is relaxed
only when you ask for it: nothing here is active unless you construct a solver
and hand it to a handler, an environment or a session manager.

Only :data:`~arena_api.auth.challenge.ChallengeKind.RECAPTCHA` is covered,
because that is what :mod:`recaptcha_toolkit` can solve. Turnstile, hCaptcha and
Cloudflare managed challenges keep the detect-and-report path -- see
:data:`~arena_api.auth.challenge.REMEDIATION_TEMPLATE` for the durable fixes.

No second browser
-----------------
The toolkit's bots are used as a *solver facade* over a page that this package
already opened. ``PlaywrightBot.solve_recaptcha_v2/v3`` accept an external
``page`` and never touch their own context, so the bot is constructed but never
entered: no extra Chromium, no extra ``sync_playwright()``. The one exception is
``SolverBackend.VISION_AI``, whose YOLO solver replicates the widget in a
browser of its own; it therefore needs ``website_key``/``website_url``, which
are read from the live page (and the page's cookies are handed over so the
solver's browser shares the session).

Stealth
-------
Solving a reCAPTCHA while announcing ``navigator.webdriver`` and a synthetic
monitor header is self-defeating, so the solver applies
``playwright-stealth`` to the page before handing it to the audio/image/v3
backend. Set ``apply_stealth=False`` to opt out -- and prefer
:meth:`~arena_api.auth.browser.BrowserEnvironmentOptions.with_captcha_solving`,
which also drops the synthetic-monitor announcement so the two postures do not
contradict each other.

Dependencies stay optional: :mod:`recaptcha_toolkit` is imported lazily inside
the methods that need it, so importing this module (and therefore
``arena_api``) requires nothing beyond the standard library.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..exceptions import CaptchaSolveError
from .challenge import (
    SOLVABLE_CHALLENGE_KINDS,
    ChallengeKind,
    ChallengeObservation,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from recaptcha_toolkit import (
        PlaywrightSolverOptions,
        RecaptchaVersion,
        SolverBackend,
        StealthOptions,
        VisionAIOptions,
    )

__all__ = [
    "SOLVABLE_CHALLENGE_KINDS",
    "TOOLKIT_PACKAGE",
    "CaptchaAttempt",
    "CaptchaSolver",
    "RecaptchaToolkitSolver",
    "detect_recaptcha_version",
    "detect_recaptcha_version_sync",
    "observation_targets_recaptcha",
]

#: Distribution that provides the solver backends.
TOOLKIT_PACKAGE = "recaptcha_toolkit"

#: Distinguish a visible v2 checkbox from an invisible/v3 widget.
#:
#: ``data-size="invisible"`` is the documented invisible flow; an ``anchor``
#: iframe with a non-empty box is the v2 checkbox; a zero-sized anchor iframe
#: or a bare ``grecaptcha`` object means the token is produced without a
#: visible challenge, which is what the v3 interception path expects.
_VERSION_DETECT_JS = """
() => {
  const nodes = Array.from(
    document.querySelectorAll(
      '.g-recaptcha, [data-sitekey], [data-recaptcha-widget-id], #recaptcha'
    )
  );
  for (const node of nodes) {
    const size = String(node.getAttribute('data-size') || '').toLowerCase();
    if (size === 'invisible') return 'v3';
  }
  const anchor = document.querySelector(
    "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/enterprise/anchor']"
  );
  if (anchor) {
    const box = anchor.getBoundingClientRect();
    return box.width > 1 && box.height > 1 ? 'v2' : 'v3';
  }
  if (typeof window.grecaptcha !== 'undefined') return 'v3';
  return 'v2';
}
"""


def _toolkit() -> Any:
    """Import :mod:`recaptcha_toolkit` lazily, with an actionable error."""
    try:
        import recaptcha_toolkit
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise CaptchaSolveError(
            f"Solving challenges requires the {TOOLKIT_PACKAGE} package; install "
            "it with `pip install '.[auth-captcha]'` (add `.[vision]` for the "
            "local YOLO backend)."
        ) from exc
    return recaptcha_toolkit


def observation_targets_recaptcha(observation: ChallengeObservation) -> bool:
    """Whether an observation carries any reCAPTCHA evidence.

    :func:`~arena_api.auth.challenge.inspect_response` and
    :func:`~arena_api.auth.challenge.inspect_page` classify by priority, so a
    page that embeds a reCAPTCHA *inside* a Cloudflare interstitial is
    reported as ``TURNSTILE``/``CLOUDFLARE_MANAGED`` while still holding a
    reCAPTCHA signal. This helper exposes that distinction; whether it is
    worth acting on is up to :meth:`RecaptchaToolkitSolver.supports`.
    """
    if observation.kind in SOLVABLE_CHALLENGE_KINDS:
        return True
    return any(signal.kind in SOLVABLE_CHALLENGE_KINDS for signal in observation.signals)


@dataclass(frozen=True)
class CaptchaAttempt:
    """Outcome of one solve attempt.

    A failure is *returned*, not raised, so that a caller can retry, count
    attempts and report every reason in one message. :meth:`raise_for_status`
    turns a failed attempt into :class:`~arena_api.exceptions.CaptchaSolveError`
    when you would rather have the exception.
    """

    token: str | None = None
    backend: str = ""
    version: str = ""
    site_key: str | None = None
    attempts: int = 1
    duration: float = 0.0
    error: str | None = None
    stealth_applied: bool = False
    observation: ChallengeObservation | None = None

    @property
    def ok(self) -> bool:
        """Whether a usable ``g-recaptcha-response`` token was produced."""
        return bool(self.token)

    def describe(self) -> str:
        """One-line summary suitable for a CI log."""
        label = f"{self.backend or 'solver'}/{self.version or '?'}"
        outcome = "solved" if self.ok else f"failed ({self.error})"
        return f"{label} in {self.duration:.1f}s: {outcome}"

    def raise_for_status(self) -> str:
        """Return the token, or raise :class:`CaptchaSolveError`."""
        if self.ok:
            return str(self.token)
        raise CaptchaSolveError(
            f"reCAPTCHA was not solved ({self.describe()}). "
            "Check the solver backend, its dependencies (FFmpeg for the audio "
            "backend, CAPSOLVER_API_KEY for the image backend) and that the "
            "page really hosts a reCAPTCHA widget."
        )


@runtime_checkable
class CaptchaSolver(Protocol):
    """Duck-typed solver accepted by :class:`~arena_api.auth.challenge.ChallengeHandler`.

    Implementations decide for themselves which observations they can act on;
    the handler consults :meth:`supports` before handing over a page.
    """

    def supports(self, observation: ChallengeObservation) -> bool:
        """Whether this solver can act on ``observation``."""
        ...

    async def asolve(
        self, page: Any, observation: ChallengeObservation | None = None
    ) -> CaptchaAttempt:
        """Solve the challenge on an already-open Playwright page (async)."""
        ...

    def solve(
        self, page: Any, observation: ChallengeObservation | None = None
    ) -> CaptchaAttempt:
        """Synchronous twin of :meth:`asolve`."""
        ...


@dataclass
class RecaptchaToolkitSolver:
    """Solve reCAPTCHA on an ``arena_api.auth`` page via :mod:`recaptcha_toolkit`.

    Parameters
    ----------
    backend:
        :class:`~recaptcha_toolkit.SolverBackend` (or its string value):
        ``"audio"`` (Google Speech, needs FFmpeg), ``"image"`` (CapSolver, needs
        ``CAPSOLVER_API_KEY``) or ``"vision_ai"`` (local YOLO, own browser).
    version:
        ``"v2"``/``"v3"`` to force a path; ``None`` (default) inspects the page
        and picks one -- a visible checkbox is v2, an invisible widget is
        handled by the v3 token interception.
    solver:
        :class:`~recaptcha_toolkit.PlaywrightSolverOptions` (attempts, timeouts,
        CapSolver key).
    vision_ai:
        :class:`~recaptcha_toolkit.VisionAIOptions` for the YOLO backend. Give
        concurrent solvers distinct ``server_port``/``download_dir`` values.
    stealth:
        :class:`~recaptcha_toolkit.StealthOptions` applied to the page before
        solving. ``None`` uses the toolkit defaults.
    apply_stealth:
        Set to ``False`` to solve without masking ``navigator.webdriver`` and
        friends (the audio backend then fails far more often).
    reload_for_v3:
        The v3 listener must be attached *before* the browser requests
        ``/recaptcha/api2/reload``; since the page is already loaded, it is
        reloaded after the listener is installed.
    solve_kinds:
        Challenge kinds this solver accepts. Defaults to reCAPTCHA only.
    allow_mixed_signals:
        Also accept observations whose *classification* is something else (e.g.
        a Cloudflare interstitial) but which contain a reCAPTCHA signal. Off by
        default: solving the widget will not clear a managed challenge.
    share_cookies_with_vision_ai:
        Hand the page's cookies to the YOLO solver's own browser so it works
        inside the same session.
    """

    backend: "SolverBackend | str" = "audio"
    version: "RecaptchaVersion | str | None" = None
    solver: "PlaywrightSolverOptions | None" = None
    vision_ai: "VisionAIOptions | None" = None
    stealth: "StealthOptions | None" = None
    apply_stealth: bool = True
    reload_for_v3: bool = True
    solve_kinds: tuple[ChallengeKind, ...] = tuple(SOLVABLE_CHALLENGE_KINDS)
    allow_mixed_signals: bool = False
    share_cookies_with_vision_ai: bool = True

    def __post_init__(self) -> None:
        if not self.solve_kinds:
            raise ValueError("solve_kinds must contain at least one ChallengeKind")
        unknown = [kind for kind in self.solve_kinds if not isinstance(kind, ChallengeKind)]
        if unknown:
            raise TypeError(f"solve_kinds must be ChallengeKind members, got {unknown!r}")

    # ------------------------------------------------------------------ #
    # Capability
    # ------------------------------------------------------------------ #

    def supports(self, observation: ChallengeObservation) -> bool:
        """Whether this solver will act on ``observation``."""
        if not observation.is_challenge:
            return False
        if observation.kind in self.solve_kinds:
            return True
        return self.allow_mixed_signals and observation_targets_recaptcha(observation)

    def describe(self) -> str:
        """Human-readable backend label for logs."""
        version = "auto" if self.version is None else str(getattr(self.version, "value", self.version))
        stealth = "stealth" if self.apply_stealth else "no-stealth"
        return f"recaptcha_toolkit[{self.backend}/{version}/{stealth}]"

    # ------------------------------------------------------------------ #
    # Async path
    # ------------------------------------------------------------------ #

    async def asolve(
        self, page: Any, observation: ChallengeObservation | None = None
    ) -> CaptchaAttempt:
        """Solve the reCAPTCHA on ``page`` and return the attempt.

        Failures are reported through :attr:`CaptchaAttempt.error` rather than
        raised, so a handler can retry and then explain every attempt.
        """
        toolkit = _toolkit()
        backend = toolkit.SolverBackend(self.backend)
        label = str(getattr(backend, "value", backend))
        started = time.perf_counter()
        version = await detect_recaptcha_version(page, toolkit=toolkit, override=self.version)
        stealth_applied = False
        try:
            stealth_applied = await self._apply_stealth_async(page, toolkit, backend)
            if backend is toolkit.SolverBackend.VISION_AI:
                token, site_key = await self._asolve_vision_ai(page, toolkit, version)
            else:
                token, site_key = await self._asolve_playwright(page, toolkit, backend, version)
        except Exception as exc:  # noqa: BLE001 - toolkit raises its own types
            return CaptchaAttempt(
                backend=label,
                version=str(getattr(version, "value", version)),
                duration=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                stealth_applied=stealth_applied,
                observation=observation,
            )
        return CaptchaAttempt(
            token=token,
            backend=label,
            version=str(getattr(version, "value", version)),
            site_key=site_key,
            duration=time.perf_counter() - started,
            stealth_applied=stealth_applied,
            observation=observation,
        )

    async def _apply_stealth_async(self, page: Any, toolkit: Any, backend: Any) -> bool:
        """Patch the page with ``playwright-stealth`` before solving."""
        if not self.apply_stealth or backend is toolkit.SolverBackend.VISION_AI:
            # The YOLO solver replicates the widget in its own browser, so
            # patching this page would change nothing; its user agent is
            # forwarded from the stealth options instead.
            return False
        from recaptcha_toolkit.stealth import apply_stealth_async

        await apply_stealth_async(page, self.stealth)
        return True

    def _async_bot(self, toolkit: Any, backend: Any) -> Any:
        """Build an ``AsyncPlaywrightBot`` **without** launching its browser.

        The bot is only a facade here: its ``solve_recaptcha_v2/v3`` accept an
        external page and never touch ``bot.context``, so entering the context
        manager (which would start a second Chromium) is both unnecessary and
        wasteful. Stealth is disabled at bot level because this class applies
        it to the page itself.
        """
        return toolkit.AsyncPlaywrightBot(
            backend=backend,
            stealth=toolkit.StealthOptions(enabled=False),
            solver=self.solver,
            vision_ai=self.vision_ai,
        )

    async def _asolve_playwright(
        self, page: Any, toolkit: Any, backend: Any, version: Any
    ) -> tuple[str, None]:
        bot = self._async_bot(toolkit, backend)
        if version is toolkit.RecaptchaVersion.V3:
            token = await bot.solve_recaptcha_v3(page, reload=self.reload_for_v3)
        else:
            token = await bot.solve_recaptcha_v2(page)
        return str(token), None

    async def _asolve_vision_ai(
        self, page: Any, toolkit: Any, version: Any
    ) -> tuple[str, str]:
        site_key = await toolkit.playwright_bot.detect_site_key_async(page)
        url = str(getattr(page, "url", "") or "")
        if not url:
            raise toolkit.SiteKeyDetectionError(
                "SolverBackend.VISION_AI needs the page URL; the supplied page "
                "has none."
            )
        cookies = await _page_cookies_async(page) if self.share_cookies_with_vision_ai else None
        result = await toolkit.VisionAISolver(self.vision_ai).solve_async(
            website_key=site_key,
            website_url=url,
            is_invisible=version is toolkit.RecaptchaVersion.V3,
            cookies=cookies,
            user_agent=(self.stealth.navigator_user_agent if self.stealth else None),
        )
        return str(result.token), site_key

    # ------------------------------------------------------------------ #
    # Sync path
    # ------------------------------------------------------------------ #

    def solve(
        self, page: Any, observation: ChallengeObservation | None = None
    ) -> CaptchaAttempt:
        """Synchronous twin of :meth:`asolve` for ``playwright.sync_api`` pages."""
        toolkit = _toolkit()
        backend = toolkit.SolverBackend(self.backend)
        label = str(getattr(backend, "value", backend))
        started = time.perf_counter()
        version = detect_recaptcha_version_sync(page, toolkit=toolkit, override=self.version)
        stealth_applied = False
        try:
            stealth_applied = self._apply_stealth_sync(page, toolkit, backend)
            if backend is toolkit.SolverBackend.VISION_AI:
                token, site_key = self._solve_vision_ai(page, toolkit, version)
            else:
                token, site_key = self._solve_playwright(page, toolkit, backend, version)
        except Exception as exc:  # noqa: BLE001 - toolkit raises its own types
            return CaptchaAttempt(
                backend=label,
                version=str(getattr(version, "value", version)),
                duration=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                stealth_applied=stealth_applied,
                observation=observation,
            )
        return CaptchaAttempt(
            token=token,
            backend=label,
            version=str(getattr(version, "value", version)),
            site_key=site_key,
            duration=time.perf_counter() - started,
            stealth_applied=stealth_applied,
            observation=observation,
        )

    def _apply_stealth_sync(self, page: Any, toolkit: Any, backend: Any) -> bool:
        if not self.apply_stealth or backend is toolkit.SolverBackend.VISION_AI:
            return False
        from recaptcha_toolkit.stealth import apply_stealth_sync

        apply_stealth_sync(page, self.stealth)
        return True

    def _sync_bot(self, toolkit: Any, backend: Any) -> Any:
        """Build a ``PlaywrightBot`` without launching its browser (see :meth:`_async_bot`)."""
        return toolkit.PlaywrightBot(
            backend=backend,
            stealth=toolkit.StealthOptions(enabled=False),
            solver=self.solver,
            vision_ai=self.vision_ai,
        )

    def _solve_playwright(
        self, page: Any, toolkit: Any, backend: Any, version: Any
    ) -> tuple[str, None]:
        bot = self._sync_bot(toolkit, backend)
        if version is toolkit.RecaptchaVersion.V3:
            token = bot.solve_recaptcha_v3(page, reload=self.reload_for_v3)
        else:
            token = bot.solve_recaptcha_v2(page)
        return str(token), None

    def _solve_vision_ai(self, page: Any, toolkit: Any, version: Any) -> tuple[str, str]:
        site_key = toolkit.playwright_bot.detect_site_key(page)
        url = str(getattr(page, "url", "") or "")
        if not url:
            raise toolkit.SiteKeyDetectionError(
                "SolverBackend.VISION_AI needs the page URL; the supplied page has none."
            )
        cookies = _page_cookies_sync(page) if self.share_cookies_with_vision_ai else None
        result = toolkit.VisionAISolver(self.vision_ai).solve_detailed(
            website_key=site_key,
            website_url=url,
            is_invisible=version is toolkit.RecaptchaVersion.V3,
            cookies=cookies,
            user_agent=(self.stealth.navigator_user_agent if self.stealth else None),
        )
        return str(result.token), site_key


# --------------------------------------------------------------------------- #
# Version detection
# --------------------------------------------------------------------------- #


def _normalize_version(raw: Any, toolkit: Any, override: Any) -> Any:
    if override is not None:
        return toolkit.RecaptchaVersion(override)
    text = str(raw or "").strip().lower()
    if text in {"v3", "invisible"}:
        return toolkit.RecaptchaVersion.V3
    return toolkit.RecaptchaVersion.V2


async def detect_recaptcha_version(page: Any, *, toolkit: Any = None, override: Any = None) -> Any:
    """Guess whether ``page`` hosts a v2 checkbox or an invisible/v3 widget.

    The heuristic is intentionally conservative: anything it cannot classify is
    treated as v2, because the v2 solver waits for a visible widget and reports
    a clear :class:`~recaptcha_toolkit.RecaptchaNotFoundError` instead of
    silently intercepting nothing.
    """
    toolkit = toolkit or _toolkit()
    if override is not None:
        return _normalize_version(None, toolkit, override)
    try:
        raw = await page.evaluate(_VERSION_DETECT_JS)
    except Exception:  # noqa: BLE001 - closed page, CSP, non-JS context
        raw = None
    return _normalize_version(raw, toolkit, None)


def detect_recaptcha_version_sync(page: Any, *, toolkit: Any = None, override: Any = None) -> Any:
    """Synchronous twin of :func:`detect_recaptcha_version`."""
    toolkit = toolkit or _toolkit()
    if override is not None:
        return _normalize_version(None, toolkit, override)
    try:
        raw = page.evaluate(_VERSION_DETECT_JS)
    except Exception:  # noqa: BLE001
        raw = None
    return _normalize_version(raw, toolkit, None)


async def _page_cookies_async(page: Any) -> list[dict[str, Any]] | None:
    """Read the page context's cookies for the YOLO solver's own browser."""
    context = getattr(page, "context", None)
    if context is None:
        return None
    try:
        cookies = await context.cookies()
    except Exception:  # noqa: BLE001 - context may already be closed
        return None
    return [dict(cookie) for cookie in cookies or []] or None


def _page_cookies_sync(page: Any) -> list[dict[str, Any]] | None:
    context = getattr(page, "context", None)
    if context is None:
        return None
    try:
        cookies = context.cookies()
    except Exception:  # noqa: BLE001
        return None
    return [dict(cookie) for cookie in cookies or []] or None
