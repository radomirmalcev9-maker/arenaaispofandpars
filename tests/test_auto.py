"""Tests for the zero-configuration bootstrap layer (:mod:`arena_api.auto`).

The point of ``arena_api.auto`` is that the only manual step is signing in to an
account in a real browser, so these tests cover: where a session may come from
and in which order, what the probe concludes from each response shape, how a
rejected session is healed at startup, the refresh ladder, and what the
``python -m arena_api.auto`` CLI reports. Nothing here touches the network or
launches a browser.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from arena_api import ArenaClient, AsyncArenaClient
from arena_api.auth.captcha import CaptchaAttempt, RecaptchaToolkitSolver
from arena_api.auth.session import BrowserSessionManager, FormLoginFlow
from arena_api.auto import (
    AutoOptions,
    AutoSessionManager,
    DirectVerificationToken,
    DiscoveredSession,
    ProbeResult,
    _flat_records,
    _merge_options,
    _options_from_args,
    aprobe,
    auto_async_client,
    auto_client,
    auto_direct_client,
    build_arg_parser,
    build_session_manager,
    discover_session,
    explain_login,
    explain_sources,
    login_flow_from_env,
    main,
    probe,
    resolve_solver,
    solver_dependencies_available,
)
from arena_api.constants import CONFIG_PATH, DEFAULT_BASE_URL
from arena_api.exceptions import AuthenticationError
from arena_api.models import ModelInfo

import arena_api.auto as auto_module

FUTURE = int(time.time()) + 86_400
ENV_VARS = (
    "ARENA_COOKIE",
    "ARENA_COOKIES_FILE",
    "ARENA_LOGIN_URL",
    "ARENA_USERNAME",
    "ARENA_PASSWORD",
    "ARENA_USER_SELECTOR",
    "ARENA_PASS_SELECTOR",
    "ARENA_SUBMIT_SELECTOR",
    "ARENA_SUCCESS_URL",
)
CHALLENGE_HTML = (
    b'<!doctype html><html><head><title>Just a moment...</title></head>'
    b'<body><div id="cf-chl-widget"></div></body></html>'
)
#: Served with HTTP 200 and no marker the transport knows: only the probe's own
#: HTML inspection catches this one.
RECAPTCHA_PAGE = (
    b'<!doctype html><html><head>'
    b'<script src="https://www.google.com/recaptcha/api.js"></script></head>'
    b'<body><div class="g-recaptcha" data-sitekey="6Lc"></div></body></html>'
)
CONFIG_JSON = json.dumps(
    {"dependencies": [{"fn_index": 4, "api_name": "chat", "queue": True}], "components": []}
).encode()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep host-level ``ARENA_*`` configuration out of every test."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class Recorder:
    """Mock transport that replays canned responses and records requests."""

    def __init__(self, *responses: tuple):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            status, body, content_type = self.responses.pop(0)
        else:
            status, body, content_type = 200, b"{}", "application/json"
        return httpx.Response(
            status,
            request=request,
            headers={"content-type": content_type},
            content=body,
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def cookies(self, index: int = 0) -> str:
        return self.requests[index].headers.get("cookie", "")


def make_firefox_profile(tmp_path: Path, value: str = "from-firefox") -> Path:
    """Build a throwaway Firefox profile tree holding one arena.ai cookie."""
    root = tmp_path / "firefox"
    profile = root / "abcd1234.default-release"
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / "cookies.sqlite")
    connection.executescript(
        """
        CREATE TABLE moz_cookies (
            id INTEGER PRIMARY KEY,
            originAttributes TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            host TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT '/',
            expiry INTEGER NOT NULL,
            lastAccessed INTEGER NOT NULL,
            creationTime INTEGER NOT NULL,
            isSecure INTEGER NOT NULL,
            isHttpOnly INTEGER NOT NULL,
            inBrowserElement INTEGER NOT NULL DEFAULT 0,
            sameSite INTEGER NOT NULL DEFAULT 0,
            rawSameSite INTEGER NOT NULL DEFAULT 0,
            schemeMap INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.execute(
        "INSERT INTO moz_cookies"
        " (name, value, host, path, expiry, lastAccessed, creationTime, isSecure, isHttpOnly)"
        " VALUES (?, ?, ?, '/', ?, 0, 0, 1, 0)",
        ("session", value, ".arena.ai", FUTURE),
    )
    connection.commit()
    connection.close()
    (profile / "compatibility.ini").write_text(
        "[Compatibility]\nLastVersion=156.0_20250101000000\n", encoding="utf-8"
    )
    (root / "profiles.ini").write_text(
        "[Install1234ABCD]\nDefault=abcd1234.default-release\n", encoding="utf-8"
    )
    return root


def isolated_options(tmp_path: Path, **kwargs) -> AutoOptions:
    """Options that cannot pick anything up from the working directory."""
    kwargs.setdefault("storage_path", tmp_path / "unused" / "cookies.json")
    kwargs.setdefault("use_browser_profiles", False)
    kwargs.setdefault("solve_captchas", False)
    kwargs.setdefault("artifacts_dir", tmp_path / "artifacts")
    kwargs.setdefault("output", io.StringIO())
    return AutoOptions(**kwargs)


def write_storage(tmp_path: Path, cookies: dict[str, str]) -> Path:
    path = tmp_path / "state" / "cookies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies), encoding="utf-8")
    return path


class StubSource:
    """A browser source standing in for the ladder's cheap first rung."""

    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0"

    def __init__(self, cookies: dict[str, str] | None = None):
        self.cookies = cookies
        self.refresh_reasons: list = []

    def load_cookies(self, domains=None):
        return dict(self.cookies or {})

    def firefox_headers(self, *, navigation: bool = False):
        return {
            "User-Agent": self.USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        }

    def refresh_session(self, reason=None):
        self.refresh_reasons.append(reason)
        return dict(self.cookies) if self.cookies else None


class TokenSolver:
    """Custom solver stand-in handing back a fixed token."""

    backend = "stub"

    def __init__(self, token: str = "solved-token"):
        self.token = token
        self.calls: list = []

    def supports(self, kind, observation=None) -> bool:
        return True

    async def asolve(self, page, observation=None) -> CaptchaAttempt:
        self.calls.append(page.url)
        return CaptchaAttempt(token=self.token, backend=self.backend)

    def solve(self, page, observation=None) -> CaptchaAttempt:
        return asyncio.run(self.asolve(page, observation))


class Chunk:
    def __init__(self, delta: str):
        self.delta = delta


class DummyClient:
    """Stand-in for the CLI, so no test needs a reachable service."""

    def __init__(self, models=(), answer="echo"):
        self._models = list(models)
        self.answer = answer
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def get_models(self, **kwargs):
        return self._models

    def stream(self, prompt, **kwargs):
        yield Chunk(f"{self.answer}: {prompt}")


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


def test_defaults_need_no_configuration() -> None:
    options = AutoOptions()
    assert options.base_url == DEFAULT_BASE_URL
    assert options.probe_path == CONFIG_PATH
    assert options.use_browser_profiles is True
    assert options.interactive is True
    assert options.verify is True
    assert options.solve_captchas == "auto"
    assert options.login_from_env is True
    assert options.prompt is input


def test_cookie_domains_cover_the_configured_host() -> None:
    assert AutoOptions(base_url="https://example.test:8443").cookie_domains()[0] == "example.test"
    assert "lmarena.ai" in AutoOptions().cookie_domains()
    assert AutoOptions(domains=("only.test",)).cookie_domains() == ("only.test",)


def test_solve_captchas_accepts_only_three_values() -> None:
    with pytest.raises(ValueError, match="solve_captchas"):
        AutoOptions(solve_captchas="maybe")


def test_merge_options_replaces_fields_of_an_existing_object(tmp_path) -> None:
    options = isolated_options(tmp_path, cookies="session=a")
    merged = _merge_options(options, {"verify": False, "captcha_backend": "image"})
    assert merged is not options
    assert merged.cookies == "session=a"
    assert merged.verify is False
    assert merged.captcha_backend == "image"
    assert _merge_options(None, {}) == AutoOptions()


# --------------------------------------------------------------------------- #
# Discovery: sources and precedence
# --------------------------------------------------------------------------- #


def test_explicit_cookies_win_over_every_other_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARENA_COOKIE", "session=from-env")
    write_storage(tmp_path, {"session": "from-storage"})
    options = AutoOptions(
        cookies={"session": "explicit"},
        storage_path=tmp_path / "state" / "cookies.json",
        profiles_root=make_firefox_profile(tmp_path),
        solve_captchas=False,
        output=io.StringIO(),
    )
    found = discover_session(options)
    assert found is not None
    assert found.source == "cookies"
    assert found.cookies == {"session": "explicit"}


def test_cookie_header_export_is_parsed(tmp_path) -> None:
    found = discover_session(isolated_options(tmp_path, cookies="b=2; a=1; session=S"))
    assert found.cookies == {"session": "S", "a": "1", "b": "2"}
    assert found.source == "cookies"


def test_cookie_file_export_is_read(tmp_path, monkeypatch) -> None:
    export = tmp_path / "export.json"
    export.write_text(json.dumps([{"name": "session", "value": "file"}]), encoding="utf-8")
    monkeypatch.setenv("ARENA_COOKIES_FILE", str(export))
    found = discover_session(isolated_options(tmp_path))
    assert found.source == "env"
    assert found.cookies == {"session": "file"}


def test_unreadable_cookie_export_is_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARENA_COOKIE", str(tmp_path / "missing.json"))
    with pytest.raises(AuthenticationError, match="could not be read"):
        discover_session(isolated_options(tmp_path))


def test_stored_state_file_is_reused(tmp_path) -> None:
    path = write_storage(tmp_path, {"session": "stored"})
    options = AutoOptions(storage_path=path, solve_captchas=False, output=io.StringIO())
    found = discover_session(options)
    assert found.source == "storage"
    assert found.cookies == {"session": "stored"}
    # Re-reading the same file would return the rejected cookies, so there is no
    # cheap rung: the ladder (login flow / solver) is the refresh path.
    assert found.manager is None


def test_firefox_profile_is_discovered_without_configuration(tmp_path) -> None:
    profiles = make_firefox_profile(tmp_path)
    options = isolated_options(tmp_path, use_browser_profiles=True, profiles_root=profiles)
    found = discover_session(options)
    assert found.source == "firefox"
    assert found.cookies == {"session": "from-firefox"}
    assert "default-release" in found.description


def test_browser_profile_discovery_can_be_turned_off(tmp_path) -> None:
    options = isolated_options(tmp_path, profiles_root=make_firefox_profile(tmp_path))
    assert discover_session(options) is None


def test_other_browsers_are_read_through_rookiepy(tmp_path, monkeypatch) -> None:
    seen = []

    def chrome(domains=()):
        seen.append(tuple(domains))
        return [
            {"name": "session", "value": "from-chrome", "domain": ".arena.ai", "expires": FUTURE},
            {"name": "gtoken", "value": "", "domain": ".arena.ai"},  # recaptcha, empty value
            {"name": "pref", "value": "kept", "domain": ".arena.ai", "expires": FUTURE},
        ]

    monkeypatch.setattr(
        auto_module, "_rookiepy_module", lambda: types.SimpleNamespace(chrome=chrome)
    )
    found = discover_session(isolated_options(tmp_path, use_browser_profiles=True))
    assert found.source == "browser:chrome"
    # rookiepy already filtered by domain; empty values are legitimate cookies.
    assert found.cookies == {"session": "from-chrome", "gtoken": "", "pref": "kept"}
    # rookiepy takes bare domains, the same list the Firefox path queries with.
    assert seen[0] == ("arena.ai", "lmarena.ai")


def test_rookiepy_browsers_are_tried_in_a_fixed_order(tmp_path, monkeypatch) -> None:
    calls = []

    def factory(name):
        def reader(domains=()):
            calls.append(name)
            return []

        return reader

    monkeypatch.setattr(
        auto_module,
        "_rookiepy_module",
        lambda: types.SimpleNamespace(**{name: factory(name) for name in auto_module.ROOKIEPY_BROWSERS}),
    )
    options = isolated_options(tmp_path, use_browser_profiles=True, profiles_root=tmp_path / "none")
    assert discover_session(options) is None
    assert calls == list(auto_module.ROOKIEPY_BROWSERS)


def test_flat_record_filtering_drops_junk() -> None:
    expired = int(time.time()) - 10
    assert _flat_records(
        [
            {"name": "a", "value": "1", "domain": ".arena.ai", "expires": FUTURE},
            {"name": "", "value": "x"},
            {"name": "b", "domain": ".arena.ai"},  # no value
            {"name": "c", "value": "3", "expires": expired},  # expired
            {"name": "a", "value": "duplicate"},  # first record of a name wins
            {"name": "d", "value": "4", "domain": "other.test"},
            "not a mapping",
        ]
    ) == {"a": "1", "d": "4"}


def test_flat_records_survives_an_unexpected_shape() -> None:
    assert _flat_records("not a list") == {}
    assert _flat_records(None) == {}


def test_discovery_reports_every_source_it_considered(tmp_path) -> None:
    options = isolated_options(
        tmp_path, use_browser_profiles=True, profiles_root=make_firefox_profile(tmp_path)
    )
    joined = "\n".join(explain_sources(options))
    assert "cookies=" in joined
    assert "ARENA_COOKIE" in joined
    assert "stored cookies.json" in joined
    assert "Firefox profile" in joined
    assert "default-release" in joined
    assert "other browsers" in joined
    assert "rookiepy not installed" in joined


def test_discovery_reports_a_firefox_profile_it_cannot_use(tmp_path) -> None:
    options = isolated_options(
        tmp_path, use_browser_profiles=True, profiles_root=tmp_path / "empty"
    )
    lines = explain_sources(options)
    assert any("unavailable" in line for line in lines)


def test_discovery_explains_disabled_browser_profiles(tmp_path) -> None:
    joined = "\n".join(explain_sources(isolated_options(tmp_path)))
    assert "disabled by use_browser_profiles=False" in joined
    assert "Firefox profile" not in joined


# --------------------------------------------------------------------------- #
# Login flow discovery from the environment
# --------------------------------------------------------------------------- #


def test_complete_environment_builds_a_form_login_flow(monkeypatch) -> None:
    monkeypatch.setenv("ARENA_LOGIN_URL", "https://arena.ai/login")
    monkeypatch.setenv("ARENA_USERNAME", "user@example.com")
    monkeypatch.setenv("ARENA_PASSWORD", "secret")
    monkeypatch.setenv("ARENA_USER_SELECTOR", "#email")
    monkeypatch.setenv("ARENA_PASS_SELECTOR", "#pass")
    monkeypatch.setenv("ARENA_SUBMIT_SELECTOR", "button[type=submit]")
    monkeypatch.setenv("ARENA_SUCCESS_URL", "https://arena.ai/")
    flow = login_flow_from_env(AutoOptions(solve_captchas=False))
    assert isinstance(flow, FormLoginFlow)
    assert flow.login_url == "https://arena.ai/login"
    assert flow.username_selector == "#email"
    assert flow.password_selector == "#pass"
    assert flow.submit_selector == "button[type=submit]"
    assert flow.success_url_contains == "https://arena.ai/"
    assert flow.credentials() == ("user@example.com", "secret")


def test_partial_environment_configuration_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("ARENA_USERNAME", "user@example.com")
    options = AutoOptions(solve_captchas=False)
    assert login_flow_from_env(options) is None
    explanation = explain_login(options)
    assert "unavailable" in explanation
    assert "ARENA_PASSWORD" in explanation
    assert "ARENA_LOGIN_URL" in explanation


def login_flow() -> FormLoginFlow:
    return FormLoginFlow(
        login_url="https://arena.ai/login",
        username_selector="#email",
        password_selector="#pass",
        submit_selector="#go",
        username="user@example.com",
        password="secret",
    )


def test_explicit_login_flow_is_kept() -> None:
    flow = login_flow()
    options = AutoOptions(login_flow=flow, solve_captchas=False)
    assert login_flow_from_env(options) is flow
    assert explain_login(options) == "login flow: configured explicitly"


def test_environment_login_can_be_disabled(monkeypatch) -> None:
    for name, value in (
        ("ARENA_LOGIN_URL", "https://arena.ai/login"),
        ("ARENA_USERNAME", "user@example.com"),
        ("ARENA_PASSWORD", "secret"),
        ("ARENA_USER_SELECTOR", "#email"),
        ("ARENA_PASS_SELECTOR", "#pass"),
        ("ARENA_SUBMIT_SELECTOR", "#go"),
    ):
        monkeypatch.setenv(name, value)
    options = AutoOptions(login_from_env=False, solve_captchas=False)
    assert login_flow_from_env(options) is None
    assert "disabled" in explain_login(options)


def test_complete_environment_is_announced(monkeypatch) -> None:
    for name, value in (
        ("ARENA_LOGIN_URL", "https://arena.ai/login"),
        ("ARENA_USERNAME", "user@example.com"),
        ("ARENA_PASSWORD", "secret"),
        ("ARENA_USER_SELECTOR", "#email"),
        ("ARENA_PASS_SELECTOR", "#pass"),
        ("ARENA_SUBMIT_SELECTOR", "#go"),
    ):
        monkeypatch.setenv(name, value)
    assert explain_login(AutoOptions()) == "login flow: available from the environment"


# --------------------------------------------------------------------------- #
# Solver resolution
# --------------------------------------------------------------------------- #


def test_solver_stays_off_when_dependencies_are_missing(monkeypatch) -> None:
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: False)
    assert resolve_solver(AutoOptions()) is None


