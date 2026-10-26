"""The redacted profile report: useful to a human, safe to paste.

A cookie value *is* the account, so the report must be diagnosable without it:
the tests below build a profile holding a recognisable secret and assert it never
reaches the text or the JSON, while the facts that actually explain a broken
setup -- which profile, which host, which name, how long the value is, whether
it expired, whether the site lives on another domain -- all do.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from arena_api.auth.diagnostics import (
    CookieFingerprint,
    collect_firefox_report,
    format_firefox_report,
    report_as_json,
)
from arena_api.auth.firefox_session import FIREFOX_PROFILE_ENV, FIREFOX_ROOT_ENV

import arena_api.auth.firefox_session as firefox_module

SECRET = "SUPER-SECRET-SESSION-VALUE-9f2b"
FUTURE = int(time.time()) + 86_400
PAST = int(time.time()) - 86_400

SCHEMA = """
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


def make_profile(path: Path, rows=()) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "cookies.sqlite")
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO moz_cookies"
        " (name, value, host, path, expiry, lastAccessed, creationTime,"
        " isSecure, isHttpOnly, sameSite)"
        " VALUES (?, ?, ?, '/', ?, 0, 0, 1, 1, 0)",
        rows,
    )
    connection.commit()
    connection.close()
    (path / "prefs.js").write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_firefox_env(monkeypatch):
    for name in (FIREFOX_ROOT_ENV, FIREFOX_PROFILE_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "firefox"
    (root / "Profiles").mkdir(parents=True)
    return root


def test_the_report_never_contains_a_cookie_value(root: Path) -> None:
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [
            ("session", SECRET, ".arena.ai", FUTURE),
            ("cf_clearance", SECRET + "-cf", ".arena.ai", FUTURE),
        ],
    )
    report = collect_firefox_report(profiles_root=root)
    rendered = "\n".join(format_firefox_report(report))
    serialized = report_as_json(report)
    assert SECRET not in rendered
    assert SECRET not in serialized
    # ... and yet the session is fully described.
    assert "session" in rendered
    assert str(len(SECRET)) in rendered
    fingerprint = report["profiles"][0]["cookies"][0]["value_sha256"]
    assert fingerprint and len(fingerprint) == 8
    assert report["cookies"]["names"] == ["cf_clearance", "session"]


def test_the_report_points_at_the_profile_that_holds_the_session(root: Path) -> None:
    make_profile(root / "Profiles" / "aaa.default-release", [])
    wanted = make_profile(
        root / "Profiles" / "bbb.work", [("session", SECRET, ".arena.ai", FUTURE)]
    )
    (root / "profiles.ini").write_text(
        "[Install4F96]\nDefault=Profiles/aaa.default-release\n", encoding="utf-8"
    )
    report = collect_firefox_report(profiles_root=root)
    assert report["selected"]["profile"] == str(wanted)
    assert "holds the session" in report["selected"]["reason"]
    rendered = "\n".join(format_firefox_report(report))
    assert "profile 2" in rendered and "(selected)" in rendered


def test_expired_cookies_are_reported_as_such(root: Path) -> None:
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", SECRET, ".arena.ai", PAST)],
    )
    report = collect_firefox_report(profiles_root=root)
    assert report["profiles"][0]["expired_matching"] == 1
    assert report["profiles"][0]["matching"] == 0
    assert any("expired" in warning for warning in report["warnings"])
    assert any("holds a cookie" in warning for warning in report["warnings"])


def test_a_session_on_another_arena_host_is_called_out(root: Path) -> None:
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", SECRET, ".arena.evil.example", FUTURE)],
    )
    report = collect_firefox_report(profiles_root=root)
    assert report["profiles"][0]["other_arena_hosts"] == [".arena.evil.example"]
    joined = " ".join(report["warnings"])
    assert "other Arena hosts" in joined
    assert "--base-url" in joined


