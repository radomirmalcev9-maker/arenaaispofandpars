"""Profile discovery and reading, beyond the single-default-profile case.

The reader used to resolve *one* profile: the one ``profiles.ini`` calls the
default. On a machine with several profiles that is regularly not the profile
holding the signed-in session, so ``load_cookies()`` returned an empty mapping
while the cookies sat one directory away. These tests pin the behaviour that
replaced it: every profile of every root is ranked, and the one with content
wins -- without disturbing the older preference order when nothing has content.
"""

from __future__ import annotations

import io
import sqlite3
import time
from pathlib import Path

import pytest

from arena_api.auth.firefox_session import (
    FIREFOX_PROFILE_ENV,
    FIREFOX_ROOT_ENV,
    FirefoxSessionError,
    FirefoxSessionManager,
    firefox_profiles_roots,
)

import arena_api.auth.firefox_session as firefox_module

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


def make_profile(path: Path, rows=(), *, prefs: bool = True) -> Path:
    """Create a profile directory with a ``cookies.sqlite`` and ``prefs.js``."""
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "cookies.sqlite")
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO moz_cookies"
        " (name, value, host, path, expiry, lastAccessed, creationTime,"
        " isSecure, isHttpOnly)"
        " VALUES (?, ?, ?, '/', ?, 0, 0, 1, 1)",
        rows,
    )
    connection.commit()
    connection.close()
    if prefs:
        (path / "prefs.js").write_text("", encoding="utf-8")
    return path


def make_tree(tmp_path: Path, name: str = "firefox") -> Path:
    root = tmp_path / name
    (root / "Profiles").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def clean_firefox_env(monkeypatch):
    """Host-level overrides must never decide a test."""
    for name in (FIREFOX_ROOT_ENV, FIREFOX_PROFILE_ENV):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# The bug: the configured default is not always the profile with the session
# --------------------------------------------------------------------------- #


def test_the_profile_holding_the_cookies_wins_over_the_configured_default(
    tmp_path,
) -> None:
    root = make_tree(tmp_path)
    make_profile(root / "Profiles" / "aaa.default-release", [("pref", "x", ".example.com", FUTURE)])
    work = make_profile(
        root / "Profiles" / "bbb.work",
        [("session", "from-work", ".arena.ai", FUTURE)],
    )
    (root / "profiles.ini").write_text(
        "[Install4F96D1932A9F858E]\n"
        "Default=Profiles/aaa.default-release\n"
        "\n"
        "[Profile1]\n"
        "Name=default-release\n"
        "IsRelative=1\n"
        "Path=Profiles/aaa.default-release\n"
        "Default=1\n"
        "\n"
        "[Profile0]\n"
        "Name=work\n"
        "IsRelative=1\n"
        "Path=Profiles/bbb.work\n",
        encoding="utf-8",
    )
    output = io.StringIO()
    manager = FirefoxSessionManager(
        profiles_root=root, interactive=False, output=output, max_cookie_attempts=1
    )
    manager._rookiepy_module = lambda: None
    assert manager.profile_path() == work
    assert manager.load_cookies() == {"session": "from-work"}
    # The switch is explained, not silent: a user who expected the default
    # profile needs to know another one was read.
    assert "holds the cookies" in output.getvalue()


def test_the_configured_default_is_kept_when_it_has_the_cookies(tmp_path) -> None:
    root = make_tree(tmp_path)
    default = make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", "from-default", ".arena.ai", FUTURE)],
    )
    make_profile(root / "Profiles" / "bbb.work", [("session", "stale", ".arena.ai", FUTURE)])
    (root / "profiles.ini").write_text(
        "[Install4F96D1932A9F858E]\nDefault=Profiles/aaa.default-release\n",
        encoding="utf-8",
    )
    manager = FirefoxSessionManager(profiles_root=root, interactive=False)
    manager._rookiepy_module = lambda: None
    assert manager.profile_path() == default
    assert manager.load_cookies() == {"session": "from-default"}


