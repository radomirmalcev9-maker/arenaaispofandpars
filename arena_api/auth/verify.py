"""Human-in-the-loop session capture for automated tests.

This is the *only* session-acquisition path in :mod:`arena_api.auth`, and its
contract is narrow: it opens a normal, unmodified browser at a page, waits for
a person to sign in and to finish whatever the site asks of them, then exports
the cookies that site set for that origin via Playwright's
``storage_state``.

What it never does
------------------
* It does not look for a challenge frame, compute a checkbox coordinate, move
  the mouse, click, type into, or otherwise interact with a CAPTCHA/Turnstile/
  DataDome widget — that is what a "solver" is, and it is not in this library.
* It does not inject script (no ``add_init_script``), patch
  ``navigator.webdriver``, fake ``chrome.runtime``, override WebGL/canvas/
  audio/fonts/permissions/screen metrics, or set/rotate a User-Agent.
* It does not run headless. A headless browser with no person in front of it is
  exactly the thing a challenge exists to stop, so "headless + waiting for a
  human" is a contradiction; if you need headless runs, exempt the environment
  by configuration (``docs/automated-tests-and-captcha.md``) instead.

The ``allowed_hosts`` guard is friction with a purpose: passing a host means
you are asserting you may test it (your own app, or a staging origin your team
administers). Default is localhost and other private-use hosts only.

Playwright is an optional dependency (``pip install 'arena-api[browser]'``
then ``playwright install chromium``); nothing in :mod:`arena_api.auth` needs
it unless you use this module.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .manager import CheckpointContext, CookieJar

__all__ = [
    "ALLOWED_LOCAL_HOSTS",
    "PlaywrightCheckpoint",
    "PlaywrightNotAvailable",
    "VerificationError",
    "VerificationResult",
    "capture_storage_state",
    "import_playwright",
    "is_allowed_test_host",
    "wait_for_cookie_names",
]

#: Hosts that never need an explicit ``allowed_hosts`` entry: local dev boxes.
ALLOWED_LOCAL_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "[::1]",
        "testserver",
        "host.docker.internal",
    }
)

#: Domain suffixes treated as private-use for test purposes.
ALLOWED_LOCAL_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".test",
    ".invalid",
    ".example",
    ".internal",
)

#: Optional extra allow-list for CI, comma-separated:
#: ``ARENA_API_TEST_ORIGINS=staging.myapp.dev,localhost:8000``.
ALLOWED_HOSTS_ENV_VAR = "ARENA_API_TEST_ORIGINS"

_VERIFICATION_PROMPT = (
    "Откройся в браузере, войди в аккаунт и пройди все проверки, которые "
    "запрашивает сайт.\n"
    "Когда будешь залогинен — нажми Enter здесь, чтобы сохранить сессию..."
)


class PlaywrightNotAvailable(RuntimeError):
    """Playwright (or its browser binaries) is not installed."""


class VerificationError(RuntimeError):
    """The manual verification could not be completed or captured."""


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def _environment_allowlist() -> tuple[str, ...]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV_VAR, "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _hostname(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.strip("[]").lower()


def is_allowed_test_host(
    host_or_url: str, allowed_hosts: Iterable[str] | None = None
) -> bool:
    """Return ``True`` for local/private-use hosts and explicit allow-list hits.

    Matching is by registrable-ish suffix, so an allowed ``myapp.dev`` also
    covers ``staging.myapp.dev`` — the same way a cookie domain does.
    """
    host = _hostname(host_or_url) if "://" in host_or_url else host_or_url.strip("[]").lower()
    if not host:
        return False
    bare = host.split(":")[0]
    if bare in ALLOWED_LOCAL_HOSTS or host in ALLOWED_LOCAL_HOSTS:
        return True
    if any(bare.endswith(suffix) for suffix in ALLOWED_LOCAL_SUFFIXES):
        return True
    if bare.startswith(("127.", "10.", "192.168.")):
        return True
    if bare.startswith("172."):
        parts = bare.split(".")
        try:
            return 16 <= int(parts[1]) <= 31
        except (IndexError, ValueError):
            return False
    if ":" in bare and ("::1" == bare or bare in {"::ffff:127.0.0.1"}):
        return True
    candidates = tuple(item.strip().lower() for item in (allowed_hosts or ()) if item.strip())
    for entry in (*candidates, *_environment_allowlist()):
        entry_host = _hostname(entry) if "://" in entry else entry.strip("[]").split("/")[0]
        entry_bare = entry_host.split(":")[0]
        if not entry_bare:
            continue
        if bare == entry_bare or bare.endswith(f".{entry_bare}"):
            return True
    return False


def _require_allowed_origin(
    url: str, allowed_hosts: Iterable[str] | None, *, action: str
) -> str:
    host = _hostname(url)
    if not host:
        raise VerificationError(f"cannot derive a host from {url!r}")
    if not is_allowed_test_host(url, allowed_hosts):
        raise VerificationError(
            f"refusing to {action} for {host!r}: not a local/private-use test host. "
            f"Pass allowed_hosts=({host!r}, ...) to assert you own or are authorized "
            f"to test this origin, or set {ALLOWED_HOSTS_ENV_VAR}."
        )
    return host


def import_playwright() -> Any:
    """Import ``playwright.sync_api`` with an actionable error message."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise PlaywrightNotAvailable(
            "Playwright is not installed. Enable this path with:\n"
            "    pip install 'arena-api[browser]'\n"
            "    python -m playwright install chromium"
        ) from exc
    return sync_playwright


