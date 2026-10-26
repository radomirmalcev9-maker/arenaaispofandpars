"""Redacted, read-only reports about the local Firefox profile.

Why this exists
---------------
A cookie *value* is a bearer credential: pasting one into a chat, an issue or a
log hands over the account. So when the reader finds nothing it is impossible to
tell from the outside whether the profile is empty, whether the browser was
never signed in, whether the site is served from a different domain, or whether
the library looked in the wrong place.

This module answers those questions without ever printing a value: it reports
hosts, names, paths, flags, expiry dates, the value *length* and a truncated
SHA-256 fingerprint. That is enough to diagnose a real setup -- and little
enough to share safely::

    python -m arena_api --diagnose          # human-readable
    python -m arena_api --diagnose --json   # same data, machine-readable

Nothing here writes to the profile: the cookie database is copied to a private
temporary directory first (Firefox keeps it in WAL mode and may hold it open),
so a running browser is never disturbed.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .firefox_session import (
    DEFAULT_COOKIE_DOMAINS,
    FIREFOX_PROFILE_ENV,
    FIREFOX_ROOT_ENV,
    FirefoxSessionError,
    FirefoxSessionManager,
    _expiry_seconds,
    looks_like_anonymous_cookie,
    looks_like_auth_cookie,
)

__all__ = [
    "MAX_REPORTED_COOKIES",
    "CookieFingerprint",
    "collect_firefox_report",
    "format_firefox_report",
    "report_as_json",
    "report_for_options",
]

#: Cookies detailed per profile. Enough for a real session (a frontend sets a
#: handful) and short enough to stay readable in a bug report.
MAX_REPORTED_COOKIES = 25

_SAME_SITE: dict[int, str] = {0: "none", 1: "lax", 2: "strict"}

#: Environment variables reported as set/not set (values are never printed).
_REPORTED_ENV: tuple[str, ...] = (
    FIREFOX_ROOT_ENV,
    FIREFOX_PROFILE_ENV,
    "ARENA_COOKIE",
    "ARENA_COOKIES_FILE",
    "ARENA_COOKIES",
)


@dataclass(frozen=True)
class CookieFingerprint:
    """One cookie, described without its value."""

    host: str
    name: str
    path: str
    expires: int
    secure: bool
    http_only: bool
    same_site: str
    container: str
    value_length: int
    value_sha256: str
    shape: str
    looks_like_auth: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "name": self.name,
            "path": self.path,
            "expires": (
                datetime.fromtimestamp(self.expires, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if self.expires
                else "session"
            ),
            "secure": self.secure,
            "http_only": self.http_only,
            "same_site": self.same_site,
            "container": self.container,
            "value_length": self.value_length,
            "value_sha256": self.value_sha256,
            "shape": self.shape,
            "looks_like_auth": self.looks_like_auth,
        }

    def describe(self) -> str:
        flags = "".join(
            (
                "S" if self.secure else "-",
                "H" if self.http_only else "-",
            )
        )
        expiry = (
            datetime.fromtimestamp(self.expires, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            if self.expires
            else "session"
        )
        marker = "*" if self.looks_like_auth else " "
        return (
            f"{marker} {self.host:<22} {self.name:<24} {flags} "
            f"{self.same_site:<6} len={self.value_length:<4} "
            f"sha256={self.value_sha256 or '--------'} "
            f"expiry={expiry} shape={self.shape}"
        )


# --------------------------------------------------------------------------- #
# Value shape (never the value)
# --------------------------------------------------------------------------- #


def _fingerprint_value(value: str) -> tuple[int, str, str]:
    """Return ``(length, sha256 prefix, shape)`` for a cookie value."""

    text = str(value or "")
    if not text:
        return 0, "", "empty"
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return len(text), digest, _value_shape(text)


def _value_shape(value: str) -> str:
    if value.count(".") == 2 and all(part for part in value.split(".")):
        return "jwt"
    if len(value) == 36 and value.count("-") == 4:
        return "uuid"
    if value.isdigit():
        return "numeric"
    lowered = value.lower()
    if len(lowered) >= 16 and all(ch in "0123456789abcdef" for ch in lowered):
        return "hex"
    if value.startswith("{") and value.endswith("}"):
        return "json"
    if any(ch in value for ch in "+/="):
        return "base64"
    if all(ch.isalnum() or ch in "-_" for ch in value):
        return "base64url-ish" if len(value) >= 16 else "short"
    return "opaque"


def _looks_like_auth(name: str) -> bool:
    """Session-looking name, or one a signed-out visitor is given."""
    return looks_like_auth_cookie(name) or looks_like_anonymous_cookie(name)


# --------------------------------------------------------------------------- #
# Reading one profile
# --------------------------------------------------------------------------- #


def _domain_matches(host: str, targets: Sequence[str]) -> bool:
    candidate = str(host).lstrip(".").lower()
    for target in targets:
        wanted = str(target).lstrip(".").lower()
        if candidate == wanted or candidate.endswith("." + wanted):
            return True
    return False


@contextmanager
def _snapshot(database: Path) -> Iterator[Path]:
    """Copy ``cookies.sqlite`` and its sidecars so a live Firefox is untouched."""
    with tempfile.TemporaryDirectory(prefix="arena-api-report-") as temporary:
        target = Path(temporary) / database.name
        shutil.copy2(database, target)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, target.with_name(target.name + suffix))
        yield target


def _columns(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute("PRAGMA table_info(moz_cookies)").fetchall()
    ]


def _journal_mode(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.DatabaseError:
        return "unknown"
    return str(row[0]) if row else "unknown"


def _database_overview(database: Path) -> dict[str, Any]:
    """Size, journal mode, row count and schema of one cookie database."""
    overview: dict[str, Any] = {
        "path": str(database),
        "exists": database.is_file(),
        "size": 0,
        "modified": None,
        "journal_mode": None,
        "rows": 0,
        "columns": [],
        "error": None,
    }
    if not database.is_file():
        return overview
    try:
        stat = database.stat()
        overview["size"] = stat.st_size
        overview["modified"] = (
            datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError as exc:  # pragma: no cover - permission edge
        overview["error"] = f"{type(exc).__name__}: {exc}"
        return overview
    try:
        with _snapshot(database) as snapshot:
            connection = sqlite3.connect(str(snapshot))
            try:
                connection.row_factory = sqlite3.Row
                overview["journal_mode"] = _journal_mode(connection)
                overview["columns"] = _columns(connection)
                overview["rows"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM moz_cookies"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        overview["error"] = f"{type(exc).__name__}: {exc}"
    return overview


def _raw_rows(database: Path) -> list[sqlite3.Row]:
    """Every row of the cookie store, whatever columns this version has."""
    with _snapshot(database) as snapshot:
        connection = sqlite3.connect(str(snapshot))
        try:
            connection.row_factory = sqlite3.Row
            available = set(_columns(connection))
            wanted = [
                "host",
                "name",
                "value",
                "path",
                "expiry",
                "isSecure",
                "isHttpOnly",
                "sameSite",
                "originAttributes",
            ]
            selected = [column for column in wanted if column in available]
            rows = connection.execute(
                "SELECT " + ", ".join(selected) + " FROM moz_cookies"
            ).fetchall()
            return list(rows)
        finally:
            connection.close()


def _row(record: Any, key: str, default: Any = None) -> Any:
    """Read one column of a :class:`sqlite3.Row` that may not exist here."""
    return record[key] if key in record.keys() else default


@dataclass(frozen=True)
class ProfileScan:
    """What one profile holds, split into live and expired cookies."""

    matching: list[CookieFingerprint]
    expired: list[CookieFingerprint]
    other_hosts: list[str]
    subdomain_hosts: list[str]
    rows: int


def _fingerprints(database: Path, targets: Sequence[str]) -> ProfileScan:
    """Fingerprints of the cookies this profile stores for the targets.

    Expired rows are returned too, not dropped: "0 cookies, 12 expired" is the
    difference between "you never signed in here" and "you signed in, the
    session died on the 21st". ``subdomain_hosts`` matters because a cookie
    stored for ``beta.arena.ai`` is *not* sent to ``arena.ai`` by any browser: if
    the whole session lives there, the base URL is the thing to change.
    """
    matching: list[CookieFingerprint] = []
    expired: list[CookieFingerprint] = []
    others: set[str] = set()
    subdomains: set[str] = set()
    rows = _raw_rows(database)
    now = time.time()
    for row in rows:
        host = str(_row(row, "host", "") or "")
        name = str(_row(row, "name", "") or "")
        value = _row(row, "value", "") or ""
        if not _domain_matches(host, targets):
            if "arena" in host.lower():
                others.add(host)
            continue
        host_only = host.lstrip(".").lower()
        if any(
            host_only != str(target).lstrip(".").lower()
            and host_only.endswith("." + str(target).lstrip(".").lower())
            for target in targets
        ):
            subdomains.add(host)
        moment = _expiry_seconds(_row(row, "expiry"))
        dead = moment is not None and moment < now
        length, digest, shape = _fingerprint_value(str(value))
        same_site = _row(row, "sameSite", 0)
        (expired if dead else matching).append(
            CookieFingerprint(
                host=host,
                name=name,
                path=str(_row(row, "path", "/") or "/"),
                expires=int(moment) if moment is not None else 0,
                secure=bool(_row(row, "isSecure", 0)),
                http_only=bool(_row(row, "isHttpOnly", 0)),
                same_site=_SAME_SITE.get(
                    int(same_site) if isinstance(same_site, int) else 0, "unset"
                ),
                container=str(_row(row, "originAttributes", "") or ""),
                value_length=length,
                value_sha256=digest,
                shape=shape,
                looks_like_auth=_looks_like_auth(name),
            )
        )
    for group in (matching, expired):
        group.sort(key=lambda item: (not item.looks_like_auth, item.name))
    return ProfileScan(
        matching=matching,
        expired=expired,
        other_hosts=sorted(others),
        subdomain_hosts=sorted(subdomains),
        rows=len(rows),
    )


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python 3.10+
        return "unknown"
    for name in ("arena-api", "arena_api"):
        try:
            return version(name)
        except PackageNotFoundError:
            continue
        except Exception:  # pragma: no cover - defensive
            break
    return "unknown"


def _rookiepy_state() -> str:
    """Whether the optional accelerator is importable (it reads only defaults)."""
    try:
        importlib.import_module("rookiepy")
    except ImportError:
        return "not installed"
    except Exception as exc:  # pragma: no cover - broken install
        return f"import failed ({type(exc).__name__})"
    return "installed"


def collect_firefox_report(
    *,
    domains: Sequence[str] | None = None,
    profiles_root: str | Path | None = None,
    profile_name: str | None = None,
    cookie_database: str | Path | None = None,
) -> dict[str, Any]:
    """Gather a redacted description of what the reader can see.

    The structure is stable enough to paste into an issue: ``profiles`` holds one
    entry per profile considered (with its cookie fingerprints), ``selected``
    says which one discovery would use and why, and ``warnings`` spells out the
    likely causes when nothing was found.
    """
    targets = tuple(domains) if domains else DEFAULT_COOKIE_DOMAINS
    notes = io.StringIO()
    manager = FirefoxSessionManager(
        domains=targets,
        profiles_root=profiles_root,
        profile_name=profile_name,
        cookie_database=cookie_database,
        interactive=False,
        output=notes,
    )
    roots = manager.profiles_roots()
    report: dict[str, Any] = {
        "generated_at": datetime.now(tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "arena_api": _version(),
        "python": sys.version.split()[0],
        "system": {
            "platform": sys.platform,
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "domains": list(targets),
        "environment": {
            name: "set" for name in _REPORTED_ENV if os.environ.get(name, "").strip()
        },
        "rookiepy": _rookiepy_state(),
        "roots": [
            {
                "path": str(root),
                "exists": root.is_dir(),
                "profiles_ini": (root / "profiles.ini").is_file(),
            }
            for root in roots
        ],
        "profiles": [],
        "selected": {"profile": None, "reason": None, "error": None},
        "cookies": {"count": 0, "names": [], "values": "hidden", "error": None},
        "warnings": [],
    }

    candidates: list[Path] = []
    if cookie_database is not None:
        database = Path(cookie_database).expanduser()
        candidates = [database.parent]
    else:
        try:
            candidates = manager.profile_candidates()
        except FirefoxSessionError as exc:
            report["selected"]["error"] = str(exc)
    if len(candidates) > 8:
        report["warnings"].append(
            f"only the first 8 of {len(candidates)} profiles are detailed"
        )
    for profile in candidates[:8]:
        database = profile / "cookies.sqlite"
        entry: dict[str, Any] = {
            "profile": str(profile),
            "cookies_sqlite": _database_overview(database),
            "matching": 0,
            "expired_matching": 0,
            "verdict": None,
            "other_arena_hosts": [],
            "subdomain_hosts": [],
            "cookies": [],
            "expired_cookies": [],
        }
        if database.is_file():
            try:
                scan = _fingerprints(database, targets)
            except (sqlite3.DatabaseError, OSError) as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            else:
                entry["matching"] = len(scan.matching)
                entry["expired_matching"] = len(scan.expired)
                entry["other_arena_hosts"] = scan.other_hosts
                entry["subdomain_hosts"] = scan.subdomain_hosts
                entry["cookies"] = [
                    item.as_dict() for item in scan.matching[:MAX_REPORTED_COOKIES]
                ]
                entry["expired_cookies"] = [
                    item.as_dict() for item in scan.expired[:MAX_REPORTED_COOKIES]
                ]
                if len(scan.matching) > MAX_REPORTED_COOKIES:
                    entry["omitted"] = len(scan.matching) - MAX_REPORTED_COOKIES
                entry["verdict"] = _verdict(scan)
                report["warnings"].extend(_profile_warnings(profile, scan, targets))
        report["profiles"].append(entry)

    try:
        selected = manager.profile_path()
    except FirefoxSessionError as exc:
        report["selected"]["error"] = str(exc)
    else:
        report["selected"]["profile"] = str(selected)
        report["selected"]["reason"] = manager.selection_reason()

    try:
        cookies = manager.load_cookies()
    except FirefoxSessionError as exc:
        report["cookies"]["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - a report must never explode
        report["cookies"]["error"] = f"{type(exc).__name__}: {exc}"
    else:
        report["cookies"]["count"] = len(cookies)
        report["cookies"]["names"] = sorted(cookies)
        report["cookies"]["fingerprints"] = {
            name: _fingerprint_value(value)[1] for name, value in cookies.items()
        }

    captured = notes.getvalue().strip()
    if captured:
        report["notes"] = [
            line for line in captured.splitlines() if line.strip()
        ]
    report["warnings"].extend(_report_warnings(report, targets))
    # A warning can be produced by several profiles; keep it once.
    seen: set[str] = set()
    unique: list[str] = []
    for warning in report["warnings"]:
        if warning and warning not in seen:
            seen.add(warning)
            unique.append(warning)
    report["warnings"] = unique
    return report


def _verdict(scan: ProfileScan) -> str:
    """One sentence a person can act on: did the login survive?

    The cases are distinct and lead to different fixes, which is why they are
    not collapsed into "no session": never signed in, signed in and the session
    expired (with the date), signed in through a visitor cookie, alive.
    """
    live_auth = [
        item.name for item in scan.matching if looks_like_auth_cookie(item.name)
    ]
    dead_auth = [
        item.name for item in scan.expired if looks_like_auth_cookie(item.name)
    ]
    anonymous = [
        item.name
        for item in scan.matching
        if looks_like_anonymous_cookie(item.name)
    ]
    if live_auth:
        return "signed in: " + ", ".join(sorted(live_auth))
    if dead_auth:
        died = max(
            item.expires
            for item in scan.expired
            if looks_like_auth_cookie(item.name) and item.expires
        )
        when = (
            datetime.fromtimestamp(died, tz=timezone.utc).strftime("%Y-%m-%d")
            if died
            else "unknown date"
        )
        return (
            "was signed in, but the session cookie(s) "
            + ", ".join(sorted(dead_auth))
            + f" expired on {when}: sign in to the site again"
        )
    if anonymous:
        return (
            "not signed in: the profile only holds visitor cookie(s) "
            + ", ".join(sorted(anonymous))
        )
    if scan.matching:
        return (
            "no login cookie: only "
            + ", ".join(sorted(item.name for item in scan.matching))
            + " are present"
        )
    if scan.expired:
        return (
            f"no live cookie at all ({len(scan.expired)} expired, oldest "
            f"{_oldest(scan.expired)}): the browser must visit the site again"
        )
    return "no cookie for these domains: the site was never opened in this profile"


def _oldest(items: Sequence[CookieFingerprint]) -> str:
    stamps = [item.expires for item in items if item.expires]
    if not stamps:
        return "session"
    return datetime.fromtimestamp(min(stamps), tz=timezone.utc).strftime("%Y-%m-%d")


def _profile_warnings(
    profile: Path, scan: ProfileScan, targets: Sequence[str]
) -> list[str]:
    warnings: list[str] = []
    name = profile.name
    if scan.expired:
        auth = [item for item in scan.expired if looks_like_auth_cookie(item.name)]
        detail = ""
        if auth:
            detail = " (session cookie(s) among them: " + ", ".join(
                sorted(item.name for item in auth)
            ) + ")"
        warnings.append(
            f"{name}: {len(scan.expired)} cookie(s) for "
            f"{', '.join(targets)} expired and were skipped{detail}"
        )
    if scan.other_hosts:
        warnings.append(
            f"{name}: cookies exist for other Arena hosts "
            f"({', '.join(scan.other_hosts)}) but not for {', '.join(targets)} -- "
            "is the site served from a different domain? use --base-url to match it"
        )
    if scan.subdomain_hosts:
        readable = scan.subdomain_hosts[0].lstrip(".")
        warnings.append(
            f"{name}: cookies are stored for the subdomain "
            f"{', '.join(scan.subdomain_hosts)}; a browser would not send them to "
            f"{', '.join(targets)}. If the site is served from there, pass "
            f"--base-url https://{readable}"
        )
    if scan.matching and not any(
        looks_like_auth_cookie(item.name) for item in scan.matching
    ):
        warnings.append(
            f"{name}: {len(scan.matching)} live cookie(s) match, but none of their "
            "names looks like a login cookie -- the account is not signed in "
            "in this profile"
        )
    empty = [item.name for item in scan.matching if item.shape == "empty"]
    if empty:
        warnings.append(
            f"{name}: cookie(s) with an empty value: {', '.join(sorted(set(empty)))}"
        )
    return warnings


def _report_warnings(
    report: Mapping[str, Any], targets: Sequence[str]
) -> list[str]:
    warnings: list[str] = []
    roots = list(report.get("roots") or [])
    if roots and not any(root.get("exists") for root in roots):
        warnings.append(
            "no Firefox profiles directory exists: looked in "
            + ", ".join(str(root["path"]) for root in roots)
        )
    profiles = list(report.get("profiles") or [])
    if profiles and not any(
        str(entry.get("verdict") or "").startswith("signed in")
        for entry in profiles
    ):
        verdicts = [
            f"  - {entry.get('profile')}: {entry.get('verdict')}"
            for entry in profiles
            if entry.get("verdict")
        ]
        if verdicts:
            warnings.append(
                "no profile holds a live login cookie:"
            )
            warnings.extend(verdicts)
    if profiles and not any(entry.get("matching") for entry in profiles):
        warnings.append(
            f"none of the {len(profiles)} profile(s) holds a cookie for "
            f"{', '.join(targets)}"
        )
        hosts = sorted(
            {
                host
                for entry in profiles
                for host in entry.get("other_arena_hosts") or []
            }
        )
        if hosts:
            warnings.append(
                "closest hosts seen in those profiles: " + ", ".join(hosts)
            )
    cookies = report.get("cookies") or {}
    if cookies.get("error"):
        warnings.append(f"reading the session failed: {cookies['error']}")
    elif cookies.get("count"):
        names = ", ".join(cookies.get("names") or [])
        warnings.append(
            f"session read successfully: {cookies['count']} cookie name(s) -- {names}"
        )
    return warnings


def format_firefox_report(report: Mapping[str, Any]) -> list[str]:
    """Render :func:`collect_firefox_report` as readable lines."""
    lines: list[str] = []

    def line(label: str, value: Any) -> None:
        lines.append(f"{label:<20}: {value}")

    def section(title: str) -> None:
        lines.append("")
        lines.append(f"-- {title} " + "-" * max(0, 60 - len(title)))

    line("arena-api", report.get("arena_api", "unknown"))
    system = report.get("system") or {}
    line(
        "system",
        f"{system.get('platform', '?')} {system.get('release', '')} "
        f"({system.get('machine', '?')}), python {report.get('python', '?')}",
    )
    line("cookie domains", ", ".join(report.get("domains") or []))
    line("rookiepy", report.get("rookiepy", "unknown"))
    environment = report.get("environment") or {}
    line(
        "env overrides",
        ", ".join(sorted(environment)) if environment else "none",
    )

    section("profiles")
    roots = report.get("roots") or []
    line("roots searched", len(roots))
    for root in roots:
        flags = []
        if root.get("exists"):
            flags.append("exists")
        if root.get("profiles_ini"):
            flags.append("profiles.ini")
        line("  root", f"{root.get('path')} [{' '.join(flags) or 'missing'}]")

    profiles = report.get("profiles") or []
    selected = (report.get("selected") or {}).get("profile")
    for index, entry in enumerate(profiles, start=1):
        marker = " (selected)" if str(entry.get("profile")) == str(selected) else ""
        line(f"profile {index}", f"{entry.get('profile')}{marker}")
        database = entry.get("cookies_sqlite") or {}
        if not database.get("exists"):
            line("  cookies.sqlite", "missing")
            continue
        line(
            "  cookies.sqlite",
            f"{database.get('size', 0)} bytes, "
            f"journal {database.get('journal_mode')}, "
            f"{database.get('rows', 0)} row(s), "
            f"modified {database.get('modified')}",
        )
        if database.get("error"):
            line("  read error", database["error"])
        line(
            "  live cookies",
            f"{entry.get('matching', 0)} "
            f"({entry.get('expired_matching', 0)} expired, kept aside)",
        )
        for cookie in entry.get("cookies") or []:
            lines.append(
                "    "
                + f"{'*' if cookie.get('looks_like_auth') else ' '} "
                + f"{cookie.get('host', ''):<22} {cookie.get('name', ''):<24} "
                + f"len={cookie.get('value_length', 0):<5} "
                + f"sha256={cookie.get('value_sha256') or '--------'} "
                + f"expiry={cookie.get('expires')} "
                + f"{'secure' if cookie.get('secure') else 'plain'}"
                + f"{' httpOnly' if cookie.get('http_only') else ''}"
            )
        if entry.get("omitted"):
            lines.append(f"    ... and {entry['omitted']} more")
        # Expired rows are the interesting ones when a login "disappeared":
        # their dates say whether the session died before or after the last
        # sign-in, which points at completely different fixes.
        for cookie in entry.get("expired_cookies") or []:
            lines.append(
                "    "
                + "- "
                + f"{cookie.get('host', ''):<22} {cookie.get('name', ''):<24} "
                + f"EXPIRED {cookie.get('expires')} "
                + f"len={cookie.get('value_length', 0)}"
            )
        if entry.get("verdict"):
            line("  verdict", entry["verdict"])
        if entry.get("other_arena_hosts"):
            line("  other hosts", ", ".join(entry["other_arena_hosts"]))
        if entry.get("subdomain_hosts"):
            line("  subdomain hosts", ", ".join(entry["subdomain_hosts"]))

    section("session the library would use")
    line("profile", selected or "-")
    line("selection", (report.get("selected") or {}).get("reason") or "-")
    cookies = report.get("cookies") or {}
    if cookies.get("error"):
        line("cookies", f"ERROR {cookies['error']}")
    else:
        line(
            "cookies",
            f"{cookies.get('count', 0)} name(s): "
            + (", ".join(cookies.get("names") or []) or "-"),
        )
        line("values", "always hidden (a cookie is a credential)")

    notes = report.get("notes") or []
    if notes:
        section("notes")
        lines.extend(f"  {note}" for note in notes)

    section("warnings")
    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(f"  ! {warning}" for warning in warnings)
    else:
        lines.append("  none -- the session was found and read")
    return lines


def report_as_json(report: Mapping[str, Any]) -> str:
    """Serialize a report for a machine-readable paste."""
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)


def report_for_options(options: Any) -> dict[str, Any]:
    """Collect a report from an :class:`arena_api.auto.AutoOptions`.

    Kept here rather than in :mod:`arena_api.auto` so the CLI stays a thin
    wrapper and the report is usable from a script of your own.
    """
    return collect_firefox_report(
        domains=options.cookie_domains(),
        profiles_root=options.profiles_root,
        profile_name=options.profile_name,
        cookie_database=options.cookie_database,
    )