def test_profile_order_is_kept_when_no_profile_has_cookies(tmp_path) -> None:
    """Without content the old ranking still decides -- and says so honestly."""
    root = make_tree(tmp_path)
    release = make_profile(root / "Profiles" / "aaa.default-release", [])
    manager = FirefoxSessionManager(profiles_root=root, interactive=False)
    assert manager.profile_path() == release
    assert "no cookies for arena.ai" in manager.selection_reason()


# --------------------------------------------------------------------------- #
# Roots: snap, Flatpak, portable installs, environment overrides
# --------------------------------------------------------------------------- #


def test_all_platform_roots_are_searched(tmp_path, monkeypatch) -> None:
    """A snap install keeps the profile where the conventional path is empty."""
    home = tmp_path / "home"
    snap_root = home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"
    (snap_root / "Profiles").mkdir(parents=True)
    profile = make_profile(
        snap_root / "Profiles" / "ccc.default-release",
        [("session", "from-snap", ".arena.ai", FUTURE)],
    )
    monkeypatch.setattr(firefox_module, "default_profiles_root", lambda: home / ".mozilla" / "firefox")
    monkeypatch.setattr(
        firefox_module,
        "_platform_profile_roots",
        lambda: (str(home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"),),
    )
    roots = firefox_profiles_roots()
    assert roots[0] == home / ".mozilla" / "firefox"
    assert snap_root in roots

    manager = FirefoxSessionManager(interactive=False, max_cookie_attempts=1)
    manager._rookiepy_module = lambda: None
    assert manager.profile_path() == profile
    assert manager.load_cookies() == {"session": "from-snap"}


def test_root_override_from_the_environment(tmp_path, monkeypatch) -> None:
    root = make_tree(tmp_path)
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", "from-override", ".arena.ai", FUTURE)],
    )
    monkeypatch.setenv(FIREFOX_ROOT_ENV, str(root))
    manager = FirefoxSessionManager(interactive=False, max_cookie_attempts=1)
    manager._rookiepy_module = lambda: None
    assert manager.profile_path().parent.parent == root
    assert manager.load_cookies() == {"session": "from-override"}


def test_root_override_may_point_at_profiles_ini(tmp_path, monkeypatch) -> None:
    root = make_tree(tmp_path)
    make_profile(root / "Profiles" / "aaa.default-release", [("session", "v", ".arena.ai", FUTURE)])
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=d\nIsRelative=1\nPath=Profiles/aaa.default-release\nDefault=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(FIREFOX_ROOT_ENV, str(root / "profiles.ini"))
    manager = FirefoxSessionManager(interactive=False)
    assert manager.profiles_roots() == (root,)


def test_profile_override_from_the_environment(tmp_path, monkeypatch) -> None:
    root = make_tree(tmp_path)
    wanted = make_profile(
        root / "Profiles" / "bbb.work", [("session", "pinned", ".arena.ai", FUTURE)]
    )
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", "other", ".arena.ai", FUTURE)],
    )
    monkeypatch.setenv(FIREFOX_ROOT_ENV, str(root))
    monkeypatch.setenv(FIREFOX_PROFILE_ENV, "bbb.work")
    manager = FirefoxSessionManager(interactive=False)
    manager._rookiepy_module = lambda: None
    assert manager.profile_path() == wanted
    assert manager.load_cookies() == {"session": "pinned"}


def test_a_profile_name_is_looked_up_in_every_root(tmp_path, monkeypatch) -> None:
    first = make_tree(tmp_path, "first")
    second = make_tree(tmp_path, "second")
    wanted = make_profile(
        second / "Profiles" / "shared.name", [("session", "second", ".arena.ai", FUTURE)]
    )
    monkeypatch.setattr(
        firefox_module,
        "firefox_profiles_roots",
        lambda: (first, second),
    )
    manager = FirefoxSessionManager(profile_name="shared.name", interactive=False)
    assert manager.profile_candidates() == [wanted]


def test_container_directories_are_not_mistaken_for_profiles(tmp_path) -> None:
    root = make_tree(tmp_path)
    make_profile(root / "Profiles" / "aaa.default-release", [])
    manager = FirefoxSessionManager(profiles_root=root, interactive=False)
    names = [candidate.name for candidate in manager.profile_candidates()]
    assert names == ["aaa.default-release"]
    assert "Profiles" not in names