# --------------------------------------------------------------------------- #
# Cookie polling
# --------------------------------------------------------------------------- #


def wait_for_cookie_names(
    context: Any,
    url: str,
    names: Sequence[str],
    *,
    timeout: float = 300.0,
    poll_interval: float = 0.5,
    require_all: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Poll a Playwright context until the *site* has set the named cookies.

    This is a readiness signal for a script, not an interaction: it never
    touches a widget, only reads ``context.cookies(url)``. Returns the matching
    cookie list (empty when ``timeout`` elapsed).
    """
    wanted = [str(name) for name in names]
    if not wanted:
        return {}
    deadline = time.monotonic() + timeout
    while True:
        try:
            cookies = list(context.cookies(url) or [])
        except Exception as exc:
            raise VerificationError(f"cannot read cookies from the context: {exc}") from exc
        present = {str(item.get("name")) for item in cookies}
        hits = [name for name in wanted if name in present]
        if (require_all and len(hits) == len(wanted)) or (not require_all and hits):
            return {name: next(c for c in cookies if str(c.get("name")) == name) for name in hits}
        if should_stop is not None and should_stop():
            return {}
        if time.monotonic() >= deadline:
            return {}
        time.sleep(max(0.01, poll_interval))


# --------------------------------------------------------------------------- #
# The capture itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of :func:`capture_storage_state`."""

    storage_state_path: Path
    origin: str
    cookie_names: tuple[str, ...] = ()
    cookies: tuple[Mapping[str, Any], ...] = ()
    waited_seconds: float = 0.0
    completed_by: str = "human"

    @property
    def cookie_count(self) -> int:
        return len(self.cookies)

    def jar(self) -> CookieJar:
        return CookieJar.from_storage_state({"cookies": list(self.cookies)})

    def as_dict(self) -> dict[str, Any]:
        return {
            "storage_state_path": str(self.storage_state_path),
            "origin": self.origin,
            "cookie_names": list(self.cookie_names),
            "cookie_count": self.cookie_count,
            "waited_seconds": round(self.waited_seconds, 3),
            "completed_by": self.completed_by,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<VerificationResult {self.origin} cookies={list(self.cookie_names)} "
            f"saved to {self.storage_state_path}>"
        )


@dataclass(slots=True)
class _CaptureState:
    browser: Any = None
    context: Any = None
    page: Any = None
    playwright: Any = None
    started: float = field(default=0.0)


def capture_storage_state(
    url: str,
    *,
    save_to: Path | str,
    allowed_hosts: Iterable[str] | None = None,
    wait_for_cookie: Sequence[str] | None = None,
    require_all_cookies: bool = False,
    timeout: float = 600.0,
    poll_interval: float = 0.5,
    prompt: str = _VERIFICATION_PROMPT,
    output: Any = None,
    input_fn: Callable[[str], str] = input,
    interactive: bool = True,
    resume_from: Path | str | None = None,
    close_browser: bool = True,
) -> VerificationResult:
    """Open ``url`` in a real browser, wait for a person, export the session.

    ``wait_for_cookie`` shortens the wait: as soon as the site has set one of
    those cookie names the capture finishes, so a CI job can call this while a
    human does the login in the window that appeared.

    Nothing about the browser is disguised on the way in and nothing about the
    challenge is touched while waiting: the person in front of the screen is
    the verification.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    origin_host = _require_allowed_origin(url, allowed_hosts, action="capture a session")
    target = Path(save_to).expanduser()
    sync_playwright = import_playwright()
    started = time.monotonic()
    stream = output if output is not None else sys.stdout
    state = _CaptureState(started=started)
    cookie_list: list[Mapping[str, Any]] = []
    resume_path = (
        Path(resume_from).expanduser()
        if resume_from is not None and str(resume_from).strip()
        else None
    )

    try:
        state.playwright = sync_playwright().start()
        # Deliberately: no --disable-blink-features, no --use-fake-*, no custom
        # user_agent, no init scripts. An honest browser pointed at your app.
        state.browser = state.playwright.chromium.launch(headless=False)
        context_options: dict[str, Any] = {}
        if resume_path is not None and resume_path.is_file():
            context_options["storage_state"] = str(resume_path)
        state.context = state.browser.new_context(**context_options)
        state.page = state.context.new_page()
        state.page.goto(url, wait_until="domcontentloaded")
        if prompt:
            print(prompt.format(url=url, host=origin_host), file=stream, flush=True)

        if wait_for_cookie:
            found = wait_for_cookie_names(
                state.context,
                url,
                wait_for_cookie,
                timeout=timeout,
                poll_interval=poll_interval,
                require_all=require_all_cookies,
            )
            cookie_list = list(found.values())
            if not found and interactive:
                _wait_for_enter(input_fn, stream)
        elif interactive:
            _wait_for_enter(input_fn, stream)

        if not cookie_list:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                state.context.storage_state(path=str(target))
                try:
                    # Playwright creates the file with the process umask; a
                    # session file is a credential, so narrow it explicitly.
                    os.chmod(target, 0o600)
                except OSError:
                    pass
            except TypeError:
                # Fake/mocked contexts in tests may not accept path=.
                payload = state.context.storage_state()
                _write_private_json(target, payload)
            written = _read_storage_state(target)
        else:
            payload = {
                "cookies": cookie_list,
                "origins": [],
                "arena_api": {"version": 1, "origin": origin_host},
            }
            _write_private_json(target, payload)
            written = payload
    except VerificationError:
        raise
    except PlaywrightNotAvailable:
        raise
    except Exception as exc:
        raise VerificationError(f"verification capture failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if close_browser:
            _quietly_close(state)

    cookies = [dict(item) for item in written.get("cookies", []) if isinstance(item, Mapping)]
    names = tuple(dict.fromkeys(str(item.get("name", "")) for item in cookies if item.get("name")))
    return VerificationResult(
        storage_state_path=target,
        origin=origin_host,
        cookie_names=names,
        cookies=tuple(cookies),
        waited_seconds=time.monotonic() - started,
        completed_by="human",
    )


def _wait_for_enter(input_fn: Callable[[str], str], stream: Any) -> None:
    """Block until the person signals they are done.

    Deliberately unbuffered and uninterruptible-by-timeout: the whole point of
    this checkpoint is that the wait ends when a human says it does. The
    ``timeout`` argument applies to the cookie-polling path, where a script can
    decide on its own that nothing is happening.
    """
    try:
        input_fn("")
    except (EOFError, KeyboardInterrupt):
        print("interrupted; capturing whatever the site has already set", file=stream, flush=True)
    except OSError as exc:
        raise VerificationError(
            f"no terminal to wait on ({exc}); pass wait_for_cookie= or input_fn="
        ) from exc


def _quietly_close(state: _CaptureState) -> None:
    for closer in (
        getattr(state.page, "close", None),
        getattr(state.context, "close", None),
        getattr(state.browser, "close", None),
        getattr(state.playwright, "stop", None),
    ):
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                continue


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_storage_state(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerificationError(f"storage state was not written to {path}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(f"storage state {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise VerificationError(f"storage state {path} must be a JSON object")
    return payload


# --------------------------------------------------------------------------- #
# Checkpoint adapter for AutoSessionManager
# --------------------------------------------------------------------------- #


class PlaywrightCheckpoint:
    """Wire :func:`capture_storage_state` into
    :class:`~arena_api.auth.manager.AutoSessionManager` as its ``checkpoint``.

    When the manager's probe reports that the origin wants a human, this
    opens the verification URL in a browser, waits for the person, and returns
    the cookies the site set — which the manager then re-adopts. It is a
    *waiter*, never a solver::

        manager = AutoSessionManager(
            PlaywrightStorageSource(Path("~/.cache/arena-api/state.json")),
            domains=("staging.myapp.dev",),
            checkpoint=PlaywrightCheckpoint(
                "https://staging.myapp.dev/login",
                storage_state=Path("~/.cache/arena-api/state.json"),
                allowed_hosts=("staging.myapp.dev",),
                wait_for_cookie=("sessionid",),
            ),
        )
    """

    __slots__ = (
        "allowed_hosts",
        "interactive",
        "poll_interval",
        "prompt",
        "require_all_cookies",
        "resume_from",
        "storage_state",
        "timeout",
        "url",
        "wait_for_cookie",
    )

    def __init__(
        self,
        url: str,
        *,
        storage_state: Path | str,
        allowed_hosts: Sequence[str] | None = None,
        wait_for_cookie: Sequence[str] | None = None,
        require_all_cookies: bool = False,
        timeout: float = 600.0,
        poll_interval: float = 0.5,
        interactive: bool = True,
        prompt: str = _VERIFICATION_PROMPT,
        resume_from: Path | str | None = None,
    ) -> None:
        self.url = url
        self.storage_state = Path(storage_state).expanduser()
        self.allowed_hosts = tuple(allowed_hosts or ())
        self.wait_for_cookie = tuple(wait_for_cookie or ())
        self.require_all_cookies = require_all_cookies
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.interactive = bool(interactive)
        self.prompt = prompt
        self.resume_from = Path(resume_from).expanduser() if resume_from else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PlaywrightCheckpoint {self.url} -> {self.storage_state}>"

    def __call__(self, context: CheckpointContext) -> CookieJar:
        _require_allowed_origin(
            self.url, self.allowed_hosts or context.domains, action="open a verification page"
        )
        result = capture_storage_state(
            self.url,
            save_to=self.storage_state,
            allowed_hosts=self.allowed_hosts,
            wait_for_cookie=self.wait_for_cookie,
            require_all_cookies=self.require_all_cookies,
            timeout=self.timeout,
            poll_interval=self.poll_interval,
            prompt=self.prompt,
            output=context.output,
            interactive=self.interactive and context.interactive,
            resume_from=self.resume_from,
        )
        return result.jar()
