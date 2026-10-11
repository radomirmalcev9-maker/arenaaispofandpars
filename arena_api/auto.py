"""Zero-configuration session bootstrap: sign in once, the rest is automatic.

The one manual step
-------------------
You sign in to your account in your own browser. Nothing else is required: no
cookie export, no headers, no callback indices, no refresh loop.

What happens automatically
--------------------------
1. **Find the session.** Sources are tried in order, first hit wins:
   ``cookies=`` (mapping / header / ``cookies.json``), ``ARENA_COOKIE`` /
   ``ARENA_COOKIES_FILE``, a stored ``cookies.json`` (yours, or written by an
   earlier run that had ``storage_path=`` -- the bootstrap never writes cookies
   to disk on its own), then your browser profile -- Firefox natively
   (``rookiepy`` or the built-in lock-safe ``cookies.sqlite`` reader) and other
   browsers through ``rookiepy`` when it is installed.
2. **Match the fingerprint.** Request headers come from the same source as the
   cookies, so the client presents the browser the session actually came from.
3. **Verify.** One probe request classifies the session: accepted, rejected,
   challenged, rate-limited -- or "reachable, not an auth problem", which does
   not block startup.
4. **Heal, cheapest path first.** When a request later fails with 401/403 or a
   challenge, the refresh ladder runs: re-read the browser profile (in
   interactive mode it asks you to refresh the page and press Enter), then a
   real form login if credentials are configured, then -- only if
   ``solve_captchas`` allows it and the dependencies are installed -- solve a
   reCAPTCHA in a browser.
5. **Retry transparently.** The transport applies the healed cookies and
   replays the original request, streams included.

Nothing here obtains credentials on its own and nothing is solved silently:
challenge solving needs ``solve_captchas`` (default ``"auto"`` -- on when the
dependencies are importable) and a form login needs credentials *you* put in
the environment. Use it only with accounts and deployments you are entitled to
automate.

Command line::

    python -m arena_api --status             # what was found, is it accepted
    python -m arena_api --explain            # every source considered and why
    python -m arena_api --models             # model list of the frontend
    python -m arena_api --prompt "Hi"        # one completion

(``python -m arena_api.auto`` works too; ``python -m arena_api`` is the
warning-free spelling, see :mod:`arena_api.__main__`.)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal, TextIO
from urllib.parse import urlparse

from .auth.browser import BrowserEnvironment, BrowserEnvironmentOptions, run_sync
from .auth.captcha import RecaptchaToolkitSolver
from .auth.challenge import ChallengeHandler
from .auth.firefox_session import (
    DEFAULT_COOKIE_DOMAINS,
    FirefoxSessionError,
    FirefoxSessionManager,
    SessionManager,
)
from .auth.session import BrowserSessionManager, FormLoginFlow, LoginFlow
from .client import ArenaClient, AsyncArenaClient
from .constants import CONFIG_PATH, DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from .direct import DIRECT_PAGE_PATH, AsyncDirectArenaClient, DirectArenaClient
from .exceptions import (
    ArenaError,
    AuthenticationError,
    ChallengeRequiredError,
    RateLimitError,
)
from .transport import CookieInput, normalize_cookies

__all__ = [
    "AutoOptions",
    "AutoSessionManager",
    "DirectVerificationToken",
    "DiscoveredSession",
    "ProbeResult",
    "aprobe",
    "auto_async_client",
    "auto_async_direct_client",
    "auto_client",
    "auto_direct_client",
    "discover_session",
    "explain_login",
    "explain_sources",
    "login_flow_from_env",
    "main",
    "probe",
    "resolve_solver",
    "solver_dependencies_available",
]

#: Printed when no session was found anywhere and the user must sign in.
LOGIN_PROMPT_TEMPLATE = (
    "No session found for {site}. Open it in your browser, sign in to your "
    "account, then press Enter to continue..."
)

#: Browsers ``rookiepy`` can read, in the order they are tried after Firefox.
ROOKIEPY_BROWSERS: tuple[str, ...] = (
    "chrome",
    "chromium",
    "edge",
    "brave",
    "vivaldi",
    "opera",
)

#: Environment variables that turn a stored login into an automatic one.
LOGIN_URL_ENV = "ARENA_LOGIN_URL"
USERNAME_ENV = "ARENA_USERNAME"
PASSWORD_ENV = "ARENA_PASSWORD"
USER_SELECTOR_ENV = "ARENA_USER_SELECTOR"
PASS_SELECTOR_ENV = "ARENA_PASS_SELECTOR"
SUBMIT_SELECTOR_ENV = "ARENA_SUBMIT_SELECTOR"
SUCCESS_URL_ENV = "ARENA_SUCCESS_URL"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiscoveredSession:
    """A session found by :func:`discover_session`.

    :param source: short label -- ``"cookies"``, ``"env"``, ``"storage"``,
        ``"firefox"`` or ``"browser:<name>"``.
    :param cookies: flat ``{name: value}`` mapping.
    :param headers: browser-consistent headers from the same source.
    :param manager: the source's own manager, used as the cheap first rung of
        the refresh ladder (``None`` for plain cookie exports).
    :param description: human-readable one-liner for logs.
    """

    source: str
    cookies: dict[str, str]
    headers: dict[str, str]
    manager: SessionManager | None = None
    description: str = ""


#: Captcha/challenge markup that must never appear in an API response. Only
#: checked when the body is HTML: a JSON payload cannot be mistaken for one, so
#: a model or endpoint merely *named* "recaptcha" stays a healthy session.
_INTERSTITIAL_MARKERS: tuple[str, ...] = (
    "g-recaptcha",
    "/recaptcha/api2/anchor",
    "/recaptcha/enterprise/anchor",
    "/recaptcha/api.js",
    "hcaptcha",
    "cf-turnstile",
    "challenge-platform",
    "just a moment",
    "captcha-delivery",
)


def _interstitial_marker(text: str) -> str | None:
    """Name the challenge markup in an HTML body, or ``None`` for a real payload."""
    head = text.lstrip()[:512].lower()
    if not head.startswith(("<!doctype html", "<html")):
        return None
    lowered = text.lower()
    for marker in _INTERSTITIAL_MARKERS:
        if marker in lowered:
            return marker
    return None


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of the one verification request made at bootstrap."""

    ok: bool
    reason: str
    auth_problem: bool = False
    error: ArenaError | None = None

    def describe(self) -> str:
        """One-line summary for logs."""
        return f"{'ok' if self.ok else 'not ok'}: {self.reason}"


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass
class AutoOptions:
    """Every knob of the bootstrap, with defaults that need no configuration."""

    base_url: str = DEFAULT_BASE_URL

    #: Explicit cookie export; wins over every discovered source.
    cookies: CookieInput = None

    #: ``cookies.json`` written by an earlier run; also where a browser login
    #: persists its result. Defaults to ``./state/cookies.json`` when present.
    storage_path: str | Path | None = None

    #: Cookie domains; defaults to the ``base_url`` host plus the Arena ones.
    domains: tuple[str, ...] | None = None

    headers: Mapping[str, str] | None = None

    #: Read cookies from browser profiles. ``False`` limits discovery to
    #: explicit exports and the stored file.
    use_browser_profiles: bool = True

    #: Firefox profile pass-throughs (portable installs, a non-default profile,
    #: an explicit ``cookies.sqlite``). ``None`` keeps auto-discovery.
    profiles_root: str | Path | None = None
    profile_name: str | None = None
    cookie_database: str | Path | None = None

    #: Ask for Enter when the session must be refreshed by hand.
    interactive: bool = True
    prompt: Callable[[str], str] = input
    output: TextIO | None = None

    #: Real form login used when the profile re-read is not enough. ``None``
    #: builds one from ``ARENA_*`` environment variables when they are complete.
    login_flow: LoginFlow | None = None
    login_from_env: bool = True

    #: ``True``/``False`` force challenge solving on or off; ``"auto"`` (the
    #: default) enables it when the solver dependencies are importable.
    solve_captchas: bool | Literal["auto"] = "auto"
    captcha_backend: str = "audio"
    captcha_solver: Any | None = None

    #: Path probed to verify the session.
    probe_path: str = CONFIG_PATH
    verify: bool = True

    max_auth_refreshes: int = 2
    timeout: float = DEFAULT_TIMEOUT
    http2: bool = True
    verify_tls: bool | str = True
    http_transport: Any = None

    #: Where browser artefacts (challenge captures, traces) are written.
    artifacts_dir: str | Path | None = "./artifacts"

    def __post_init__(self) -> None:
        if self.storage_path is not None:
            self.storage_path = Path(self.storage_path)
        if self.artifacts_dir is not None:
            self.artifacts_dir = Path(self.artifacts_dir)
        if self.solve_captchas not in (True, False, "auto"):
            raise ValueError("solve_captchas must be True, False or 'auto'")

    @property
    def site(self) -> str:
        """Host of ``base_url``, used in user-facing messages."""
        return urlparse(self.base_url).hostname or self.base_url

    def cookie_domains(self) -> tuple[str, ...]:
        """Domains to read from browser profiles."""
        if self.domains:
            return tuple(self.domains)
        host = self.site
        extras = tuple(domain for domain in DEFAULT_COOKIE_DOMAINS if domain != host)
        return (host, *extras) if host else DEFAULT_COOKIE_DOMAINS


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _emit(options: AutoOptions, message: str) -> None:
    stream = options.output if options.output is not None else sys.stderr
    print(message, file=stream, flush=True)