def test_every_root_is_named_when_none_exists(tmp_path, monkeypatch) -> None:
    missing_a = tmp_path / "a"
    missing_b = tmp_path / "b"
    monkeypatch.setattr(
        firefox_module, "firefox_profiles_roots", lambda: (missing_a, missing_b)
    )
    manager = FirefoxSessionManager(interactive=False)
    with pytest.raises(FirefoxSessionError) as excinfo:
        manager.profile_path()
    message = str(excinfo.value)
    assert str(missing_a) in message and str(missing_b) in message


# --------------------------------------------------------------------------- #
# rookiepy only ever reads the default profile
# --------------------------------------------------------------------------- #


def test_rookiepy_is_bypassed_when_another_profile_holds_the_session(tmp_path) -> None:
    root = make_tree(tmp_path)
    make_profile(root / "Profiles" / "aaa.default-release", [])
    make_profile(
        root / "Profiles" / "bbb.work", [("session", "from-sqlite", ".arena.ai", FUTURE)]
    )
    (root / "profiles.ini").write_text(
        "[Install4F96]\nDefault=Profiles/aaa.default-release\n", encoding="utf-8"
    )
    manager = FirefoxSessionManager(
        profiles_root=root, interactive=False, max_cookie_attempts=1
    )
    calls: list[list[str]] = []

    def firefox(domains):
        calls.append(list(domains))
        return [{"name": "session", "value": "from-default-profile", "domain": ".arena.ai"}]

    manager._rookiepy_module = lambda: type("M", (), {"firefox": staticmethod(firefox)})()
    assert manager.load_cookies() == {"session": "from-sqlite"}
    assert calls == [], "rookiepy would have read the wrong profile"


def test_rookiepy_is_used_when_it_reads_the_selected_profile(tmp_path) -> None:
    root = make_tree(tmp_path)
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [("session", "from-sqlite", ".arena.ai", FUTURE)],
    )
    manager = FirefoxSessionManager(
        profiles_root=root, interactive=False, max_cookie_attempts=1
    )
    manager._rookiepy_module = lambda: type(
        "M",
        (),
        {
            "firefox": staticmethod(
                lambda domains: [
                    {
                        "name": "session",
                        "value": "from-rookiepy",
                        "domain": ".arena.ai",
                        "expires": FUTURE,
                    }
                ]
            )
        },
    )()
    assert manager.load_cookies() == {"session": "from-rookiepy"}


# --------------------------------------------------------------------------- #
# Expiry units
# --------------------------------------------------------------------------- #


def test_microsecond_expiry_is_understood(tmp_path) -> None:
    """Old profile formats store ``expiry`` in microseconds, not seconds."""
    root = make_tree(tmp_path)
    make_profile(
        root / "Profiles" / "aaa.default-release",
        [
            ("future", "kept", ".arena.ai", FUTURE * 1_000_000),
            ("past", "dropped", ".arena.ai", PAST * 1_000_000),
            ("session", "kept", ".arena.ai", 0),
        ],
    )
    manager = FirefoxSessionManager(
        profiles_root=root, interactive=False, max_cookie_attempts=1
    )
    manager._rookiepy_module = lambda: None
    assert manager.load_cookies() == {"future": "kept", "session": "kept"}
    records = {record["name"]: record for record in manager.cookie_records()}
    # Records always report seconds, whatever the store used.
    assert records["future"]["expires"] == pytest.approx(FUTURE, abs=2)
    assert records["session"]["expires"] == 0


def test_expired_cookies_do_not_make_a_profile_the_source(tmp_path) -> None:
    root = make_tree(tmp_path)
    make_profile(
        root / "Profiles" / "aaa.default-release", [("session", "dead", ".arena.ai", PAST)]
    )
    live = make_profile(
        root / "Profiles" / "bbb.work", [("session", "alive", ".arena.ai", FUTURE)]
    )
    manager = FirefoxSessionManager(profiles_root=root, interactive=False)
    manager._rookiepy_module = lambda: None
    assert manager.profile_path() == live
