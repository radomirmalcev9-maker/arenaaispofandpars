"""Reading a profile the way a running Firefox actually leaves it.

Firefox keeps ``cookies.sqlite`` in WAL mode: a session created by a page load
right now lives in ``cookies.sqlite-wal`` until a checkpoint, and a reader that
copies the files in the wrong order can miss it entirely. That failure looks
exactly like "the library does not read my cookies" -- the browser is signed in,
the reader says zero. These tests pin the behaviour that replaced the copy-only
reader, plus the report improvements that make the situation legible.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from arena_api.auth.diagnostics import collect_firefox_report, format_firefox_report
from arena_api.auth.firefox_session import (
    ANONYMOUS_COOKIE_HINTS,
    AUTH_COOKIE_HINTS,
    FirefoxSessionManager,
    looks_like_anonymous_cookie,
    looks_like_auth_cookie,
    read_firefox_cookie_records,
)

import arena_api.auth.firefox_session as firefox_module

FUTURE = int(time.time()) + 86_400
PAST = int(time.time()) - 5 * 86_400

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

INSERT = (
    "INSERT INTO moz_cookies"
    " (name, value, host, path, expiry, lastAccessed, creationTime,"
    " isSecure, isHttpOnly)"
    " VALUES (?, ?, ?, '/', ?, 0, 0, 1, 1)"
)


def make_profile(path: Path, rows=(), *, wal: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "cookies.sqlite")
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    connection.executemany(INSERT, rows)
    connection.commit()
    connection.close()
    (path / "prefs.js").write_text("", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# A signed-in session that only exists in the write-ahead log
# --------------------------------------------------------------------------- #


def test_a_session_sitting_in_the_wal_is_read(tmp_path) -> None:
    profile = make_profile(tmp_path / "p", [("theme", "dark", ".arena.ai", FUTURE)])
    # Firefox has the database open and has just stored the sign-in cookie:
    # committed, visible to WAL readers, absent from cookies.sqlite itself.
    writer = sqlite3.connect(profile / "cookies.sqlite")
    writer.execute(
        INSERT, ("arena-auth-prod-v1.0", "s1gn3d-1n", ".arena.ai", FUTURE)
    )
    writer.commit()
    try:
        records = read_firefox_cookie_records(
            profile / "cookies.sqlite", ["arena.ai"]
        )
        names = {record["name"] for record in records}
        assert names == {"theme", "arena-auth-prod-v1.0"}
        assert (profile / "cookies.sqlite-wal").exists()
    finally:
        writer.close()


def test_the_profile_is_only_read(tmp_path) -> None:
    profile = make_profile(
        tmp_path / "p", [("arena-auth-prod-v1.0", "v", ".arena.ai", FUTURE)]
    )
    database = profile / "cookies.sqlite"
    before = database.read_bytes()
    read_firefox_cookie_records(database, ["arena.ai"])
    assert database.read_bytes() == before


def test_an_unreadable_database_falls_back_to_a_snapshot(tmp_path, monkeypatch) -> None:
    profile = make_profile(
        tmp_path / "p", [("arena-auth-prod-v1.0", "from-snapshot", ".arena.ai", FUTURE)]
    )

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(firefox_module, "_read_live_database", refuse)
    records = read_firefox_cookie_records(profile / "cookies.sqlite", ["arena.ai"])
    assert [record["value"] for record in records] == ["from-snapshot"]


def test_expired_rows_can_be_inspected_on_request(tmp_path) -> None:
    profile = make_profile(
        tmp_path / "p",
        [
            ("arena-auth-prod-v1.0", "dead", ".arena.ai", PAST),
            ("arena_build_bucket", "live", ".arena.ai", FUTURE),
        ],
    )
    live = read_firefox_cookie_records(profile / "cookies.sqlite", ["arena.ai"])
    assert [record["name"] for record in live] == ["arena_build_bucket"]
    both = read_firefox_cookie_records(
        profile / "cookies.sqlite", ["arena.ai"], include_expired=True
    )
    flags = {record["name"]: record["expired"] for record in both}
    assert flags == {"arena-auth-prod-v1.0": True, "arena_build_bucket": False}


# --------------------------------------------------------------------------- #
# What counts as a login cookie
# --------------------------------------------------------------------------- #


def test_login_cookie_names_are_recognised() -> None:
    for name in (
        "arena-auth-prod-v1.0",
        "session",
        "__Secure-next-auth.session-token",
        "auth_token",
        "user_id",
    ):
        assert looks_like_auth_cookie(name), name
    for name in (
        "_ga",
        "cf_clearance",
        "__cf_bm",
        "_dd_s",
        "sidebar_state",
        "user_country_code",
        "provisional_user_id",  # contains "user_id" but marks a visitor
    ):
        assert not looks_like_auth_cookie(name), name


def test_visitor_cookies_are_recognised() -> None:
    assert looks_like_anonymous_cookie("provisional_user_id")
    assert looks_like_anonymous_cookie("guest_id")
    assert not looks_like_anonymous_cookie("arena_build_bucket")
    assert "provisional_user_id" in ANONYMOUS_COOKIE_HINTS
    assert "arena-auth" in AUTH_COOKIE_HINTS


# --------------------------------------------------------------------------- #
# The report says what happened, with dates
# --------------------------------------------------------------------------- #


def test_a_session_that_expired_is_reported_with_its_date(tmp_path) -> None:
    profile = make_profile(
        tmp_path / "p", [("arena-auth-prod-v1.0", "dead", ".arena.ai", PAST)]
    )
    manager = FirefoxSessionManager(profiles_root=profile.parent, interactive=False)
    report = collect_firefox_report(profiles_root=profile.parent)
    verdict = report["profiles"][0]["verdict"]
    assert verdict.startswith("was signed in, but the session cookie(s)")
    assert "arena-auth-prod-v1.0" in verdict
    assert "expired on" in verdict
    # The expired row itself is listed, so the date is visible, not just counted.
    expired = report["profiles"][0]["expired_cookies"]
    assert [cookie["name"] for cookie in expired] == ["arena-auth-prod-v1.0"]
    assert manager.load_cookies() == {}
    rendered = "\n".join(format_firefox_report(report))
    assert "EXPIRED" in rendered


def test_a_visitor_only_profile_is_called_what_it_is(tmp_path) -> None:
    profile = make_profile(
        tmp_path / "p",
        [
            ("provisional_user_id", "abc", ".arena.ai", FUTURE),
            ("arena_build_bucket", "b", ".arena.ai", FUTURE),
        ],
    )
    report = collect_firefox_report(profiles_root=profile.parent)
    verdict = report["profiles"][0]["verdict"]
    assert verdict.startswith("not signed in")
    assert "provisional_user_id" in verdict
    assert any(
        "no profile holds a live login cookie" in warning
        for warning in report["warnings"]
    )


def test_a_signed_in_profile_is_reported_as_signed_in(tmp_path) -> None:
    profile = make_profile(
        tmp_path / "p",
        [
            ("arena-auth-prod-v1.0", "alive", ".arena.ai", FUTURE),
            ("_ga", "GA1", ".arena.ai", FUTURE),
        ],
    )
    report = collect_firefox_report(profiles_root=profile.parent)
    assert report["profiles"][0]["verdict"] == "signed in: arena-auth-prod-v1.0"
    assert not any(
        "no profile holds a live login cookie" in warning
        for warning in report["warnings"]
    )


def test_a_profile_never_visited_says_so(tmp_path) -> None:
    profile = make_profile(tmp_path / "p", [])
    report = collect_firefox_report(profiles_root=profile.parent)
    assert "never opened in this profile" in report["profiles"][0]["verdict"]


# --------------------------------------------------------------------------- #
# Windows: the Microsoft Store build keeps its profile elsewhere
# --------------------------------------------------------------------------- #


def test_store_package_profile_root_is_searched(monkeypatch, tmp_path) -> None:
    import sys

    local = tmp_path / "Local"
    package = (
        local
        / "Packages"
        / "Mozilla.Firefox_1abc2def"
        / "LocalCache"
        / "Roaming"
        / "Mozilla"
        / "Firefox"
    )
    package.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(
        firefox_module, "default_profiles_root", lambda: tmp_path / "Roaming" / "Firefox"
    )
    monkeypatch.delenv("ARENA_FIREFOX_ROOT", raising=False)
    roots = firefox_module.firefox_profiles_roots()
    assert package in roots


def test_no_store_packages_means_no_extra_root(monkeypatch, tmp_path) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("ARENA_FIREFOX_ROOT", raising=False)
    assert firefox_module._platform_profile_roots() == ()