def _domains_for(options: AutoOptions) -> list[str]:
    return list(options.cookie_domains())


def _from_explicit_cookies(options: AutoOptions) -> DiscoveredSession | None:
    if options.cookies is None:
        return None
    try:
        cookies = normalize_cookies(options.cookies)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise AuthenticationError(f"cookies= could not be read: {exc}") from exc
    if not cookies:
        return None
    return DiscoveredSession(
        source="cookies",
        cookies=cookies,
        headers={},
        description=f"explicit cookies= ({len(cookies)} names)",
    )


def _from_environment(options: AutoOptions) -> DiscoveredSession | None:
    header = os.environ.get("ARENA_COOKIE")
    path = os.environ.get("ARENA_COOKIES_FILE")
    if not header and not path:
        return None
    try:
        cookies = normalize_cookies(Path(path) if path else header)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise AuthenticationError(
            f"ARENA_COOKIE{'S_FILE' if path else ''} could not be read: {exc}"
        ) from exc
    if not cookies:
        return None
    return DiscoveredSession(
        source="env",
        cookies=cookies,
        headers={},
        description="ARENA_COOKIES_FILE" if path else "ARENA_COOKIE",
    )


def _default_storage_path(options: AutoOptions) -> Path | None:
    if options.storage_path is not None:
        return Path(options.storage_path)
    candidate = Path("state") / "cookies.json"
    return candidate if candidate.is_file() else None