def test_a_cookie_for_a_subdomain_is_not_silently_reused(root: Path) -> None:
    """A browser would not send ``beta.arena.ai`` cookies to ``arena.ai``."""
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", SECRET, ".beta.arena.ai", FUTURE)],
    )
    report = collect_firefox_report(profiles_root=root)
    assert report["profiles"][0]["subdomain_hosts"] == [".beta.arena.ai"]
    joined = " ".join(report["warnings"])
    assert "subdomain .beta.arena.ai" in joined
    assert "https://beta.arena.ai" in joined


def test_cookies_without_a_session_like_name_are_flagged(root: Path) -> None:
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("theme", "dark", ".arena.ai", FUTURE)],
    )
    report = collect_firefox_report(profiles_root=root)
    assert any("looks like a session token" in warning for warning in report["warnings"])


def test_a_missing_root_is_named_everywhere(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nowhere"
    monkeypatch.setattr(firefox_module, "default_profiles_root", lambda: missing)
    monkeypatch.setattr(firefox_module, "_platform_profile_roots", lambda: ())
    report = collect_firefox_report()
    assert report["roots"] == [
        {"path": str(missing), "exists": False, "profiles_ini": False}
    ]
    assert any("no Firefox profiles directory exists" in item for item in report["warnings"])
    rendered = "\n".join(format_firefox_report(report))
    assert "no Firefox profiles directory exists" in rendered
    assert str(missing) in rendered


def test_the_report_is_json_serialisable(root: Path) -> None:
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", SECRET, ".arena.ai", FUTURE)],
    )
    payload = json.loads(report_as_json(collect_firefox_report(profiles_root=root)))
    assert payload["arena_api"]
    assert payload["domains"] == ["arena.ai", "lmarena.ai"]
    assert payload["profiles"][0]["cookies"][0]["expires"].endswith("Z")
    assert payload["profiles"][0]["cookies_sqlite"]["journal_mode"]


def test_an_explicit_cookie_database_is_reported(tmp_path: Path) -> None:
    database = make_profile(tmp_path / "elsewhere", [("session", SECRET, ".arena.ai", FUTURE)])
    report = collect_firefox_report(cookie_database=database / "cookies.sqlite")
    assert report["profiles"][0]["profile"] == str(database)
    assert report["cookies"]["count"] == 1


def test_a_corrupt_database_does_not_break_the_report(tmp_path: Path) -> None:
    profile = tmp_path / "broken"
    profile.mkdir()
    (profile / "cookies.sqlite").write_bytes(b"this is not a database")
    report = collect_firefox_report(cookie_database=profile / "cookies.sqlite")
    assert report["profiles"][0]["cookies_sqlite"]["exists"] is True
    assert report["profiles"][0].get("error") or report["profiles"][0]["matching"] == 0
    assert isinstance(format_firefox_report(report), list)


def test_environment_overrides_are_reported_without_their_values(
    root: Path, monkeypatch
) -> None:
    make_profile(root / "Profiles" / "aaa.default-release", [])
    monkeypatch.setenv(FIREFOX_ROOT_ENV, str(root))
    monkeypatch.setenv(FIREFOX_PROFILE_ENV, "aaa.default-release")
    report = collect_firefox_report()
    assert report["environment"] == {
        FIREFOX_ROOT_ENV: "set",
        FIREFOX_PROFILE_ENV: "set",
    }
    assert report["selected"]["profile"].endswith("aaa.default-release")


def test_a_cookie_fingerprint_describes_instead_of_revealing() -> None:
    fingerprint = CookieFingerprint(
        host=".arena.ai",
        name="session",
        path="/",
        expires=FUTURE,
        secure=True,
        http_only=True,
        same_site="lax",
        container="",
        value_length=len(SECRET),
        value_sha256="deadbeef",
        shape="opaque",
        looks_like_auth=True,
    )
    described = fingerprint.describe()
    assert SECRET not in described
    assert "sha256=deadbeef" in described
    assert described.startswith("*")
    assert fingerprint.as_dict()["expires"].endswith("Z")
