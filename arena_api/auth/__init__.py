"""Session extraction and browser-session helpers for the Arena clients.

Two backends live here:

* :mod:`arena_api.auth.firefox_session` reuses the signed-in session of your
  local Firefox profile (stdlib-only, ``rookiepy`` optional).
* :mod:`arena_api.auth.session` drives a real Playwright sign-in with your own
  credentials and persists the result as ``cookies.json`` /
  ``storage_state``, with :mod:`arena_api.auth.browser`,
  :mod:`arena_api.auth.environment` and :mod:`arena_api.auth.kinematics`
  providing the Playwright wrapper, determinism init scripts and pointer
  kinematics respectively.

:mod:`arena_api.auth.challenge` detects browser verification challenges,
captures diagnostics, and turns them into an actionable
:class:`~arena_api.exceptions.ChallengeRequiredError` with remediation
guidance -- which is what a synthetic monitor actually needs.

:mod:`arena_api.auth.captcha` is the opt-in bridge to
:mod:`recaptcha_toolkit`: hand a ``RecaptchaToolkitSolver`` to a
:class:`~arena_api.auth.challenge.ChallengeHandler`, a
:class:`~arena_api.auth.browser.BrowserEnvironmentOptions` or a
:class:`~arena_api.auth.session.BrowserSessionManager` and a detected reCAPTCHA
is solved instead of only reported. Nothing is active until you do.

Heavy dependencies (``playwright``, ``curl_cffi``, ``numpy``,
``recaptcha_toolkit``) are imported lazily inside the functions that need them,
so importing this package stays cheap and works without them installed.
"""

from .browser import (
    SYNTHETIC_MONITOR_HEADER,
    BrowserEnvironment,
    BrowserEnvironmentOptions,
    run_sync,
)
from .captcha import (
    TOOLKIT_PACKAGE,
    CaptchaAttempt,
    CaptchaSolver,
    RecaptchaToolkitSolver,
    detect_recaptcha_version,
    detect_recaptcha_version_sync,
    observation_targets_recaptcha,
)
from .challenge import (
    CLOUDFLARE_FRAME_HOSTS,
    HCAPTCHA_SELECTORS,
    RECAPTCHA_SELECTORS,
    REMEDIATION_TEMPLATE,
    SOLVABLE_CHALLENGE_KINDS,
    STATUS_TEXT_MARKERS,
    TURNSTILE_SELECTORS,
    ChallengeHandler,
    ChallengeKind,
    ChallengeObservation,
    ChallengeSignal,
    inspect_page,
    inspect_response,
)
from .environment import (
    ANIMATION_FREEZE_CSS,
    DEFAULT_INIT_SCRIPT,
    DeterminismOptions,
    build_init_script,
)
from .firefox_session import (
    DEFAULT_COOKIE_DOMAINS,
    FirefoxSessionError,
    FirefoxSessionManager,
    SessionManager,
)
from .kinematics import (
    KinematicMouse,
    KinematicMouseSync,
    MouseSample,
    Point,
    Trajectory,
    TrajectoryPlanner,
    cubic_bezier_point,
    fitts_duration,
    resolve_element_center,
)
from .session import (
    BrowserSessionManager,
    CurlCffiProbe,
    FormLoginFlow,
    LoginFlow,
    RawResponse,
    SessionProbe,
    SessionValidation,
    chromium_user_agent,
    normalize_state_payload,
)

__all__ = [
    "ANIMATION_FREEZE_CSS",
    "BrowserEnvironment",
    "BrowserEnvironmentOptions",
    "BrowserSessionManager",
    "CLOUDFLARE_FRAME_HOSTS",
    "CaptchaAttempt",
    "CaptchaSolver",
    "ChallengeHandler",
    "ChallengeKind",
    "ChallengeObservation",
    "ChallengeSignal",
    "CurlCffiProbe",
    "DEFAULT_COOKIE_DOMAINS",
    "DEFAULT_INIT_SCRIPT",
    "DeterminismOptions",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "FormLoginFlow",
    "HCAPTCHA_SELECTORS",
    "KinematicMouse",
    "KinematicMouseSync",
    "LoginFlow",
    "MouseSample",
    "Point",
    "RECAPTCHA_SELECTORS",
    "REMEDIATION_TEMPLATE",
    "RawResponse",
    "RecaptchaToolkitSolver",
    "SOLVABLE_CHALLENGE_KINDS",
    "STATUS_TEXT_MARKERS",
    "TOOLKIT_PACKAGE",
    "SYNTHETIC_MONITOR_HEADER",
    "SessionManager",
    "SessionProbe",
    "SessionValidation",
    "TURNSTILE_SELECTORS",
    "Trajectory",
    "TrajectoryPlanner",
    "build_init_script",
    "chromium_user_agent",
    "cubic_bezier_point",
    "detect_recaptcha_version",
    "detect_recaptcha_version_sync",
    "fitts_duration",
    "inspect_page",
    "inspect_response",
    "normalize_state_payload",
    "observation_targets_recaptcha",
    "resolve_element_center",
    "run_sync",
]