def _from_storage(options: AutoOptions) -> DiscoveredSession | None:
    path = _default_storage_path(options)
    if path is None or not path.is_file():
        return None
    manager = BrowserSessionManager(
        base_url=options.base_url,
        domains=options.cookie_domains(),
        storage_path=path,
    )
    cookies = manager.load_cookies()
    if not cookies:
        # Not a Playwright ``storage_state`` file: a plain cookie export (a
        # ``{name: value}`` mapping, a ``Cookie:`` header, an extension's
        # record list) is just as good a source.
        try:
            cookies = normalize_cookies(path)
        except (ValueError, OSError) as exc:
            raise AuthenticationError(f"{path} could not be read: {exc}") from exc
        if cookies:
            manager.import_cookies(cookies)
    if not cookies:
        return None
    return DiscoveredSession(
        source="storage",
        cookies=cookies,
        headers=manager.headers(),
        # Re-reading the same file would return the very cookies that were just
        # rejected, so the ladder (login flow / solver) is the refresh path here.
        manager=None,
        description=f"{path} ({len(cookies)} names)",
    )


def _from_firefox(options: AutoOptions) -> DiscoveredSession | None:
    manager = FirefoxSessionManager(
        domains=_domains_for(options),
        profiles_root=options.profiles_root,
        profile_name=options.profile_name,
        cookie_database=options.cookie_database,
        interactive=options.interactive,
        prompt=options.prompt,
        output=options.output,
    )
    try:
        cookies = manager.load_cookies()
    except FirefoxSessionError:
        return None
    if not cookies:
        return None
    return DiscoveredSession(
        source="firefox",
        cookies=cookies,
        headers=manager.firefox_headers(),
        manager=manager,
        description=f"Firefox profile {manager.profile_path()} ({len(cookies)} names)",
    )


def _rookiepy_module() -> Any | None:
    try:
        import rookiepy  # type: ignore[import-untyped]
    except ImportError:
        return None
    return rookiepy


def _from_other_browsers(options: AutoOptions) -> DiscoveredSession | None:
    """Read a Chromium-family profile through ``rookiepy``, if installed.

    Firefox is handled by :func:`_from_firefox`, which also works without
    ``rookiepy``; this covers the browsers the optional dependency adds.
    """
    rookiepy = _rookiepy_module()
    if rookiepy is None:
        return None
    domains = _domains_for(options)
    for name in ROOKIEPY_BROWSERS:
        reader = getattr(rookiepy, name, None)
        if reader is None:
            continue
        try:
            records = reader(domains=domains)
        except Exception:  # noqa: BLE001 - locked store, missing profile, ...
            continue
        cookies = _flat_records(records)
        if cookies:
            return DiscoveredSession(
                source=f"browser:{name}",
                cookies=cookies,
                headers={},
                description=f"{name} profile via rookiepy ({len(cookies)} names)",
            )
    return None


def _flat_records(records: Any) -> dict[str, str]:
    """Flatten a rookiepy record list into ``{name: value}``."""
    from .auth.firefox_session import cookies_from_rookiepy

    try:
        return cookies_from_rookiepy(records)
    except Exception:  # noqa: BLE001 - unexpected record shape
        return {}