def test_solver_switches_itself_on_when_dependencies_exist(monkeypatch) -> None:
    monkeypatch.setattr(
        auto_module,
        "_has_spec",
        lambda name: name in {"playwright", "playwright_recaptcha", "playwright_stealth"},
    )
    solver = resolve_solver(AutoOptions())
    assert isinstance(solver, RecaptchaToolkitSolver)
    assert solver.backend == "audio"
    assert solver.apply_stealth is True


def test_solver_can_be_forced_on(monkeypatch) -> None:
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: False)
    solver = resolve_solver(AutoOptions(solve_captchas=True, captcha_backend="image"))
    assert isinstance(solver, RecaptchaToolkitSolver)
    assert solver.backend == "image"


def test_solver_can_be_forced_off_even_with_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: True)
    assert resolve_solver(AutoOptions(solve_captchas=False)) is None


def test_explicit_solver_object_is_used_verbatim() -> None:
    solver = TokenSolver()
    assert resolve_solver(AutoOptions(captcha_solver=solver)) is solver


def test_dependency_probe_understands_both_solvers(monkeypatch) -> None:
    installed: set[str] = set()
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: name in installed)
    assert solver_dependencies_available("audio") is False
    installed.update({"playwright", "playwright_stealth"})
    assert solver_dependencies_available("audio") is False
    installed.add("playwright_recaptcha")
    assert solver_dependencies_available("audio") is True
    installed.discard("playwright_recaptcha")
    assert solver_dependencies_available("vision_ai") is False
    installed.add("vision_ai_recaptcha_solver")
    assert solver_dependencies_available("vision_ai") is True


