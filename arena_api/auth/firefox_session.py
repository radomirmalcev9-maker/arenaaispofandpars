"""Session extraction from the user's local Firefox profile.

This module reuses *your own* signed-in Firefox session instead of imitating
a browser or reverse-engineering any protection:

* Cookies are extracted with ``rookiepy`` (a Rust library that reads the
  profile's ``cookies.sqlite``). When rookiepy is missing, the cookie store
  is locked by a running Firefox, or it returns nothing, a dependency-free
  fallback snapshots ``cookies.sqlite`` (plus its ``-wal``/``-shm``
  sidecars) into a private temporary directory and reads it with the
  standard :mod:`sqlite3` module — no lock contention with the browser.
* The default profile is resolved from ``profiles.ini`` (the ``[Install*]``
  section first, then the profile flagged ``Default=1``), falling back to the
  conventional ``*.default-release`` directory name.
* Request headers (:meth:`FirefoxSessionManager.firefox_headers`) are derived
  from the same profile — a Firefox User-Agent for the current OS and the
  installed Firefox version, ``Sec-GPC``, and ``Sec-Fetch-*`` — so the
  client's header fingerprint matches the real browser the session came from.
* :meth:`FirefoxSessionManager.refresh_session` is the hook the clients
  install for HTTP 403 / ``ChallengeRequiredError``: it prints guidance,
  waits for the user to refresh the page in Firefox, and re-reads the
  profile. No challenge is ever solved automatically here.

``load_cookies()`` returns a flat ``{"name": "value"}`` mapping that can be
passed directly to ``curl_cffi.requests.Session`` (or ``httpx.Client``);
:meth:`~FirefoxSessionManager.to_curl_cffi` provides a domain-grouped export
for curl_cffi's domain-aware cookie jar.
"""

from __future__ import annotations

import configparser
import locale
import math
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO, runtime_checkable

__all__ = [
    "DEFAULT_COOKIE_DOMAINS",
    "FIREFOX_PROFILE_ENV",
    "FIREFOX_ROOT_ENV",
    "FirefoxSessionError",
    "FirefoxSessionManager",
    "SessionManager",
    "cookies_from_rookiepy",
    "default_profiles_root",
    "firefox_profiles_roots",
    "read_firefox_cookie_records",
    "read_firefox_cookies",
]


#: Domains queried when the caller does not specify any.
DEFAULT_COOKIE_DOMAINS: tuple[str, ...] = ("arena.ai", "lmarena.ai")

#: Point the reader at a root (or a ``profiles.ini``) it would not guess -- a
#: portable install, a second root, a profile on another disk.
FIREFOX_ROOT_ENV = "ARENA_FIREFOX_ROOT"
#: Name or absolute path of the profile to use, skipping discovery entirely.
FIREFOX_PROFILE_ENV = "ARENA_FIREFOX_PROFILE"

#: Roots tried after the platform default. Distribution packaging puts the real
#: profile inside a sandbox, where ``~/.mozilla/firefox`` does not exist at all;
#: on such a machine every discovery attempt used to end in "not found".
_SANDBOX_PROFILE_ROOTS: dict[str, tuple[str, ...]] = {
    "linux": (
        "~/snap/firefox/common/.mozilla/firefox",
        "~/.var/app/org.mozilla.firefox/.mozilla/firefox",
        "~/snap/firefox/current/.mozilla/firefox",
    ),
}

# Profile directory suffixes tried, in order, when profiles.ini gives no hint.
DEFAULT_PROFILE_HINTS: tuple[str, ...] = ("default-release", "default")

# Used only when the profile's compatibility.ini cannot be read.
FALLBACK_FIREFOX_VERSION = "156.0"

# Firefox froze the Gecko trail version ("rv:") in the User-Agent at 109.0;
# only the trailing "Firefox/<version>" tracks the real release.
FROZEN_GECKO_TRAIL_VERSION = "109.0"

STALE_SESSION_TEMPLATE = (
    "Сессия устарела. Откройте {host} в Firefox, обновите страницу, затем "
    "нажмите Enter..."
)

_LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
    "unable to open database file",
)