def discover_session(options: AutoOptions | None = None) -> DiscoveredSession | None:
    """Return the first usable session, or ``None`` when there is none.

    Order: explicit ``cookies=``, ``ARENA_COOKIE``/``ARENA_COOKIES_FILE``, a
    stored ``cookies.json``, the Firefox profile, then other browsers through
    ``rookiepy``.
    """
    options = options or AutoOptions()
    candidates: list[Callable[[AutoOptions], DiscoveredSession | None]] = [
        _from_explicit_cookies,
        _from_environment,
        _from_storage,
    ]
    if options.use_browser_profiles:
        candidates.append(_from_firefox)
        candidates.append(_from_other_browsers)
    for candidate in candidates:
        found = candidate(options)
        if found is not None:
            return found
    return None


def explain_sources(options: AutoOptions | None = None) -> list[str]:
    """Describe every source considered, for ``--explain`` and error messages."""
    options = options or AutoOptions()
    report: list[str] = []

    report.append(
        "cookies=          : "
        + ("set" if options.cookies is not None else "not given")
    )
    report.append(
        "ARENA_COOKIE(S_FILE): "
        + (
            "set"
            if os.environ.get("ARENA_COOKIE") or os.environ.get("ARENA_COOKIES_FILE")
            else "not set"
        )
    )
    path = _default_storage_path(options)
    report.append(
        f"stored cookies.json : {path} "
        + ("(exists)" if path is not None and path.is_file() else "(absent)")
    )
    if not options.use_browser_profiles:
        report.append("browser profiles   : disabled by use_browser_profiles=False")
        return report

    manager = FirefoxSessionManager(
        domains=_domains_for(options),
        profiles_root=options.profiles_root,
        profile_name=options.profile_name,
        cookie_database=options.cookie_database,
    )
    try:
        profile = manager.profile_path()
    except FirefoxSessionError as exc:
        report.append(f"Firefox profile     : unavailable ({exc})")
    else:
        try:
            found = len(manager.load_cookies())
        except FirefoxSessionError as exc:
            report.append(f"Firefox profile     : {profile} unreadable ({exc})")
        else:
            report.append(
                f"Firefox profile     : {profile} ({found} cookie names for "
                f"{', '.join(_domains_for(options))})"
            )
    if _rookiepy_module() is None:
        report.append(
            "other browsers      : rookiepy not installed "
            "(pip install '.[firefox]' to add Chrome/Edge/Brave/...)"
        )
    else:
        report.append("other browsers      : rookiepy available")
    return report


def _wait_for_login(options: AutoOptions) -> bool:
    """Ask the user to sign in, then look for a session again."""
    if not options.interactive:
        return False
    message = LOGIN_PROMPT_TEMPLATE.format(site=options.site)
    try:
        # ``input`` prints the message itself; a custom prompt receives it.
        options.prompt(message)
    except (EOFError, KeyboardInterrupt):
        _emit(options, "[arena-api] login wait cancelled")
        return False
    return discover_session(options) is not None


# --------------------------------------------------------------------------- #
# Refresh ladder
# --------------------------------------------------------------------------- #