def test_dependency_probe_never_imports_a_solver(monkeypatch) -> None:
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: name == "playwright")
    assert solver_dependencies_available("audio") is False
    assert "playwright_recaptcha" not in sys.modules


# --------------------------------------------------------------------------- #
# Probe classification
# --------------------------------------------------------------------------- #


def test_probe_accepts_a_200() -> None:
    recorder = Recorder((200, CONFIG_JSON, "application/json"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert result.ok and not result.auth_problem
    assert result.reason == "session accepted"
    assert result.describe() == "ok: session accepted"


def test_probe_accepts_json_that_merely_mentions_a_captcha() -> None:
    body = json.dumps({"dependencies": [{"api_name": "g-recaptcha-reset"}]}).encode()
    recorder = Recorder((200, body, "application/json"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert result.ok and not result.auth_problem


def test_probe_reads_an_html_page_without_captcha_as_accepted() -> None:
    recorder = Recorder((200, b"<!doctype html><html><body>hi</body></html>", "text/html"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/text/direct")
    assert result.ok and not result.auth_problem


def test_probe_reads_a_403_as_an_auth_problem() -> None:
    recorder = Recorder((403, b"denied", "text/plain"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert not result.ok and result.auth_problem
    assert "rejected" in result.reason
    assert result.describe() == "not ok: session rejected or expired"


def test_probe_reads_a_cloudflare_challenge_as_an_auth_problem() -> None:
    """The transport itself refuses a known challenge page."""
    recorder = Recorder((200, CHALLENGE_HTML, "text/html"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert not result.ok and result.auth_problem
    assert "browser verification challenge" in result.reason
    assert result.error is not None


def test_probe_reads_a_bare_recaptcha_page_as_an_auth_problem() -> None:
    """A captcha interstitial with HTTP 200 and no Cloudflare marker."""
    recorder = Recorder((200, RECAPTCHA_PAGE, "text/html"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert not result.ok and result.auth_problem
    assert "challenge page instead of data" in result.reason
    assert "g-recaptcha" in result.reason


def test_probe_reads_a_rate_limit_as_not_an_auth_problem() -> None:
    recorder = Recorder((429, b"slow down", "text/plain"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert not result.ok and not result.auth_problem
    assert "rate limit" in result.reason.lower()


def test_probe_treats_a_redirect_as_reachable() -> None:
    recorder = Recorder((302, b"", "text/plain"))
    with ArenaClient(http_transport=recorder.transport) as client:
        result = probe(client, "/config")
    assert result.ok and not result.auth_problem
    assert "inconclusive" in result.reason


def test_async_probe_matches_the_sync_one() -> None:
    recorder = Recorder((403, b"denied", "text/plain"))

    async def run() -> ProbeResult:
        async with AsyncArenaClient(http_transport=recorder.transport) as client:
            return await aprobe(client, "/config")

    result = asyncio.run(run())
    assert result.auth_problem and "rejected" in result.reason


# --------------------------------------------------------------------------- #
# Bootstrap: building a client
# --------------------------------------------------------------------------- #


def test_bootstrap_sends_the_discovered_cookie(tmp_path) -> None:
    recorder = Recorder((200, CONFIG_JSON, "application/json"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    with auto_client(options) as client:
        assert client.session_manager is not None
        assert client.session_manager.load_cookies() == {"session": "abc"}
    assert "session=abc" in recorder.cookies(0)


def test_bootstrap_matches_the_fingerprint_of_the_source_browser(tmp_path) -> None:
    recorder = Recorder((200, b"{}", "application/json"))
    options = isolated_options(
        tmp_path,
        use_browser_profiles=True,
        profiles_root=make_firefox_profile(tmp_path),
        http_transport=recorder.transport,
    )
    with auto_client(options):
        pass
    headers = recorder.requests[0].headers
    assert "Firefox/156.0" in headers["user-agent"]
    assert "Gecko/20100101" in headers["user-agent"]
    assert "accept-language" in headers


def test_bootstrap_without_a_browser_source_uses_a_chromium_fingerprint(tmp_path) -> None:
    recorder = Recorder((200, b"{}", "application/json"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    with auto_client(options):
        pass
    assert "Chrome/" in recorder.requests[0].headers["user-agent"]


def test_bootstrap_heals_a_rejected_session_during_the_probe(tmp_path, monkeypatch) -> None:
    """The transport asks the ladder for fresh cookies and replays the request."""
    recorder = Recorder(
        (403, b"denied", "text/plain"),
        (200, CONFIG_JSON, "application/json"),
    )

    def heal(self, reason=None):
        self.adopt({"session": "fresh"})
        return {"session": "fresh"}

    monkeypatch.setattr(AutoSessionManager, "refresh_session", heal)
    options = isolated_options(tmp_path, cookies="session=stale", http_transport=recorder.transport)
    output = options.output
    with auto_client(options) as client:
        assert client.session_manager.load_cookies() == {"session": "fresh"}
    assert "session=stale" in recorder.cookies(0)
    assert "session=fresh" in recorder.cookies(1)
    assert "verification: ok" in output.getvalue()


def test_bootstrap_runs_the_ladder_when_the_transport_gives_up(tmp_path, monkeypatch) -> None:
    """Every attempt rejected: the ladder is tried once more, then reported."""
    recorder = Recorder()  # always 403 below

    def handler(request):
        recorder.requests.append(request)
        return httpx.Response(
            403, request=request, headers={"content-type": "text/plain"}, content=b"denied"
        )

    transport = httpx.MockTransport(handler)
    reasons: list = []

    def exhausted(self, reason=None):
        reasons.append(reason)
        return None

    monkeypatch.setattr(AutoSessionManager, "refresh_session", exhausted)
    options = isolated_options(tmp_path, cookies="session=stale", http_transport=transport)
    output = options.output
    with auto_client(options) as client:
        assert client.session_manager.load_cookies() == {"session": "stale"}
    assert reasons and all(isinstance(reason, AuthenticationError) for reason in reasons)
    assert "needs a refresh" in output.getvalue()
    assert "refresh produced no cookies" in output.getvalue()
    assert "verification: not ok" in output.getvalue()


def test_bootstrap_escalates_a_challenge_page_to_the_ladder(tmp_path) -> None:
    """A captcha interstitial is an auth problem, so the ladder is tried."""
    recorder = Recorder((200, RECAPTCHA_PAGE, "text/html"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    output = options.output
    with pytest.raises(AuthenticationError, match="no automatic refresh path"):
        auto_client(options)
    assert "needs a refresh" in output.getvalue()


def test_bootstrap_raises_when_a_rejected_session_cannot_be_healed(tmp_path) -> None:
    recorder = Recorder((403, b"denied", "text/plain"))
    options = isolated_options(tmp_path, cookies="session=stale", http_transport=recorder.transport)
    with pytest.raises(AuthenticationError, match="no automatic refresh path"):
        auto_client(options)


def test_bootstrap_without_verification_keeps_the_client(tmp_path) -> None:
    recorder = Recorder((403, b"denied", "text/plain"))
    options = isolated_options(
        tmp_path, cookies="session=abc", http_transport=recorder.transport, verify=False
    )
    with auto_client(options) as client:
        assert client.session_manager.load_cookies() == {"session": "abc"}
    assert recorder.requests == []


def test_bootstrap_asks_the_user_to_sign_in_when_nothing_is_found(tmp_path, monkeypatch) -> None:
    attempts = {"n": 0}
    prompts: list[str] = []

    def discovered_after_login(options):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return None
        return DiscoveredSession(
            source="env",
            cookies={"session": "after-login"},
            headers={},
            description="user signed in",
        )

    monkeypatch.setattr(auto_module, "discover_session", discovered_after_login)
    recorder = Recorder((200, b"{}", "application/json"))
    options = isolated_options(
        tmp_path,
        http_transport=recorder.transport,
        verify=False,
        prompt=lambda message: prompts.append(message) or "",
    )
    with auto_client(options) as client:
        assert client.session_manager.load_cookies() == {"session": "after-login"}
    assert len(prompts) == 1
    assert "sign in to your account" in prompts[0]
    assert "arena.ai" in prompts[0]


def test_bootstrap_gives_up_when_the_login_wait_is_cancelled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auto_module, "discover_session", lambda options: None)

    def interrupt(message):
        raise KeyboardInterrupt

    options = isolated_options(tmp_path, prompt=interrupt, interactive=True)
    with pytest.raises(AuthenticationError, match="no session found"):
        auto_client(options)


def test_bootstrap_refuses_to_guess_in_non_interactive_mode(tmp_path) -> None:
    options = isolated_options(tmp_path, interactive=False)
    with pytest.raises(AuthenticationError) as info:
        auto_client(options)
    message = str(info.value)
    assert "no session found" in message
    assert "Sign in to your account" in message
    assert "ARENA_COOKIE" in message
    assert "disabled by use_browser_profiles=False" in message


def test_bootstrap_accepts_cookies_read_from_a_session_manager(tmp_path) -> None:
    recorder = Recorder((200, b"{}", "application/json"))
    manager = BrowserSessionManager(base_url="https://arena.ai", domains=("arena.ai",))
    manager.import_cookies({"session": "injected"})
    options = isolated_options(
        tmp_path, cookies=manager.load_cookies(), http_transport=recorder.transport
    )
    with auto_client(options):
        pass
    assert "session=injected" in recorder.cookies(0)


def test_bootstrap_passes_extra_client_arguments(tmp_path) -> None:
    recorder = Recorder((200, b"{}", "application/json"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    client = auto_client(options, verify=False, timeout=7, max_auth_refreshes=5)
    assert client.transport._max_auth_refreshes == 5
    assert client.transport.session.timeout.connect == 7
    client.close()


def test_async_bootstrap_verifies_and_heals(tmp_path, monkeypatch) -> None:
    recorder = Recorder(
        (403, b"denied", "text/plain"),
        (200, CONFIG_JSON, "application/json"),
    )

    def heal(self, reason=None):
        self.adopt({"session": "fresh"})
        return {"session": "fresh"}

    monkeypatch.setattr(AutoSessionManager, "refresh_session", heal)
    options = isolated_options(tmp_path, cookies="session=stale", http_transport=recorder.transport)

    async def run() -> list[str]:
        client = await auto_async_client(options)
        try:
            assert await client.get_models() == []
        finally:
            await client.aclose()
        return [request.headers.get("cookie", "") for request in recorder.requests]

    cookies = asyncio.run(run())
    assert "session=stale" in cookies[0]
    assert any("session=fresh" in value for value in cookies[1:])


# --------------------------------------------------------------------------- #
# The refresh ladder
# --------------------------------------------------------------------------- #


def make_manager(**kwargs) -> AutoSessionManager:
    kwargs.setdefault("base_url", "https://arena.ai")
    kwargs.setdefault("domains", ("arena.ai",))
    return AutoSessionManager(**kwargs)


def test_ladder_adopts_fresh_cookies_from_the_source_browser() -> None:
    source = StubSource({"session": "renewed"})
    manager = make_manager(fallback=source)
    reason = AuthenticationError("rejected (403)")
    assert manager.refresh_session(reason) == {"session": "renewed"}
    assert manager.load_cookies() == {"session": "renewed"}
    assert source.refresh_reasons == [reason]


def test_ladder_survives_a_source_that_fails(monkeypatch) -> None:
    class Exploding:
        def refresh_session(self, reason=None):
            raise RuntimeError("profile is locked")

    async def fake_refresh(self, reason=None):
        return {"session": "browser"}

    monkeypatch.setattr(auto_module.BrowserSessionManager, "arefresh_session", fake_refresh)
    manager = make_manager(fallback=Exploding(), login_flow=login_flow())
    assert manager.refresh_session() == {"session": "browser"}


def test_ladder_falls_through_to_the_browser_path(monkeypatch) -> None:
    source = StubSource(None)
    manager = make_manager(fallback=source, login_flow=login_flow())

    async def fake_refresh(self, reason=None):
        return {"session": "from-browser"}

    monkeypatch.setattr(auto_module.BrowserSessionManager, "arefresh_session", fake_refresh)
    assert manager.refresh_session("expired") == {"session": "from-browser"}
    # The browser owns the session now, so the headers follow it.
    assert manager._from_fallback is False
    assert "Chrome/" in manager.firefox_headers()["User-Agent"]


def test_ladder_explains_itself_when_no_rung_exists() -> None:
    manager = make_manager()
    with pytest.raises(AuthenticationError) as info:
        manager.refresh_session(AuthenticationError("rejected (403)"))
    message = str(info.value)
    assert "no automatic refresh path" in message
    assert "sign in again in your browser" in message
    assert "ARENA_USERNAME" in message
    assert "solve_captchas=True" in message


def test_headers_follow_the_source_that_owns_the_cookies() -> None:
    source = StubSource({"session": "one"})
    manager = make_manager(fallback=source)
    manager.adopt({"session": "one"})
    assert "Firefox/156.0" in manager.firefox_headers()["User-Agent"]
    manager._from_fallback = False
    assert "Chrome/" in manager.firefox_headers()["User-Agent"]


def test_headers_survive_a_source_without_them() -> None:
    class Headerless:
        def load_cookies(self, domains=None):
            return {"session": "x"}

        def firefox_headers(self, *, navigation=False):
            raise RuntimeError("no headers")

        def refresh_session(self, reason=None):
            return None

    manager = make_manager(fallback=Headerless())
    assert "Chrome/" in manager.firefox_headers()["User-Agent"]


def test_adopt_replaces_the_cookie_set() -> None:
    manager = make_manager()
    manager.adopt({"session": "adopted", "other": "value"})
    assert manager.load_cookies() == {"session": "adopted", "other": "value"}


def test_build_session_manager_wires_the_ladder_and_solver(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        auto_module,
        "_has_spec",
        lambda name: name in {"playwright", "playwright_recaptcha", "playwright_stealth"},
    )
    source = StubSource({"session": "one"})
    discovered = DiscoveredSession(
        source="firefox", cookies={"session": "one"}, headers={}, manager=source
    )
    options = isolated_options(tmp_path, login_from_env=False, solve_captchas="auto")
    manager = build_session_manager(options, discovered)
    assert manager.fallback is source
    assert isinstance(manager.captcha_solver, RecaptchaToolkitSolver)
    assert manager.challenge_handler.solve_on_detection is True
    assert manager.base_url == options.base_url
    assert manager.domains == options.cookie_domains()
    assert manager.load_cookies() == {"session": "one"}


def test_build_session_manager_keeps_the_storage_path_for_stored_state(tmp_path) -> None:
    path = write_storage(tmp_path, {"session": "stored"})
    discovered = DiscoveredSession(source="storage", cookies={"session": "stored"}, headers={})
    options = isolated_options(tmp_path, storage_path=path)
    manager = build_session_manager(options, discovered)
    assert manager.storage_path == path
    assert manager.fallback is None
    assert manager.load_cookies() == {"session": "stored"}
    assert manager._from_fallback is False


def test_build_session_manager_reads_a_playwright_storage_state(tmp_path) -> None:
    path = tmp_path / "state" / "cookies.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "stored",
                        "domain": "arena.ai",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                    }
                ],
                "origins": [{"origin": "https://arena.ai", "localStorage": []}],
            }
        ),
        encoding="utf-8",
    )
    options = isolated_options(tmp_path, storage_path=path)
    discovered = discover_session(options)
    assert discovered.source == "storage"
    manager = build_session_manager(options, discovered)
    # The file is understood by the manager itself, so its origins survive.
    assert manager.load_cookies() == {"session": "stored"}
    assert manager._load_state()["origins"][0]["origin"] == "https://arena.ai"


def test_build_session_manager_ignores_the_storage_path_for_other_sources(tmp_path) -> None:
    discovered = DiscoveredSession(source="env", cookies={"session": "env"}, headers={})
    options = isolated_options(tmp_path, storage_path=tmp_path / "state.json")
    manager = build_session_manager(options, discovered)
    assert manager.storage_path is None
    assert manager.load_cookies() == {"session": "env"}


# --------------------------------------------------------------------------- #
# Direct Chat
# --------------------------------------------------------------------------- #


def test_direct_client_uses_the_self_healing_session_source(tmp_path) -> None:
    recorder = Recorder((200, b"<html></html>", "text/html"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    client = auto_direct_client(options)
    assert isinstance(client.session_manager, AutoSessionManager)
    assert client.transport._session_refresher is not None
    client.close()


def test_async_direct_client_is_wired_the_same_way(tmp_path) -> None:
    recorder = Recorder((200, b"<html></html>", "text/html"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)

    async def run() -> bool:
        client = await auto_module.auto_async_direct_client(options)
        await client.aclose()
        return client.transport._session_refresher is not None

    assert asyncio.run(run()) is True


def test_direct_token_factory_pins_the_toolkit_solver_to_v3() -> None:
    factory = DirectVerificationToken(
        AutoOptions(captcha_solver=RecaptchaToolkitSolver(version="v2"), solve_captchas=False)
    )
    assert isinstance(factory.solver, RecaptchaToolkitSolver)
    assert factory.solver.version == "v3"
    assert factory.solver.reload_for_v3 is True
    assert factory.page_url.endswith("/text/direct?model_a=max")


def test_direct_token_factory_accepts_a_custom_solver() -> None:
    solver = TokenSolver()
    factory = DirectVerificationToken(AutoOptions(captcha_solver=solver, solve_captchas=False))
    assert factory.solver is solver


def test_direct_token_factory_honours_an_explicit_page_url() -> None:
    factory = DirectVerificationToken(
        AutoOptions(captcha_solver=TokenSolver(), solve_captchas=False),
        page_url="https://example.test/direct",
    )
    assert factory.page_url == "https://example.test/direct"


def test_direct_token_factory_solves_on_the_direct_page(monkeypatch) -> None:
    class FakePage:
        url = "https://arena.ai/text/direct?model_a=max"

        def __init__(self):
            self.visited = []

        async def goto(self, url, **kwargs):
            self.visited.append((url, kwargs.get("wait_until")))

    class FakeEnvironment:
        instances = []

        def __init__(self, options):
            self.options = options
            self.page = FakePage()
            FakeEnvironment.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def new_page(self):
            return self.page

    monkeypatch.setattr(auto_module, "BrowserEnvironment", FakeEnvironment)
    solver = TokenSolver("direct-token")
    factory = DirectVerificationToken(AutoOptions(captcha_solver=solver, solve_captchas=False))
    assert asyncio.run(factory.afetch()) == "direct-token"
    assert solver.calls == ["https://arena.ai/text/direct?model_a=max"]
    environment = FakeEnvironment.instances[0]
    assert environment.page.visited[0][1] == "domcontentloaded"
    options = environment.options
    assert options.captcha_solver is solver
    assert options.solve_challenges is True
    assert options.headless is True
    assert options.identify_automation is False


def test_direct_token_factory_fails_fast_without_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: False)
    factory = DirectVerificationToken(AutoOptions(solve_captchas=False))
    with pytest.raises(AuthenticationError, match="auth-captcha"):
        asyncio.run(factory.afetch())


def test_direct_token_factory_is_callable_sync_style(monkeypatch) -> None:
    async def fake_fetch(self):
        return "sync-token"

    monkeypatch.setattr(DirectVerificationToken, "afetch", fake_fetch)
    factory = DirectVerificationToken(AutoOptions(captcha_solver=TokenSolver()))
    assert factory() == "sync-token"


def test_verification_token_auto_selects_the_factory(tmp_path) -> None:
    recorder = Recorder((200, b"<html></html>", "text/html"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    client = auto_direct_client(options, verification_token="auto")
    assert isinstance(client.verification_token, DirectVerificationToken)
    client.close()


def test_explicit_verification_token_is_kept(tmp_path) -> None:
    recorder = Recorder((200, b"<html></html>", "text/html"))
    options = isolated_options(tmp_path, cookies="session=abc", http_transport=recorder.transport)
    client = auto_direct_client(options, verification_token="03TOKEN")
    assert client.verification_token == "03TOKEN"
    client.close()


def test_options_are_replaceable(tmp_path) -> None:
    options = isolated_options(tmp_path, cookies="session=abc")
    tweaked = replace(options, verify=False)
    assert tweaked.verify is False
    assert tweaked.cookies == "session=abc"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def patch_cli(monkeypatch, client, result: ProbeResult | None = None) -> None:
    monkeypatch.setattr(auto_module, "auto_client", lambda options=None, **kwargs: client)
    monkeypatch.setattr(auto_module, "probe", lambda client, path: result or ProbeResult(True, "session accepted"))


def test_cli_explain_needs_no_session(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto_module, "_has_spec", lambda name: False)
    assert main(["--explain"]) == 0
    out = capsys.readouterr().out
    assert "cookies=" in out
    assert "login flow" in out
    assert "captcha solving" in out
    assert "dependencies missing" in out


def test_cli_status_reports_a_missing_session(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["--status", "--no-verify", "--no-browser-profiles"]) == 1
    assert "none found" in capsys.readouterr().out


def test_cli_status_reports_a_found_session_without_verifying(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_COOKIE", "session=abc")
    assert main(["--status", "--no-verify", "--no-browser-profiles"]) == 0
    assert "ARENA_COOKIE" in capsys.readouterr().out


def test_cli_status_verifies_a_found_session(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_COOKIE", "session=abc")
    client = DummyClient()
    patch_cli(monkeypatch, client)
    assert main(["--status", "--no-browser-profiles"]) == 0
    out = capsys.readouterr().out
    assert "ARENA_COOKIE" in out
    assert "verification" in out and "ok" in out
    assert client.closed is True


def test_cli_status_reports_a_rejected_session(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_COOKIE", "session=abc")
    patch_cli(
        monkeypatch,
        DummyClient(),
        ProbeResult(False, "session rejected", auth_problem=True),
    )
    assert main(["--status", "--no-browser-profiles"]) == 2
    assert "not ok: session rejected" in capsys.readouterr().out


def test_cli_lists_models(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_COOKIE", "session=abc")
    patch_cli(monkeypatch, DummyClient([ModelInfo(id="max", display_name="Max")]))
    assert main(["--models", "--no-browser-profiles"]) == 0
    out = capsys.readouterr().out
    assert "models" in out
    assert "max — Max" in out


def test_cli_sends_a_prompt(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_COOKIE", "session=abc")
    patch_cli(monkeypatch, DummyClient())
    assert main(["--prompt", "Hello", "--no-verify", "--no-browser-profiles"]) == 0
    assert "echo: Hello" in capsys.readouterr().out


def test_cli_without_a_task_reports_a_ready_session(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARENA_COOKIE", "session=abc")
    patch_cli(monkeypatch, DummyClient())
    assert main(["--no-verify", "--no-browser-profiles"]) == 0
    assert "ready" in capsys.readouterr().out


def test_cli_reports_a_bootstrap_failure_on_stderr(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    def boom(options=None, **kwargs):
        raise AuthenticationError("no session found")

    monkeypatch.setattr(auto_module, "auto_client", boom)
    assert main(["--prompt", "Hello", "--no-browser-profiles"]) == 1
    captured = capsys.readouterr()
    assert "no session found" in captured.err
    assert captured.out == ""


def test_documented_command_runs_without_warnings(tmp_path) -> None:
    """``python -m arena_api --explain`` is the command the README promises."""
    completed = subprocess.run(
        [sys.executable, "-m", "arena_api", "--explain"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONWARNINGS": "error::RuntimeWarning"},
    )
    assert completed.returncode == 0, completed.stderr
    assert "cookies=" in completed.stdout
    assert "RuntimeWarning" not in completed.stderr


def test_cli_maps_flags_onto_options(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    options = _options_from_args(
        build_arg_parser().parse_args(
            [
                "--base-url",
                "https://example.test",
                "--cookies",
                "session=abc",
                "--storage",
                "state/cookies.json",
                "--no-browser-profiles",
                "--non-interactive",
                "--solve",
                "on",
                "--backend",
                "image",
                "--probe-path",
                "/queue/status",
                "--no-verify",
            ]
        )
    )
    assert options.base_url == "https://example.test"
    assert options.cookies == "session=abc"
    assert options.storage_path == Path("state/cookies.json")
    assert options.use_browser_profiles is False
    assert options.interactive is False
    assert options.solve_captchas is True
    assert options.captcha_backend == "image"
    assert options.probe_path == "/queue/status"
    assert options.verify is False


def test_cli_solve_off_disables_the_solver(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    options = _options_from_args(build_arg_parser().parse_args(["--solve", "off"]))
    assert options.solve_captchas is False


def test_cli_defaults_to_automatic_solving(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    options = _options_from_args(build_arg_parser().parse_args([]))
    assert options.solve_captchas == "auto"
    assert options.verify is True
    assert options.interactive is True
    assert options.use_browser_profiles is True