_LAST_VERSION_RE = re.compile(r"LastVersion\s*=\s*(\d+(?:\.\d+)*)")
_ACCEPT_LANGUAGES_RE = re.compile(
    r'user_pref\(\s*"intl\.accept_languages"\s*,\s*"([^"]*)"\s*\)'
)

_COOKIE_RECORD_QUERY = """
SELECT host, name, value, path, expiry, isSecure, isHttpOnly
FROM moz_cookies
WHERE (host = :exact OR host = :dotted OR host LIKE :subdomain)
ORDER BY LENGTH(host) DESC, name COLLATE NOCASE
"""


class FirefoxSessionError(RuntimeError):
    """The Firefox profile or its cookie store could not be read."""


@runtime_checkable
class SessionManager(Protocol):
    """Duck-typed interface accepted as ``session_source`` by the clients."""

    def load_cookies(self, domains: Sequence[str] | None = None) -> dict[str, str]:
        """Return a flat ``{name: value}`` cookie mapping."""
        ...

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        """Return browser-consistent request headers."""
        ...

    def refresh_session(
        self, reason: BaseException | None = None
    ) -> dict[str, str] | None:
        """Re-read the session after an auth failure, or return ``None``."""
        ...


def default_profiles_root() -> Path:
    """Return the default Firefox profiles root for the current platform."""
    home = Path.home()
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Mozilla" / "Firefox"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Firefox"
    return home / ".mozilla" / "firefox"


def _platform_profile_roots() -> tuple[str, ...]:
    """Sandboxed roots for this platform (snap/Flatpak packaging)."""
    if sys.platform.startswith("linux"):
        return _SANDBOX_PROFILE_ROOTS.get("linux", ())
    return ()