def _has_spec(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def solver_dependencies_available(backend: str = "audio") -> bool:
    """Whether the solver backend's dependencies can be imported.

    Checked with :func:`importlib.util.find_spec` so that deciding costs
    nothing and never imports ``torch``/``playwright`` as a side effect.
    """
    if not _has_spec("playwright"):
        return False
    if str(backend) == "vision_ai":
        return _has_spec("vision_ai_recaptcha_solver")
    return _has_spec("playwright_recaptcha") and _has_spec("playwright_stealth")


def resolve_solver(options: AutoOptions) -> Any | None:
    """Build the captcha solver the options ask for, or ``None``."""
    if options.captcha_solver is not None:
        return options.captcha_solver
    if options.solve_captchas is False:
        return None
    if options.solve_captchas == "auto" and not solver_dependencies_available(
        options.captcha_backend
    ):
        return None
    return RecaptchaToolkitSolver(backend=options.captcha_backend)


def login_flow_from_env(options: AutoOptions) -> LoginFlow | None:
    """Build a :class:`FormLoginFlow` from ``ARENA_*`` variables, if complete.

    Credentials alone are not enough: the form's selectors are
    deployment-specific, so a partial configuration is ignored (and reported by
    :func:`explain_login`) rather than guessed at.
    """
    if options.login_flow is not None:
        return options.login_flow
    if not options.login_from_env:
        return None
    required = {
        "credentials": (USERNAME_ENV, PASSWORD_ENV),
        "form": (LOGIN_URL_ENV, USER_SELECTOR_ENV, PASS_SELECTOR_ENV, SUBMIT_SELECTOR_ENV),
    }
    for names in required.values():
        if not all(os.environ.get(name) for name in names):
            return None
    return FormLoginFlow(
        login_url=os.environ[LOGIN_URL_ENV],
        username_selector=os.environ[USER_SELECTOR_ENV],
        password_selector=os.environ[PASS_SELECTOR_ENV],
        submit_selector=os.environ[SUBMIT_SELECTOR_ENV],
        success_url_contains=os.environ.get(SUCCESS_URL_ENV),
        username_env=USERNAME_ENV,
        password_env=PASSWORD_ENV,
    )


def explain_login(options: AutoOptions | None = None) -> str:
    """Why the automatic form login is or is not available."""
    options = options or AutoOptions()
    if options.login_flow is not None:
        return "login flow: configured explicitly"
    if not options.login_from_env:
        return "login flow: disabled by login_from_env=False"
    credentials = [name for name in (USERNAME_ENV, PASSWORD_ENV) if not os.environ.get(name)]
    form = [
        name
        for name in (LOGIN_URL_ENV, USER_SELECTOR_ENV, PASS_SELECTOR_ENV, SUBMIT_SELECTOR_ENV)
        if not os.environ.get(name)
    ]
    if not credentials and not form:
        return "login flow: available from the environment"
    missing = ", ".join(credentials + form)
    return f"login flow: unavailable, missing {missing}"


@dataclass
class AutoSessionManager(BrowserSessionManager):
    """Session manager that refreshes through the cheapest path first.

    The ladder is: re-read the source the cookies came from (for a browser
    profile this is the interactive "refresh the page, press Enter" step, and it
    needs no browser of our own), then a real form login, then -- only when a
    solver is configured -- clearing a challenge in a browser. Each rung is
    tried only if the previous one returned nothing.
    """

    #: Manager of the source the cookies were discovered from.
    fallback: SessionManager | None = None
    _from_fallback: bool = field(default=True, repr=False, compare=False)

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        """Headers of the browser that owns the current cookies.

        A session read from Firefox must be presented as Firefox; once a
        browser login has replaced it, the Chromium headers of that run are the
        consistent ones.
        """
        if self._from_fallback and self.fallback is not None:
            try:
                return self.fallback.firefox_headers(navigation=navigation)
            except Exception:  # noqa: BLE001 - a source without headers
                pass
        return super().firefox_headers(navigation=navigation)

    def adopt(self, cookies: Mapping[str, str]) -> None:
        """Replace the stored session with freshly read cookies."""
        self.import_cookies(dict(cookies))
        self._from_fallback = True

    def refresh_session(self, reason: BaseException | None = None) -> dict[str, str] | None:
        """Run the refresh ladder and return fresh cookies, or ``None``."""
        if self.fallback is not None:
            try:
                cookies = self.fallback.refresh_session(reason)
            except Exception as exc:  # noqa: BLE001 - source-specific failures
                cookies = None
                _emit_note(self, f"source refresh failed: {type(exc).__name__}: {exc}")
            if cookies:
                self.adopt(cookies)
                return cookies
        if self.login_flow is None and self.captcha_solver is None:
            raise AuthenticationError(
                "the session was rejected and no automatic refresh path is left: "
                "sign in again in your browser, or configure one of "
                f"{USERNAME_ENV}/{PASSWORD_ENV} with form selectors "
                f"(see {__name__}.explain_login), or enable challenge solving "
                "(solve_captchas=True, pip install '.[auth-captcha]')"
            )
        self._from_fallback = False
        return super().refresh_session(reason)


def _emit_note(manager: AutoSessionManager, message: str) -> None:
    """Best-effort progress note; the manager has no output stream of its own."""
    print(f"[arena-api] {message}", file=sys.stderr, flush=True)


def build_session_manager(
    options: AutoOptions, discovered: DiscoveredSession
) -> AutoSessionManager:
    """Wire a discovered session into the manager the clients consume."""
    solver = resolve_solver(options)
    capture_dir = (
        Path(options.artifacts_dir) / "challenges" if options.artifacts_dir else None
    )
    manager = AutoSessionManager(
        base_url=options.base_url,
        domains=options.cookie_domains(),
        storage_path=(
            Path(options.storage_path)
            if options.storage_path is not None and discovered.source == "storage"
            else None
        ),
        challenge_handler=ChallengeHandler(
            capture_dir=capture_dir, raise_on_detection=False
        ),
        environment_options=BrowserEnvironmentOptions(
            headless=True, artifacts_dir=options.artifacts_dir
        ),
        login_flow=login_flow_from_env(options),
        captcha_solver=solver,
        captcha_url=options.base_url,
        timeout=options.timeout,
        fallback=discovered.manager,
    )
    if discovered.source != "storage":
        manager.adopt(discovered.cookies)
    else:
        if not manager.load_cookies():
            # The file is a plain cookie export rather than a Playwright
            # storage_state, which only the discovery path knows how to read.
            manager.adopt(discovered.cookies)
        manager._from_fallback = False
    return manager


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def _classify(exc: ArenaError) -> ProbeResult:
    if isinstance(exc, ChallengeRequiredError):
        return ProbeResult(False, "browser verification challenge", True, exc)
    if isinstance(exc, AuthenticationError):
        return ProbeResult(False, "session rejected or expired", True, exc)
    if isinstance(exc, RateLimitError):
        return ProbeResult(False, "rate limited", False, exc)
    # Anything else (redirect, non-JSON body, network hiccup) is not a session
    # problem: the frontend may simply not expose the probe path.
    return ProbeResult(True, f"reachable, probe inconclusive ({type(exc).__name__})", False, exc)


def _accepted(path: str, text: str) -> ProbeResult:
    """Classify a response the transport itself did not reject."""
    marker = _interstitial_marker(text)
    if marker is not None:
        return ProbeResult(
            False,
            f"{path} served a challenge page instead of data ({marker})",
            True,
        )
    return ProbeResult(True, "session accepted")


def probe(client: ArenaClient | DirectArenaClient, path: str = CONFIG_PATH) -> ProbeResult:
    """Verify a sync client's session with one request.

    The transport already retries with refreshed cookies, so reaching the
    classifier means every attempt failed; an HTML challenge page served with
    HTTP 200 is caught here because no status code betrays it.
    """
    try:
        text = client.transport.get_text(path)
    except ArenaError as exc:
        return _classify(exc)
    return _accepted(path, text)


async def aprobe(client: Any, path: str = CONFIG_PATH) -> ProbeResult:
    """Verify an async client's session with one request."""
    try:
        text = await client.transport.get_text(path)
    except ArenaError as exc:
        return _classify(exc)
    return _accepted(path, text)


def _heal_sync(
    client: ArenaClient,
    manager: AutoSessionManager,
    result: ProbeResult,
    options: AutoOptions,
) -> ProbeResult:
    """Run the refresh ladder once when the probe found an auth problem."""
    _emit(options, f"[arena-api] session needs a refresh ({result.reason})")
    try:
        cookies = manager.refresh_session(result.error)
    except AuthenticationError:
        client.close()
        raise
    if not cookies:
        _emit(options, "[arena-api] refresh produced no cookies; continuing as-is")
        return result
    client.transport.apply_session_cookies(cookies)
    return probe(client, options.probe_path)


async def _heal_async(
    client: AsyncArenaClient,
    manager: AutoSessionManager,
    result: ProbeResult,
    options: AutoOptions,
) -> ProbeResult:
    """Async twin of :func:`_heal_sync`; the ladder runs off the event loop."""
    _emit(options, f"[arena-api] session needs a refresh ({result.reason})")
    try:
        cookies = await asyncio.to_thread(manager.refresh_session, result.error)
    except AuthenticationError:
        await client.aclose()
        raise
    if not cookies:
        _emit(options, "[arena-api] refresh produced no cookies; continuing as-is")
        return result
    client.transport.apply_session_cookies(cookies)
    return await aprobe(client, options.probe_path)


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def _prepare(
    options: AutoOptions, *, require_session: bool = True
) -> tuple[AutoSessionManager, DiscoveredSession] | None:
    """Discover (or wait for) a session and build its manager."""
    discovered = discover_session(options)
    if discovered is None and require_session:
        if not _wait_for_login(options):
            raise AuthenticationError(
                "no session found for "
                f"{options.site}. Sign in to your account in a browser and run "
                "again, or pass cookies=/set ARENA_COOKIE.\n  "
                + "\n  ".join(explain_sources(options))
            )
        discovered = discover_session(options)
    if discovered is None:
        return None
    _emit(options, f"[arena-api] session: {discovered.description}")
    return build_session_manager(options, discovered), discovered


def auto_client(options: AutoOptions | None = None, **overrides: Any) -> ArenaClient:
    """Return a verified :class:`~arena_api.ArenaClient` for your own session.

    ``**overrides`` are :class:`AutoOptions` fields, so the common case is
    ``auto_client()`` and the tuned case is
    ``auto_client(base_url="https://lmarena.ai", solve_captchas=False)``.
    """
    options = _merge_options(options, overrides)
    prepared = _prepare(options)
    if prepared is None:  # pragma: no cover - require_session is always True here
        raise AuthenticationError("no session found")
    manager, _ = prepared
    client = ArenaClient(
        base_url=options.base_url,
        session_source=manager,
        headers=options.headers,
        timeout=options.timeout,
        http2=options.http2,
        verify=options.verify_tls,
        http_transport=options.http_transport,
        max_auth_refreshes=options.max_auth_refreshes,
    )
    if options.verify:
        result = probe(client, options.probe_path)
        if result.auth_problem:
            result = _heal_sync(client, manager, result, options)
        _emit(options, f"[arena-api] verification: {result.describe()}")
    return client


async def auto_async_client(
    options: AutoOptions | None = None, **overrides: Any
) -> AsyncArenaClient:
    """Async twin of :func:`auto_client`."""
    options = _merge_options(options, overrides)
    prepared = _prepare(options)
    if prepared is None:  # pragma: no cover
        raise AuthenticationError("no session found")
    manager, _ = prepared
    client = AsyncArenaClient(
        base_url=options.base_url,
        session_source=manager,
        headers=options.headers,
        timeout=options.timeout,
        http2=options.http2,
        verify=options.verify_tls,
        http_transport=options.http_transport,
        max_auth_refreshes=options.max_auth_refreshes,
    )
    if options.verify:
        result = await aprobe(client, options.probe_path)
        if result.auth_problem:
            result = await _heal_async(client, manager, result, options)
        _emit(options, f"[arena-api] verification: {result.describe()}")
    return client


def _merge_options(options: AutoOptions | None, overrides: Mapping[str, Any]) -> AutoOptions:
    if not overrides:
        return options or AutoOptions()
    if options is None:
        return AutoOptions(**dict(overrides))
    return replace(options, **dict(overrides))


class DirectVerificationToken:
    """Callable producing a fresh reCAPTCHA v3 assertion for Direct Chat.

    Strictly opt-in. :mod:`arena_api.direct` never obtains a verification token
    by itself; passing this object as ``verification_token=`` is the caller's
    explicit instruction to do it for them. It opens the Direct Chat page in a
    normalised browser and intercepts the v3 token with
    :class:`~arena_api.auth.captcha.RecaptchaToolkitSolver`, so it needs the
    captcha dependencies and a deployment you may automate.
    """

    def __init__(
        self,
        options: AutoOptions | None = None,
        *,
        page_url: str | None = None,
        solver: Any | None = None,
    ) -> None:
        self.options = options or AutoOptions()
        self.page_url = page_url or (
            self.options.base_url.rstrip("/") + DIRECT_PAGE_PATH
        )
        base = solver or resolve_solver(self.options) or RecaptchaToolkitSolver(
            backend=self.options.captcha_backend
        )
        # The Direct Chat assertion is always a v3 token. A custom solver object
        # decides its own path, so only the toolkit adapter is pinned.
        self.solver = (
            replace(base, version="v3", reload_for_v3=True)
            if isinstance(base, RecaptchaToolkitSolver)
            else base
        )

    async def afetch(self) -> str:
        """Fetch a token asynchronously."""
        if isinstance(self.solver, RecaptchaToolkitSolver) and not (
            solver_dependencies_available(self.solver.backend)
        ):
            raise AuthenticationError(
                "verification_token='auto' intercepts a reCAPTCHA v3 assertion, "
                "which needs the solver dependencies: pip install "
                "'.[auth-captcha]' and playwright install chromium "
                f"(backend '{self.solver.backend}' reports them missing)."
            )
        options = BrowserEnvironmentOptions.with_captcha_solving(
            self.solver, headless=True, artifacts_dir=self.options.artifacts_dir
        )
        async with BrowserEnvironment(options) as environment:
            page = await environment.new_page()
            await page.goto(self.page_url, wait_until="domcontentloaded")
            attempt = await self.solver.asolve(page)
            return attempt.raise_for_status()

    def __call__(self) -> str:
        """Fetch a token, safe to call from inside a running event loop."""
        return run_sync(self.afetch())


def auto_direct_client(
    options: AutoOptions | None = None,
    *,
    verification_token: Any = None,
    **overrides: Any,
) -> DirectArenaClient:
    """Return a :class:`~arena_api.DirectArenaClient` for your own session.

    ``verification_token`` accepts everything
    :class:`~arena_api.direct.DirectArenaClient` accepts, plus the string
    ``"auto"``: a :class:`DirectVerificationToken` is then built to intercept a
    v3 assertion with the captcha solver.
    """
    options = _merge_options(options, overrides)
    prepared = _prepare(options)
    if prepared is None:  # pragma: no cover
        raise AuthenticationError("no session found")
    manager, _ = prepared
    if isinstance(verification_token, str) and verification_token.strip().lower() == "auto":
        verification_token = DirectVerificationToken(options)
    return DirectArenaClient(
        base_url=options.base_url,
        session_source=manager,
        headers=options.headers,
        timeout=options.timeout,
        http2=options.http2,
        verify=options.verify_tls,
        verification_token=verification_token,
        http_transport=options.http_transport,
        max_auth_refreshes=options.max_auth_refreshes,
    )


async def auto_async_direct_client(
    options: AutoOptions | None = None,
    *,
    verification_token: Any = None,
    **overrides: Any,
) -> AsyncDirectArenaClient:
    """Async twin of :func:`auto_direct_client`."""
    options = _merge_options(options, overrides)
    prepared = _prepare(options)
    if prepared is None:  # pragma: no cover
        raise AuthenticationError("no session found")
    manager, _ = prepared
    if isinstance(verification_token, str) and verification_token.strip().lower() == "auto":
        verification_token = DirectVerificationToken(options)
    return AsyncDirectArenaClient(
        base_url=options.base_url,
        session_source=manager,
        headers=options.headers,
        timeout=options.timeout,
        http2=options.http2,
        verify=options.verify_tls,
        verification_token=verification_token,
        http_transport=options.http_transport,
        max_auth_refreshes=options.max_auth_refreshes,
    )


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI parser; ``--help`` documents every flag."""
    parser = argparse.ArgumentParser(
        prog="python -m arena_api",
        description=(
            "Bootstrap an Arena client from your own browser session. The only "
            "manual step is signing in to your account."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="frontend to talk to")
    parser.add_argument("--cookies", default=None, help="cookie header or cookies.json path")
    parser.add_argument("--storage", default=None, help="cookies.json written by an earlier run")
    parser.add_argument(
        "--no-browser-profiles",
        action="store_true",
        help="do not read cookies from local browser profiles",
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="never wait for Enter"
    )
    parser.add_argument(
        "--solve",
        choices=("auto", "on", "off"),
        default="auto",
        help="challenge solving: auto = on when dependencies are importable",
    )
    parser.add_argument(
        "--backend", default="audio", help="captcha backend: audio, image or vision_ai"
    )
    parser.add_argument("--probe-path", default=CONFIG_PATH, help="path used to verify")
    parser.add_argument("--no-verify", action="store_true", help="skip the probe request")
    parser.add_argument("--status", action="store_true", help="report the session and stop")
    parser.add_argument(
        "--explain", action="store_true", help="list every source considered and why"
    )
    parser.add_argument("--models", action="store_true", help="print the model list")
    parser.add_argument("--prompt", default=None, help="send one prompt and print the answer")
    return parser


def _options_from_args(args: argparse.Namespace) -> AutoOptions:
    solve: bool | Literal["auto"] = (
        "auto" if args.solve == "auto" else args.solve == "on"
    )
    return AutoOptions(
        base_url=args.base_url,
        cookies=args.cookies,
        storage_path=args.storage,
        use_browser_profiles=not args.no_browser_profiles,
        interactive=not args.non_interactive,
        solve_captchas=solve,
        captcha_backend=args.backend,
        probe_path=args.probe_path,
        verify=not args.no_verify,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m arena_api`` (and ``python -m arena_api.auto``).

    Exit codes: ``0`` everything worked, ``1`` no usable session (or another
    library error), ``2`` a session was found but the server rejected it.
    """
    args = build_arg_parser().parse_args(argv)
    options = _options_from_args(args)

    if args.explain:
        for line in explain_sources(options):
            print(line)
        print(explain_login(options))
        available = solver_dependencies_available(options.captcha_backend)
        print(
            f"captcha solving   : {options.solve_captchas} "
            f"(backend {options.captcha_backend}, dependencies "
            f"{'available' if available else 'missing'})"
        )
        return 0

    try:
        return _run(args, options)
    except ArenaError as exc:
        print(f"[arena-api] {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, options: AutoOptions) -> int:
    """The part of :func:`main` that needs a session."""
    if args.status and not (args.models or args.prompt):
        discovered = discover_session(options)
        if discovered is None:
            print("session           : none found")
            for line in explain_sources(options):
                print(f"  {line}")
            return 1
        print(f"session           : {discovered.description}")
        if not options.verify:
            return 0
        with auto_client(options) as client:
            result = probe(client, options.probe_path)
        print(f"verification      : {result.describe()}")
        return 0 if result.ok else 2

    with auto_client(options) as client:
        if args.models:
            models = client.get_models()
            print(f"models            : {len(models)}")
            for model in models[:25]:
                print(f"  {model.id} — {model.display_name}")
        if args.prompt:
            print("answer            : ", end="", flush=True)
            for chunk in client.stream(args.prompt):
                sys.stdout.write(chunk.delta)
                sys.stdout.flush()
            print()
        if not (args.models or args.prompt):
            print("session           : ready")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