def firefox_profiles_roots() -> tuple[Path, ...]:
    """Every Firefox profiles root worth searching, most likely first.

    The platform default comes first, then the sandboxed locations a snap or a
    Flatpak install uses (``~/snap/firefox/common/.mozilla/firefox`` and
    ``~/.var/app/org.mozilla.firefox/.mozilla/firefox`` on Linux), because on
    those distributions the conventional ``~/.mozilla/firefox`` does not exist.

    ``ARENA_FIREFOX_ROOT`` -- a profiles directory, or the ``profiles.ini``
    inside one -- is prepended, which covers a portable install or a copy of a
    profile restored somewhere else.

    Roots that do not exist are included: callers report them, and a report
    that names the paths it searched is what a stuck user needs.
    """
    roots: list[Path] = []
    override = os.environ.get(FIREFOX_ROOT_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        roots.append(
            candidate.parent if candidate.name == "profiles.ini" else candidate
        )
    roots.append(default_profiles_root())
    roots.extend(Path(item).expanduser() for item in _platform_profile_roots())
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return tuple(unique)


def _normalize_domains(domains: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in domains:
        host = str(item).strip().lstrip(".").lower()
        if host and host not in normalized:
            normalized.append(host)
    if not normalized:
        raise ValueError("at least one cookie domain is required")
    return tuple(normalized)


def _as_profiles_root(path: Path) -> Path:
    """Accept either a profiles directory or the ``profiles.ini`` inside one."""
    return path.parent if path.name == "profiles.ini" else path


def _read_profiles_ini(root: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(root / "profiles.ini", encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return parser


def _candidate_profile_dirs(root: Path) -> list[Path]:
    """List profile-shaped directories under ``root`` (and ``Profiles/``)."""
    candidates: list[Path] = []
    for base in (root, root / "Profiles"):
        if not base.is_dir():
            continue
        try:
            candidates.extend(
                sorted(entry for entry in base.iterdir() if entry.is_dir())
            )
        except OSError:
            continue
    return candidates


def _path_from_ini_value(root: Path, value: str) -> Path:
    path = Path(value.strip())
    return path if path.is_absolute() else root / path


def _looks_locked(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _LOCK_MARKERS)


def _expiry_seconds(expires: Any) -> float | None:
    """Normalize a cookie expiry to epoch **seconds**; ``None`` = session cookie.

    Firefox stores ``moz_cookies.expiry`` in seconds, but older profile
    formats (and some exports) carry microseconds, and ``rookiepy`` hands back
    seconds as a float. Mixing the units up silently makes an expiring cookie
    look immortal or an immortal one look long expired, so every value goes
    through here exactly once.
    """
    if expires is None or isinstance(expires, bool):
        return None
    if isinstance(expires, (int, float)):
        number = float(expires)
    elif isinstance(expires, str):
        text = expires.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None
    if number <= 0:
        return None
    # Microseconds since epoch (~1.7e15 today) rather than seconds (~1.7e9).
    if number > 1e12:
        number /= 1_000_000
    return number


def _cookie_expired(expires: Any) -> bool:
    """Whether an expiry value is in the past; ``0``/``None`` means "session"."""
    moment = _expiry_seconds(expires)
    return moment is not None and moment < time.time()


def _normalize_rookiepy_records(records: Any) -> list[dict[str, Any]]:
    """Validate rookiepy's cookie list and drop unusable/expired entries."""
    normalized: list[dict[str, Any]] = []
    if not isinstance(records, (list, tuple)):
        return normalized
    for record in records:
        if not isinstance(record, Mapping):
            continue
        name = record.get("name")
        value = record.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        if _cookie_expired(record.get("expires")):
            continue
        moment = _expiry_seconds(record.get("expires"))
        normalized.append(
            {
                "name": name,
                "value": value,
                "domain": str(record.get("domain") or ""),
                "path": str(record.get("path") or "/"),
                "expires": int(moment) if moment is not None else 0,
                "secure": bool(record.get("secure")),
                "http_only": bool(record.get("http_only")),
            }
        )
    return normalized


def _records_to_cookies(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Flatten cookie records into ``{name: value}``; first record of a name wins."""
    result: dict[str, str] = {}
    for record in records:
        result.setdefault(str(record["name"]), str(record["value"]))
    return result


def cookies_from_rookiepy(records: Any) -> dict[str, str]:
    """Convert a rookiepy cookie record list into a flat ``{name: value}`` dict."""
    return _records_to_cookies(_normalize_rookiepy_records(records))


def _query_cookie_snapshot(
    snapshot: Path, domains: Sequence[str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    connection = sqlite3.connect(str(snapshot))
    try:
        connection.row_factory = sqlite3.Row
        now = time.time()
        for domain in domains:
            rows = connection.execute(
                _COOKIE_RECORD_QUERY,
                {
                    "exact": domain,
                    "dotted": f".{domain}",
                    "subdomain": f"%.{domain}",
                },
            ).fetchall()
            for row in rows:
                name = row["name"]
                value = row["value"]
                if not name or value is None:
                    continue
                moment = _expiry_seconds(row["expiry"])
                if moment is not None and moment < now:
                    continue
                records.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": row["host"],
                        "path": row["path"],
                        "expires": int(moment) if moment is not None else 0,
                        "secure": bool(row["isSecure"]),
                        "http_only": bool(row["isHttpOnly"]),
                    }
                )
    finally:
        connection.close()
    return records


def read_firefox_cookie_records(
    database: Path, domains: Sequence[str]
) -> list[dict[str, Any]]:
    """Read full cookie records from a Firefox ``cookies.sqlite`` safely.

    Firefox keeps the database in WAL mode and may hold it open while running.
    The database and its ``-wal``/``-shm`` sidecars are copied to a private
    temporary directory first, so SQLite reads a consistent snapshot without
    contending for locks with a running browser. Cookie values in
    ``cookies.sqlite`` are stored in plain text; no key material is needed.
    """
    database = Path(database)
    targets = _normalize_domains(domains)
    with tempfile.TemporaryDirectory(prefix="arena-api-firefox-") as temporary:
        snapshot = Path(temporary) / database.name
        shutil.copy2(database, snapshot)
        for suffix in ("-wal", "-shm"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.is_file():
                shutil.copy2(
                    sidecar, snapshot.with_name(snapshot.name + suffix)
                )
        return _query_cookie_snapshot(snapshot, targets)


def read_firefox_cookies(database: Path, domains: Sequence[str]) -> dict[str, str]:
    """Lock-safe flat ``{name: value}`` read of a Firefox cookie database."""
    return _records_to_cookies(read_firefox_cookie_records(database, domains))


def _platform_token() -> str:
    """Return the Firefox User-Agent platform token for the current OS."""
    if sys.platform.startswith("win"):
        return "Windows NT 10.0; Win64; x64"
    if sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10.15"
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"X11; Linux {arch}"


def _system_locale_tag() -> str | None:
    try:
        current = locale.getlocale()
    except ValueError:
        current = None
    if current and current[0]:
        return current[0].replace("_", "-")
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(variable)
        if value and value.split(".")[0] not in ("", "C", "POSIX"):
            return value.split(".")[0].split(":")[0].replace("_", "-")
    return None


def _normalize_accept_language(value: str) -> str:
    """Turn a locale tag or preference list into a Firefox Accept-Language."""
    text = " ".join(value.split())
    if "," in text:
        return text
    tag = text.replace("_", "-")
    language = tag.split("-")[0].casefold()
    if language == "en":
        return "en-US,en;q=0.5"
    return f"{tag},{language};q=0.8,en-US;q=0.5,en;q=0.3"


class FirefoxSessionManager:
    """Load, validate, and refresh an Arena session from local Firefox.

    Parameters
    ----------
    domains:
        Cookie domains to extract. Defaults to ``("arena.ai", "lmarena.ai")``.
    profiles_root:
        Override the Firefox profiles directory (portable installs, tests).
        ``None`` searches every root of
        :func:`firefox_profiles_roots` — the platform default *and* the
        snap/Flatpak locations — and honours ``ARENA_FIREFOX_ROOT``.
    profile_name:
        Explicit profile directory name or absolute path. When omitted, every
        profile of every root is ranked by ``profiles.ini`` (the ``[Install*]``
        section's ``Default`` entry, then the profile flagged ``Default=1``,
        then ``*.default-release``/``*.default`` names, then the newest
        ``cookies.sqlite``) and the first one that actually stores cookies for
        the requested domains wins — with several profiles, the configured
        default is often not the one holding the session. Honours
        ``ARENA_FIREFOX_PROFILE``.
    cookie_database:
        Explicit ``cookies.sqlite`` path, bypassing profile resolution for
        the cookie store itself.
    interactive:
        When ``True`` (default), :meth:`refresh_session` waits for the user
        to press Enter after the stale-session message. Set to ``False`` for
        headless use — cookies are then re-read immediately.
    prompt:
        Callable used to wait for user input (injectable for tests).
    output:
        Stream guidance messages are printed to (defaults to ``sys.stderr``).
    language:
        Explicit ``Accept-Language`` value; otherwise the profile's
        ``intl.accept_languages`` preference or the system locale is used.
    firefox_version:
        Override the Firefox version used in the User-Agent. Otherwise it is
        read from the profile's ``compatibility.ini``.
    max_cookie_attempts:
        Attempts per extraction when the cookie store is locked (``>= 1``).
    lock_retry_delay:
        Base delay in seconds between lock retries (grows linearly).
    """

    def __init__(
        self,
        *,
        domains: Sequence[str] | None = None,
        profiles_root: str | Path | None = None,
        profile_name: str | None = None,
        cookie_database: str | Path | None = None,
        interactive: bool = True,
        prompt: Callable[[str], str] = input,
        output: TextIO | None = None,
        language: str | None = None,
        firefox_version: str | None = None,
        max_cookie_attempts: int = 3,
        lock_retry_delay: float = 0.35,
    ) -> None:
        if domains is None:
            self.domains = DEFAULT_COOKIE_DOMAINS
        else:
            self.domains = _normalize_domains(domains)
        self.profiles_root = (
            _as_profiles_root(Path(profiles_root).expanduser())
            if profiles_root is not None
            else None
        )
        # Environment overrides let a portable install, a second root or a
        # non-default profile be used through ``arena-api --status`` -- without
        # editing code -- when nothing on the command line says otherwise.
        if self.profiles_root is None:
            override_root = os.environ.get(FIREFOX_ROOT_ENV, "").strip()
            if override_root:
                self.profiles_root = _as_profiles_root(
                    Path(override_root).expanduser()
                )
        self.profile_name = profile_name or (
            os.environ.get(FIREFOX_PROFILE_ENV, "").strip() or None
        )
        self.cookie_database = (
            Path(cookie_database).expanduser()
            if cookie_database is not None
            else None
        )
        self.interactive = interactive
        self._prompt = prompt
        self._output = output if output is not None else sys.stderr
        self.language = language
        self.firefox_version = firefox_version
        if (
            not isinstance(max_cookie_attempts, int)
            or isinstance(max_cookie_attempts, bool)
            or max_cookie_attempts < 1
        ):
            raise ValueError("max_cookie_attempts must be an integer >= 1")
        if not math.isfinite(lock_retry_delay) or lock_retry_delay < 0:
            raise ValueError(
                "lock_retry_delay must be a finite, non-negative number"
            )
        self.max_cookie_attempts = max_cookie_attempts
        self.lock_retry_delay = lock_retry_delay
        self._profile_path: Path | None = None
        # profile -> number of matching cookies, filled by profile discovery.
        self._profile_counts: dict[Path, int] = {}

    # ------------------------------------------------------------------ #
    # Profile and database discovery
    # ------------------------------------------------------------------ #

    def profile_path(self) -> Path:
        """Return the default Firefox profile directory for this machine."""
        if self._profile_path is None:
            self._profile_path = self._resolve_profile()
        return self._profile_path

    def profiles_roots(self) -> tuple[Path, ...]:
        """Roots this manager searches, most likely first."""
        if self.profiles_root is not None:
            return (Path(self.profiles_root).expanduser(),)
        return firefox_profiles_roots()

    def _root_candidates(self, root: Path) -> list[Path]:
        """Profile directories under one root, most likely first.

        ``profiles.ini`` records the installation's default profile and a
        profile flagged ``Default=1``; anything else is guessed by the usual
        ``*.default-release`` name and then by which cookie database was
        written most recently. All of them are returned rather than the first
        hit -- see :meth:`profile_candidates` for why.
        """
        if not root.is_dir():
            return []
        ordered: list[Path] = []

        def remember(path: Path) -> None:
            if path.is_dir() and path not in ordered:
                ordered.append(path)

        ini = _read_profiles_ini(root)
        # Firefox 67+ records each installation's default profile here.
        for section in ini.sections():
            if not section.startswith("Install"):
                continue
            value = ini.get(section, "Default", fallback=None)
            if value:
                remember(_path_from_ini_value(root, value))
        for section in ini.sections():
            if not section.startswith("Profile"):
                continue
            if ini.getboolean(section, "Default", fallback=False):
                value = ini.get(section, "Path", fallback=None)
                if value:
                    remember(_path_from_ini_value(root, value))
        candidates = _candidate_profile_dirs(root)
        for hint in DEFAULT_PROFILE_HINTS:
            for candidate in candidates:
                if candidate.name.endswith(f".{hint}"):
                    remember(candidate)
        with_database = [
            candidate
            for candidate in candidates
            if (candidate / "cookies.sqlite").is_file()
        ]
        for candidate in sorted(
            with_database,
            key=lambda item: (item / "cookies.sqlite").stat().st_mtime,
            reverse=True,
        ):
            remember(candidate)
        for candidate in candidates:
            # A container directory (``Profiles``, ``Crash Reports``) is not a
            # profile; a real one always carries at least one of these.
            if any(
                (candidate / marker).exists()
                for marker in ("prefs.js", "times.json", "cookies.sqlite")
            ):
                remember(candidate)
        return ordered

    def profile_candidates(self) -> list[Path]:
        """Every profile worth reading, most likely first.

        A machine can hold several profiles, and the one ``profiles.ini``
        calls the default is frequently *not* the one holding the signed-in
        session (a separate profile made with ``about:profiles``, a work
        profile, a leftover from an older install). Discovery therefore keeps
        the whole list and picks by content in :meth:`_resolve_profile`.
        """
        if self.profile_name:
            name = Path(self.profile_name).expanduser()
            if name.is_absolute():
                if name.is_dir():
                    return [name]
                raise FirefoxSessionError(f"Firefox profile not found: {name}")
            found: list[Path] = []
            for root in self.profiles_roots():
                # ``<root>/<name>`` (unusual), ``<root>/Profiles/<name>`` (the
                # layout Firefox creates) and any directory of this root whose
                # name matches -- a profile made with about:profiles is named
                # by the user and only the directory name is predictable.
                for candidate in (
                    root / name,
                    root / "Profiles" / name,
                    *(
                        item
                        for item in self._root_candidates(root)
                        if item.name == name
                    ),
                ):
                    if candidate.is_dir() and candidate not in found:
                        found.append(candidate)
            if not found:
                raise FirefoxSessionError(f"Firefox profile not found: {name}")
            return found
        roots = self.profiles_roots()
        existing = [root for root in roots if root.is_dir()]
        if not existing:
            listed = ", ".join(str(root) for root in roots)
            raise FirefoxSessionError(
                f"Firefox profiles directory not found: {listed}"
            )
        ordered: list[Path] = []
        for root in existing:
            for candidate in self._root_candidates(root):
                if candidate not in ordered:
                    ordered.append(candidate)
        if not ordered:
            listed = ", ".join(str(root) for root in existing)
            raise FirefoxSessionError(
                f"No Firefox profile found under {listed}. Install Firefox, "
                "open arena.ai in it, and sign in."
            )
        return ordered

    def profile_cookie_count(self, profile: Path) -> int:
        """Matching cookies stored by one profile (0 when unreadable/absent).

        Permission to fail quietly is deliberate: this runs during discovery,
        where a locked or half-written database of *one* profile must not stop
        the reader from trying the others.
        """
        cached = self._profile_counts.get(profile)
        if cached is not None:
            return cached
        database = profile / "cookies.sqlite"
        count = 0
        if database.is_file():
            try:
                count = len(read_firefox_cookie_records(database, self.domains))
            except (sqlite3.DatabaseError, OSError, ValueError):
                count = 0
        self._profile_counts[profile] = count
        return count

    def selection_reason(self) -> str:
        """One line explaining which profile was chosen and why."""
        profile = self.profile_path()
        count = self.profile_cookie_count(profile)
        domains = ", ".join(self.domains)
        if count:
            return (
                f"{profile} ({count} cookie names for {domains} -- the "
                "profile that actually holds the session)"
            )
        return f"{profile} (no cookies for {domains})"

    def _resolve_profile(self) -> Path:
        candidates = self.profile_candidates()
        for candidate in candidates:
            if self.profile_cookie_count(candidate):
                if candidate != candidates[0]:
                    self._emit(
                        "[arena-api] using Firefox profile "
                        f"{candidate} (it holds the cookies; "
                        f"{candidates[0].name} does not)"
                    )
                return candidate
        return candidates[0]

    def cookie_database_path(self) -> Path:
        """Return the cookie database of the selected profile."""
        if self.cookie_database is not None:
            database = self.cookie_database
        else:
            database = self.profile_path() / "cookies.sqlite"
        if not database.is_file():
            raise FirefoxSessionError(
                f"Firefox cookie database not found: {database}"
            )
        return database

    # ------------------------------------------------------------------ #
    # Cookie extraction
    # ------------------------------------------------------------------ #

    def load_cookies(
        self, domains: Sequence[str] | None = None
    ) -> dict[str, str]:
        """Return ``{"cookie_name": "value"}`` cookies for the given domains.

        The mapping is ready for direct use as the ``cookies`` argument of
        ``curl_cffi.requests.Session`` (or ``httpx.Client``). The preferred
        extraction path is ``rookiepy.firefox(domains=...)``; the lock-safe
        SQLite snapshot reader is used as a fallback. Cookie names from
        earlier domains win when the same name exists on several domains.
        """
        return _records_to_cookies(self._session_records(domains))

    def cookie_records(
        self, domains: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return full cookie records (domain, path, expiry, flags, ...)."""
        return self._session_records(domains)

    def to_curl_cffi(
        self, domains: Sequence[str] | None = None
    ) -> dict[str, dict[str, str]]:
        """Group cookies by domain for curl_cffi's domain-aware cookie jar.

        The flat :meth:`load_cookies` mapping already works as
        ``curl_cffi.requests.Session(cookies=...)``; this export keeps the
        domain attribution::

            session = curl_cffi.requests.Session(impersonate="firefox")
            for domain, jar in manager.to_curl_cffi().items():
                for name, value in jar.items():
                    session.cookies.set(name, value, domain=domain)
        """
        grouped: dict[str, dict[str, str]] = {}
        for record in self._session_records(domains):
            host = str(record.get("domain") or "").lstrip(".")
            grouped.setdefault(host, {})[record["name"]] = record["value"]
        return grouped

    def _rookiepy_is_reading_the_selected_profile(self) -> bool:
        """Whether ``rookiepy`` would open the very profile that was selected.

        ``rookiepy.firefox()`` always opens the installation's *default*
        profile. When discovery picked a different one -- a work profile, a
        second ``about:profiles`` entry, a leftover install -- asking rookiepy
        returns that other profile's cookies, which is worse than not asking:
        the values look real and are simply the wrong session. In that case the
        snapshot reader is used instead, because it reads the profile that was
        chosen.
        """
        if self.cookie_database is not None:
            return False
        try:
            candidates = self.profile_candidates()
            selected = self.profile_path()
        except (FirefoxSessionError, OSError):
            # Nothing to compare against: let rookiepy have its usual turn.
            return True
        return bool(candidates) and selected == candidates[0]

    def _session_records(
        self, domains: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        targets = (
            _normalize_domains(domains) if domains is not None else self.domains
        )
        warnings: list[str] = []
        if self._rookiepy_is_reading_the_selected_profile():
            records = self._rookiepy_records(targets, warnings)
        else:
            records = None
        if records is None:
            # Reported apart from the notes above: what a caller needs to read
            # is why the store could not be read, not that an optional
            # accelerator was absent or skipped.
            failures: list[str] = []
            records = self._sqlite_records(targets, failures)
            if records is None:
                raise FirefoxSessionError(
                    "; ".join(failures + warnings)
                    or "the Firefox cookie store could not be read"
                )
        return records

    def _rookiepy_module(self) -> Any | None:
        """Import rookiepy lazily so the module stays an optional dependency."""
        try:
            import rookiepy  # optional dependency; mypy config silences the missing stubs
        except ImportError:
            return None
        return rookiepy

    def _rookiepy_records(
        self, domains: tuple[str, ...], warnings: list[str]
    ) -> list[dict[str, Any]] | None:
        rookiepy = self._rookiepy_module()
        if rookiepy is None:
            warnings.append(
                "rookiepy is not installed; reading cookies.sqlite directly "
                "(pip install rookiepy for the supported path)"
            )
            return None
        for attempt in range(self.max_cookie_attempts):
            try:
                records = rookiepy.firefox(domains=list(domains))
            except Exception as exc:  # rookiepy surfaces plain runtime errors
                if _looks_locked(exc) and attempt + 1 < self.max_cookie_attempts:
                    warnings.append(
                        f"rookiepy hit a locked cookie store ({exc}); retrying"
                    )
                    time.sleep(self.lock_retry_delay * (attempt + 1))
                    continue
                warnings.append(f"rookiepy failed: {type(exc).__name__}: {exc}")
                return None
            normalized = _normalize_rookiepy_records(records)
            if normalized:
                return normalized
            warnings.append(
                "rookiepy returned no cookies for: " + ", ".join(domains)
            )
            return None
        return None

    def _sqlite_records(
        self, domains: tuple[str, ...], warnings: list[str]
    ) -> list[dict[str, Any]] | None:
        try:
            database = self.cookie_database_path()
        except FirefoxSessionError as exc:
            warnings.append(str(exc))
            return None
        last_error: Exception | None = None
        for attempt in range(self.max_cookie_attempts):
            try:
                return read_firefox_cookie_records(database, domains)
            except (sqlite3.DatabaseError, OSError) as exc:
                last_error = exc
                if attempt + 1 < self.max_cookie_attempts:
                    warnings.append(
                        f"cookies.sqlite read failed ({exc}); "
                        "retrying with a fresh snapshot"
                    )
                    time.sleep(self.lock_retry_delay * (attempt + 1))
                    continue
        warnings.append(f"could not read {database}: {last_error}")
        return None

    # ------------------------------------------------------------------ #
    # Browser-consistent headers
    # ------------------------------------------------------------------ #

    def _compatibility_files(self) -> list[Path]:
        files: list[Path] = []
        if self.cookie_database is not None:
            files.append(self.cookie_database.parent / "compatibility.ini")
        try:
            files.append(self.profile_path() / "compatibility.ini")
        except FirefoxSessionError:
            pass
        return files

    def _installed_version(self) -> str:
        if self.firefox_version:
            return self.firefox_version
        for candidate in self._compatibility_files():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _LAST_VERSION_RE.search(text)
            if match:
                return match.group(1)
        return FALLBACK_FIREFOX_VERSION

    def user_agent(self) -> str:
        """Return the User-Agent of the Firefox build matching this profile."""
        version = self._installed_version()
        return (
            f"Mozilla/5.0 ({_platform_token()}; "
            f"rv:{FROZEN_GECKO_TRAIL_VERSION}) Gecko/20100101 "
            f"Firefox/{version}"
        )

    def _profile_accept_language(self) -> str | None:
        try:
            profile = self.profile_path()
        except FirefoxSessionError:
            return None
        prefs = profile / "prefs.js"
        if not prefs.is_file():
            return None
        try:
            text = prefs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        match = _ACCEPT_LANGUAGES_RE.search(text)
        return match.group(1) if match else None

    def accept_language(self) -> str:
        """Return a Firefox-style ``Accept-Language`` for this machine."""
        if self.language:
            return _normalize_accept_language(self.language)
        for source in (self._profile_accept_language(), _system_locale_tag()):
            if source:
                return _normalize_accept_language(source)
        return "en-US,en;q=0.5"

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        """Return request headers matching a real Firefox on this machine.

        The default set describes a ``fetch``/XHR call made from the Arena
        site itself; ``navigation=True`` returns document-navigation headers.
        ``Accept-Encoding`` is deliberately omitted so the HTTP backend
        (httpx, curl_cffi) advertises only the codecs it can actually decode.
        """
        headers: dict[str, str] = {
            "User-Agent": self.user_agent(),
            "Accept-Language": self.accept_language(),
            "Sec-GPC": "1",
        }
        if navigation:
            headers.update(
                {
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                }
            )
        else:
            headers.update(
                {
                    "Accept": "text/event-stream, application/json, text/plain, */*",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                }
            )
        return headers

    # ------------------------------------------------------------------ #
    # Stale-session recovery
    # ------------------------------------------------------------------ #

    def refresh_session(
        self, reason: BaseException | None = None
    ) -> dict[str, str] | None:
        """Reload cookies after asking the user to re-verify in Firefox.

        The clients install this as their auth-error hook: it prints the
        stale-session guidance, waits for the user to press Enter (unless
        ``interactive=False``), and re-reads the profile. Returns the fresh
        cookie mapping, or ``None`` when no usable session is available.
        """
        host = self.domains[0] if self.domains else "arena.ai"
        message = STALE_SESSION_TEMPLATE.format(host=host)
        if reason is not None:
            message = f"{message}\nПричина: {reason}"
        self._emit(message)
        if self.interactive:
            try:
                self._prompt("")
            except (EOFError, KeyboardInterrupt):
                return None
        try:
            cookies = self.load_cookies()
        except FirefoxSessionError as exc:
            self._emit(
                f"Не удалось перечитать cookie из Firefox: {exc}"
            )
            return None
        if not cookies:
            self._emit(f"В профиле Firefox нет cookie для {host}.")
            return None
        return cookies

    def _emit(self, message: str) -> None:
        print(message, file=self._output, flush=True)
