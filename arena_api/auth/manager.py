"""Session lifecycle: normalized cookie jars, persistence, liveness, refresh.

This is the module that answers "my automated test lost its login, now what"
without answering "how do I make the protection stop noticing me".

What it does
------------
* Normalizes cookie sources into one :class:`CookieJar`: a local Firefox
  profile (via :class:`~arena_api.auth.firefox_session.FirefoxSessionManager`),
  a Playwright ``storage_state`` file, an in-memory mapping, or your own
  callable.
* Persists that jar to a JSON file with owner-only permissions and an atomic
  replace, in a layout that doubles as a Playwright ``storage_state`` so a
  browser context can be seeded from the same file.
* Classifies a session as ``alive`` / ``expired`` / ``challenge`` /
  ``unavailable`` (:meth:`AutoSessionManager.check`) using a plain HTTP probe.
* Refreshes it under an explicit :class:`RefreshPolicy`: throttling, bounded
  attempts, exponential backoff with optional jitter, TTL-based re-reads, and
  an optional background thread.
* When the origin itself insists on a human verification, an optional
  :class:`SessionCheckpoint` (see :mod:`arena_api.auth.verify`) hands the
  browser to *you*, and the manager re-reads the session afterwards.

What it deliberately does not do
--------------------------------
No challenge detection-and-clicking, no CAPTCHA/Turnstile/DataDome solving or
token replay, no audio-speech solver, no fingerprint patching
(``navigator.webdriver``, ``chrome.runtime``, WebGL/canvas/audio, permissions,
screen metrics), no fabricated or randomized User-Agent. Those techniques
exist to defeat an access control a service is actively applying, and they
contradict this library's stated contract
(:class:`~arena_api.exceptions.ChallengeRequiredError`, README). To get
CAPTCHA-free automated tests, exempt the test environment by *configuration*
(official test keys / vendor allow-lists) — see
``docs/automated-tests-and-captcha.md``.

Nothing in this module performs network I/O unless you pass a ``probe``.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..exceptions import AuthenticationError, ChallengeRequiredError
from .firefox_session import SessionManager

__all__ = [
    "AutoCookieManager",
    "AutoSessionManager",
    "CallableSource",
    "CookieJar",
    "CookieRecord",
    "FirefoxSource",
    "HttpLivenessProbe",
    "PlaywrightStorageSource",
    "ProbeOutcome",
    "RefreshPolicy",
    "SessionCheckpoint",
    "SessionSource",
    "SessionStore",
    "SessionStoreError",
    "StaticSource",
    "looks_like_challenge",
]


#: Raised whenever a manual check is required and nothing can wait it out.
_CHALLENGE_HINT = (
    "The service requires an interactive browser verification. Complete it in a "
    "normal browser (or run arena_api.auth.verify.capture_storage_state) and "
    "refresh the session; arena-api does not bypass challenges."
)

#: Values returned by :attr:`ProbeOutcome.state`.
ProbeState = Literal["alive", "expired", "challenge", "unavailable"]

#: Markers of a "prove you are human" interstitial. Used *only* to classify a
#: session and stop with a clear error; nothing here acts on them further.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-chl-",
    "challenge-platform",
    "turnstile",
    "cf-error-details",
    "just a moment...",
    "captcha-delivery",
    "recaptcha",
    "datadome",
    "px-captcha",
    "perimeterx",
    "geo.captcha-delivery.com",
    "enable javascript and cookies to continue",
)

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _now() -> float:
    return time.time()


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat(timespec="seconds")


def _coerce_expiry(value: Any) -> float | None:
    """Best-effort conversion of the many expiry encodings to epoch seconds.

    Handles Firefox/Playwright epoch seconds, ``-1`` for session cookies,
    ISO-8601 strings, and ``None``/absent.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp <= 0:
            return None
        # Firefox stores microseconds in some builds; 10-digit cutoff is
        # somewhere around 2286 for seconds and 1970 for microseconds.
        if stamp > 1e15:
            stamp /= 1_000_000.0
        return stamp
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _coerce_expiry(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _normalize_host(value: Any, default: str = "") -> str:
    """Lower-case and drop the leading dot of a cookie domain.

    ``www.`` is deliberately *not* stripped: a cookie scoped to
    ``www.example.com`` must never be sent to ``api.example.com``, so scope is
    preserved exactly as stored.
    """
    return str(value or default).strip().lstrip(".").lower()


def _domain_matches(cookie_domain: str, target: str) -> bool:
    """Domain-match a cookie against a host (``.example.com`` covers subdomains)."""
    cookie = _normalize_host(cookie_domain)
    host = _normalize_host(target)
    if not cookie or not host:
        return not cookie and not host
    return host == cookie or host.endswith(f".{cookie}")


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str:
    if not headers:
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def _sample_body(response: Any, limit: int = 65_536) -> str:
    """Read just enough of a response body to tell HTML apart from JSON.

    A verification interstitial is HTML, and it can be served with a success
    status, so the probe has to look. To keep that cheap on API endpoints, only
    the first few hundred bytes are inspected before reading further.
    """
    try:
        head = response.read(512) or b""
    except Exception:  # noqa: BLE001 - classification only, never fatal
        return ""
    if isinstance(head, str):
        head = head.encode("utf-8", "replace")
    if not head:
        return ""
    if b"<" not in head:
        return head.decode("utf-8", "replace")
    chunks = [head]
    total = len(head)
    while total < limit:
        try:
            part = response.read(min(8_192, limit - total)) or b""
        except Exception:  # noqa: BLE001
            break
        if not part:
            break
        if isinstance(part, str):
            part = part.encode("utf-8", "replace")
        chunks.append(part)
        total += len(part)
    return b"".join(chunks).decode("utf-8", "replace")


def looks_like_challenge(
    text: str | bytes | None, headers: Mapping[str, Any] | None = None
) -> bool:
    """Return ``True`` when a response body/headers look like a bot interstitial.

    Detection here exists for one reason: to abort with
    :class:`~arena_api.exceptions.ChallengeRequiredError` instead of silently
    feeding a challenge HTML page to a JSON parser.
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    body = (text or "").lower()
    if any(marker in body for marker in CHALLENGE_MARKERS):
        return True
    mitigated = _header_value(headers, "cf-mitigated").lower()
    if mitigated.startswith("challenge"):
        return True
    server = _header_value(headers, "server").lower()
    content_type = _header_value(headers, "content-type").lower()
    # A bare HTML answer from the CDN, rather than JSON from the origin, is the
    # classic interstitial shape.
    return "cloudflare" in server and "text/html" in content_type


# --------------------------------------------------------------------------- #
# Cookie jar
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CookieRecord:
    """A single cookie plus the origin metadata needed to reuse it correctly."""

    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float | None = None
    secure: bool = False
    http_only: bool = False
    same_site: str | None = None
    source: str = "manual"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("cookie name must not be empty")
        self.value = "" if self.value is None else str(self.value)
        self.domain = _normalize_host(self.domain)
        self.path = self.path or "/"
        if not self.path.startswith("/"):
            self.path = f"/{self.path}"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.domain, self.path, self.name)

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires is None:
            return False
        return self.expires <= (now if now is not None else _now())

    def matches_domain(self, domain: str) -> bool:
        return _domain_matches(self.domain, domain)

    def covers_secure(self, scheme: str) -> bool:
        return not self.secure or scheme.lower() in {"https", "wss"}

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def from_any(cls, payload: Mapping[str, Any], *, source: str = "manual") -> CookieRecord:
        """Build a record from any of the shapes callers use.

        Accepts Firefox-style keys (``host``, ``expiry``, ``isSecure``),
        Playwright-style keys (``domain``, ``expires``, ``httpOnly``) and
        naive ``{"name": "value"}`` pairs.
        """
        if not isinstance(payload, Mapping):
            raise TypeError(f"cookie payload must be a mapping, got {type(payload).__name__}")
        name = payload.get("name")
        value = payload.get("value")
        if not isinstance(name, str) or not name:
            raise ValueError("cookie payload is missing a non-empty 'name'")
        domain = payload.get("domain")
        if domain is None:
            domain = payload.get("host")
        expires = payload.get("expires")
        if expires is None:
            expires = payload.get("expiry")
        secure = payload.get("secure")
        if secure is None:
            secure = payload.get("isSecure", payload.get("is_secure"))
        http_only = payload.get("httpOnly")
        if http_only is None:
            http_only = payload.get("http_only", payload.get("isHttpOnly"))
        same_site = payload.get("sameSite")
        if same_site is None:
            same_site = payload.get("same_site")
        if isinstance(same_site, int):
            same_site = {1: "Lax", 2: "Strict", 4: "None", 0: None}.get(same_site)
        return cls(
            name=name,
            value="" if value is None else str(value),
            domain=str(domain or ""),
            path=str(payload.get("path") or "/"),
            expires=_coerce_expiry(expires),
            secure=bool(secure),
            http_only=bool(http_only),
            same_site=None if same_site is None else str(same_site).title(),
            source=str(payload.get("source") or source),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize for :class:`SessionStore` (also valid Playwright storage)."""
        payload: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            # Playwright uses -1 for "session cookie, not persisted".
            "expires": -1.0 if self.expires is None else float(self.expires),
            "httpOnly": bool(self.http_only),
            "secure": bool(self.secure),
        }
        if self.same_site:
            payload["sameSite"] = self.same_site
        if self.source:
            payload["source"] = self.source
        return payload


class CookieJar:
    """An ordered, de-duplicated set of :class:`CookieRecord`.

    Keyed by ``(domain, path, name)`` so the same cookie name can legitimately
    exist on several hosts/paths, while :meth:`flat` keeps the naive
    ``{"name": value}`` view the HTTP clients expect (first value of a name wins,
    matching :mod:`arena_api.auth.firefox_session`).
    """

    __slots__ = ("_records",)

    def __init__(self, records: Iterable[Any] | Mapping[str, str] | None = None) -> None:
        self._records: dict[tuple[str, str, str], CookieRecord] = {}
        for record in _iter_records(records):
            self.add(record)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_mapping(cls, cookies: Mapping[str, Any], *, domain: str = "") -> CookieJar:
        jar = cls()
        for name, value in (cookies or {}).items():
            jar.add(CookieRecord(name=str(name), value=str(value), domain=domain))
        return jar

    @classmethod
    def from_storage_state(cls, payload: Mapping[str, Any]) -> CookieJar:
        """Read a Playwright ``storage_state`` mapping (or list of cookies)."""
        if isinstance(payload, Mapping):
            raw = payload.get("cookies", [])
        else:
            raw = payload
        jar = cls()
        for item in raw or []:
            try:
                jar.add(CookieRecord.from_any(item, source="storage_state"))
            except (TypeError, ValueError):
                continue
        return jar

    @classmethod
    def from_firefox_records(
        cls, records: Iterable[Mapping[str, Any]]
    ) -> CookieJar:
        jar = cls()
        for item in records or []:
            try:
                jar.add(CookieRecord.from_any(item, source="firefox"))
            except (TypeError, ValueError):
                continue
        return jar

    # -- mutation ----------------------------------------------------------- #

    def add(self, record: Any) -> CookieRecord | None:
        if record is None:
            return None
        if not isinstance(record, CookieRecord):
            record = CookieRecord.from_any(record)
        self._records[record.key] = record
        return record

    def update(self, other: CookieJar | Iterable[Any] | None) -> CookieJar:
        """Merge ``other`` into this jar; same-key records are overwritten."""
        if other is None:
            return self
        if isinstance(other, CookieJar):
            for record in other:
                self.add(record)
            return self
        for record in _iter_records(other):
            self.add(record)
        return self

    def merged_with(self, other: CookieJar) -> CookieJar:
        clone = CookieJar()
        clone.update(self).update(other)
        return clone

    def prune_expired(self, now: float | None = None) -> int:
        """Drop expired records; return how many were removed."""
        moment = now if now is not None else _now()
        stale = [key for key, record in self._records.items() if record.is_expired(moment)]
        for key in stale:
            del self._records[key]
        return len(stale)

    def remove(self, name: str) -> int:
        keys = [key for key in self._records if key[2] == name]
        for key in keys:
            del self._records[key]
        return len(keys)

    def clear(self) -> None:
        self._records.clear()

    # -- queries ------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._records)

    def __bool__(self) -> bool:
        return bool(self._records)

    def __iter__(self) -> Iterator[CookieRecord]:
        return iter(list(self._records.values()))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        names = ", ".join(sorted({key[2] for key in self._records})[:8])
        return f"<CookieJar {len(self)} cookies: {names}>"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(key[2] for key in self._records))

    def domains(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(key[0] for key in self._records if key[0]))

    def get(self, name: str, domain: str | None = None) -> CookieRecord | None:
        for key, record in self._records.items():
            if key[2] != name:
                continue
            if domain is None or _domain_matches(record.domain, domain):
                return record
        return None

    def value(self, name: str, domain: str | None = None) -> str | None:
        record = self.get(name, domain)
        return None if record is None else record.value

    def for_domain(self, domain: str) -> list[CookieRecord]:
        return [record for record in self if record.matches_domain(domain)]

    def flat(self, domain: str | None = None, *, now: float | None = None) -> dict[str, str]:
        """Return ``{name: value}``, skipping expired cookies.

        When one name exists at several scopes, the most specific domain (then
        the longest path) wins — the same precedence a browser applies when it
        builds a ``Cookie`` header, and the reason a jar keyed by
        ``(domain, path, name)`` is kept instead of a plain dict.
        """
        moment = now if now is not None else _now()
        result: dict[str, str] = {}
        ordered = sorted(
            self._records.values(),
            key=lambda record: (len(record.domain), len(record.path)),
            reverse=True,
        )
        for record in ordered:
            if record.is_expired(moment):
                continue
            if domain is not None and not record.matches_domain(domain):
                continue
            result.setdefault(record.name, record.value)
        return result

    def cookie_header(self, domain: str, *, scheme: str = "https") -> str:
        """Build the ``Cookie:`` request header for ``domain``."""
        pairs: list[str] = []
        seen: set[str] = set()
        # Longest (most specific) path first, like a browser does.
        records = sorted(
            (
                record
                for record in self.for_domain(domain)
                if record.covers_secure(scheme) and not record.is_expired()
            ),
            key=lambda record: len(record.path),
            reverse=True,
        )
        for record in records:
            if record.name in seen:
                continue
            seen.add(record.name)
            pairs.append(f"{record.name}={record.value}")
        return "; ".join(pairs)

    def has_any(self, names: Sequence[str], domain: str | None = None) -> bool:
        wanted = {str(name) for name in names}
        return any(name in wanted for name in self.flat(domain))

    def earliest_expiry(self) -> float | None:
        stamps = [record.expires for record in self if record.expires is not None]
        return min(stamps) if stamps else None

    def is_expired(self, now: float | None = None, *, margin: float = 0.0) -> bool:
        """``True`` when the jar is non-empty and *every* cookie has expired.

        Session cookies (no expiry) keep the jar alive, which is what browsers
        would do too.
        """
        if not self._records:
            return True
        moment = (now if now is not None else _now()) + margin
        return all(
            record.expires is not None and record.expires <= moment
            for record in self._records.values()
        )

    # -- exports ------------------------------------------------------------ #

    def to_storage_state(self, *, origins: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Serialize as a Playwright ``storage_state`` document.

        Extra metadata lives under the ``arena_api`` key, which Playwright
        ignores.
        """
        return {
            "cookies": [record.as_dict() for record in self],
            "origins": list(origins or []),
            "arena_api": {
                "version": 1,
                "saved_at": _iso(_now()),
                "domains": list(self.domains()),
            },
        }


def _iter_records(records: Any) -> Iterator[CookieRecord]:
    if records is None:
        return
    if isinstance(records, CookieJar):
        yield from records
        return
    if isinstance(records, CookieRecord):
        yield records
        return
    if isinstance(records, Mapping):
        for name, value in records.items():
            if isinstance(value, Mapping) and "name" in value:
                yield CookieRecord.from_any(value)
            else:
                yield CookieRecord(name=str(name), value=str(value))
        return
    if isinstance(records, (str, bytes)):
        raise TypeError("pass a mapping for cookie data, not a raw string")
    for item in records:
        if isinstance(item, CookieRecord):
            yield item
        elif isinstance(item, Mapping):
            yield CookieRecord.from_any(item)
        else:
            raise TypeError(f"unsupported cookie record type: {type(item).__name__}")


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class SessionStoreError(RuntimeError):
    """The on-disk session file is missing, unreadable, or malformed."""


@dataclass(slots=True)
class SessionStore:
    """File-backed cookie storage with atomic writes and 0600 permissions.

    Session cookies *are* credentials: the file is written through a private
    temp file in the same directory (so :func:`os.replace` is atomic), the
    parent directory is created ``0700``, and the file is ``chmod``-ed to 0600
    on every save.
    """

    path: Path
    private_permissions: bool = True
    keep_backup: bool = True

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser()

    # -- io ---------------------------------------------------------------- #

    def exists(self) -> bool:
        return self.path.is_file()

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def load(self) -> CookieJar:
        """Read the stored jar; an empty jar when the file does not exist."""
        if not self.path.is_file():
            return CookieJar()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SessionStoreError(f"cannot read {self.path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SessionStoreError(
                f"{self.path} is not valid JSON; delete it to start fresh"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SessionStoreError(f"{self.path} must contain a JSON object")
        jar = CookieJar.from_storage_state(payload)
        jar.prune_expired()
        return jar

    def save(self, jar: CookieJar, *, metadata: Mapping[str, Any] | None = None) -> Path:
        """Persist ``jar`` atomically; returns the written path."""
        document = jar.to_storage_state()
        if metadata:
            document["arena_api"] = {**document["arena_api"], **dict(metadata)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.private_permissions:
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.private_permissions:
                os.chmod(temporary, 0o600)
            if self.keep_backup and self.path.is_file():
                try:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
                except OSError:
                    pass
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise SessionStoreError(f"cannot write {self.path}: {exc}") from exc
        return self.path

    def clear(self) -> None:
        for candidate in (self.path, Path(str(self.path) + ".bak")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SessionStoreError(f"cannot remove {candidate}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Liveness probing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """Result of a session liveness check."""

    state: ProbeState
    detail: str = ""
    status: int | None = None
    checked_at: float = field(default_factory=_now)

    @property
    def alive(self) -> bool:
        return self.state == "alive"

    @property
    def needs_human(self) -> bool:
        """The origin asked for an interactive verification."""
        return self.state == "challenge"

    @property
    def is_transient(self) -> bool:
        """Network/server-side noise: do not invalidate a session over it."""
        return self.state == "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "status": self.status,
            "checked_at": _iso(self.checked_at),
        }

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        if self.status is None:
            return f"{self.state} ({self.detail})" if self.detail else self.state
        return f"{self.state} (HTTP {self.status}{': ' + self.detail if self.detail else ''})"


class LivenessProbe(Protocol):
    """Callable that answers "is this cookie set still accepted?"."""

    def __call__(self, jar: CookieJar) -> ProbeOutcome:
        ...


@dataclass(slots=True)
class HttpLivenessProbe:
    """GET a cheap endpoint and classify the answer.

    This only *reads* a verdict: 2xx/3xx means the session still works,
    401/403 means it does not, and an interstitial body means a human check is
    required. When a check is required the probe returns ``challenge`` and the
    caller raises :class:`~arena_api.exceptions.ChallengeRequiredError` — no
    attempt is made to satisfy it programmatically.
    """

    url: str
    timeout: float = 10.0
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    alive_statuses: frozenset[int] = frozenset({200, 204, 206, 301, 302, 303, 307, 308})
    opener: Callable[[Request, float], Any] | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""

    def _build_request(self, jar: CookieJar) -> Request:
        request = Request(self.url, method=self.method.upper())
        for name, value in (self.headers or {}).items():
            request.add_header(str(name), str(value))
        header = jar.cookie_header(self.host, scheme=urlparse(self.url).scheme)
        if header:
            request.add_header("Cookie", header)
        # Honest, identifiable client identification. Not spoofed, not rotated.
        request.add_header("Accept", "application/json, text/plain, */*")
        if not _header_value(self.headers, "user-agent"):
            request.add_header("User-Agent", "arena-api/0.2.0 (session liveness probe)")
        return request

    def __call__(self, jar: CookieJar) -> ProbeOutcome:
        request = self._build_request(jar)
        try:
            if self.opener is not None:
                response = self.opener(request, self.timeout)
            else:
                response = urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            body = ""
            headers: Mapping[str, Any] = dict(exc.headers or {})
            try:
                body = exc.read(65_536).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - a broken error body is still an error
                body = ""
            if looks_like_challenge(body, headers):
                return ProbeOutcome(
                    "challenge",
                    "the service served a verification interstitial",
                    exc.code,
                )
            if exc.code in (401, 403):
                return ProbeOutcome("expired", f"session rejected (HTTP {exc.code})", exc.code)
            if exc.code == 429:
                return ProbeOutcome("unavailable", "rate limited (HTTP 429)", exc.code)
            if 500 <= exc.code < 600:
                return ProbeOutcome("unavailable", f"upstream error (HTTP {exc.code})", exc.code)
            return ProbeOutcome("unavailable", f"unexpected HTTP {exc.code}", exc.code)
        except (URLError, OSError, TimeoutError) as exc:
            return ProbeOutcome("unavailable", f"probe failed: {type(exc).__name__}: {exc}")
        with response:
            status = int(getattr(response, "status", 200) or 200)
            raw_headers = dict(getattr(response, "headers", {}) or {})
            # A wall can arrive with a success code, so the body is sampled for
            # every response; the sampling stops early for non-HTML payloads.
            body = _sample_body(response)
        if looks_like_challenge(body, raw_headers):
            return ProbeOutcome("challenge", "verification interstitial in response body", status)
        if status in self.alive_statuses:
            return ProbeOutcome("alive", "", status)
        if status in (401, 403):
            return ProbeOutcome("expired", f"session rejected (HTTP {status})", status)
        return ProbeOutcome("unavailable", f"unexpected HTTP {status}", status)


# --------------------------------------------------------------------------- #
# Refresh policy
# --------------------------------------------------------------------------- #


@dataclass(slots=True, eq=False)
class RefreshPolicy:
    """Timing rules for (re)reading a session. All waits are plain sleeps.

    * ``cookie_ttl`` — how long a freshly read jar may be reused before the
      source is consulted again. ``None`` means "cache until told otherwise".
    * ``min_interval`` — minimum wall time between two refreshes; protects both
      the Firefox profile lock and the origin's auth endpoints.
    * ``max_attempts`` — bounded retry count inside one :meth:`refresh` call.
    * ``retry_backoff`` / ``backoff_multiplier`` / ``backoff_cap`` / ``jitter``
      — exponential backoff between those attempts.
    * ``liveness_interval`` — how often :meth:`AutoSessionManager.ensure` may
      spend a probe on a cached jar (``None`` disables automatic probing).
    * ``expiry_margin`` — treat cookies as expired this many seconds early, so
      a request is not sent with a jar that dies mid-flight.
    * ``refresh_on_challenge`` — allow a checkpoint to run when a probe says a
      human check is pending (the checkpoint only *waits for you*; it never
      solves anything).
    """

    cookie_ttl: float | None = 900.0
    min_interval: float = 15.0
    max_attempts: int = 3
    retry_backoff: float = 0.5
    backoff_multiplier: float = 2.0
    backoff_cap: float = 20.0
    jitter: float = 0.15
    liveness_interval: float | None = None
    expiry_margin: float = 30.0
    refresh_on_challenge: bool = True
    verify_after_refresh: bool = False
    rng: random.Random | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.retry_backoff < 0:
            raise ValueError("retry_backoff must be >= 0")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be >= 1")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter must be in [0, 1)")
        if self.min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if self.expiry_margin < 0:
            raise ValueError("expiry_margin must be >= 0")
        for name in ("cookie_ttl", "liveness_interval", "backoff_cap"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive or None")
        if self.rng is None and self.jitter:
            self.rng = random.Random()

    def next_delay(self, attempt: int) -> float:
        """Backoff before retry number ``attempt`` (0-based)."""
        base = self.retry_backoff * (self.backoff_multiplier ** max(0, attempt))
        delay = min(base, self.backoff_cap)
        if self.jitter and self.rng is not None:
            delay += delay * self.rng.uniform(0.0, self.jitter)
        return max(0.0, delay)

    def should_wait(self, last_at: float | None, now: float | None = None) -> bool:
        if last_at is None:
            return False
        return ((now if now is not None else _now()) - last_at) < self.min_interval

    def is_stale(self, fetched_at: float | None, now: float | None = None) -> bool:
        if self.cookie_ttl is None:
            return False
        if fetched_at is None:
            return True
        return ((now if now is not None else _now()) - fetched_at) >= self.cookie_ttl


# --------------------------------------------------------------------------- #
# Sources and checkpoints
# --------------------------------------------------------------------------- #


class SessionSource(Protocol):
    """Where a cookie jar comes from."""

    #: Optional, for sources that can also supply browser-consistent headers.
    def load_cookies(self, domains: Sequence[str] | None = ...) -> Mapping[str, str]:
        ...

    def fetch(self, domains: Sequence[str]) -> CookieJar:
        ...


class StaticSource:
    """An in-memory jar — handy for tests, fixtures, and injected tokens."""

    __slots__ = ("headers", "jar")

    def __init__(
        self,
        cookies: CookieJar | Mapping[str, Any] | Sequence[Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.jar = cookies if isinstance(cookies, CookieJar) else CookieJar(cookies)
        self.headers = dict(headers or {})

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StaticSource {len(self.jar)} cookies>"

    def fetch(self, domains: Sequence[str]) -> CookieJar:
        return CookieJar().update(self.jar)

    def load_cookies(self, domains: Sequence[str] | None = None) -> dict[str, str]:
        host = domains[0] if domains else None
        return self.jar.flat(host)

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        return dict(self.headers)


@dataclass(slots=True)
class CallableSource:
    """Adapt any callable (or method) into a :class:`SessionSource`."""

    factory: Callable[[Sequence[str]], Any]
    headers_factory: Callable[..., Mapping[str, str]] | None = None
    name: str = "callable"

    def fetch(self, domains: Sequence[str]) -> CookieJar:
        payload = self.factory(tuple(domains))
        if payload is None:
            return CookieJar()
        if isinstance(payload, CookieJar):
            return CookieJar().update(payload)
        if isinstance(payload, Mapping):
            if "cookies" in payload and isinstance(payload["cookies"], Sequence) and not isinstance(
                payload["cookies"], (str, bytes)
            ):
                return CookieJar.from_storage_state(payload)
            return CookieJar.from_mapping(payload, domain=domains[0] if domains else "")
        return CookieJar(payload)

    def load_cookies(self, domains: Sequence[str] | None = None) -> dict[str, str]:
        return self.fetch(tuple(domains or ())).flat()

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        if self.headers_factory is None:
            return {}
        return dict(self.headers_factory(navigation=navigation) or {})


@dataclass(slots=True)
class PlaywrightStorageSource:
    """Read a Playwright ``storage_state`` JSON file a human produced.

    Typical flow: you log in (and pass whatever the site asks) in a real
    browser, ``context.storage_state(path=...)`` writes the file, this source
    hands the cookies to the HTTP clients. A mtime check keeps the file from
    being re-read on every request.
    """

    path: Path
    headers: Mapping[str, str] = field(default_factory=dict)
    _last_mtime: float | None = None
    _cached: CookieJar = field(default_factory=CookieJar)

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser()

    def fetch(self, domains: Sequence[str]) -> CookieJar:
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            raise AuthenticationError(
                f"storage state file {self.path} is not readable: {exc}. "
                "Create it by logging in with arena_api.auth.verify.capture_storage_state()."
            ) from exc
        if mtime != self._last_mtime or not self._cached:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise AuthenticationError(
                    f"cannot read storage state {self.path}: {exc}"
                ) from exc
            except json.JSONDecodeError as exc:
                raise AuthenticationError(
                    f"storage state {self.path} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise AuthenticationError(f"storage state {self.path} must be a JSON object")
            self._cached = CookieJar.from_storage_state(payload)
            self._last_mtime = mtime
        jar = CookieJar().update(self._cached)
        jar.prune_expired()
        return jar

    def load_cookies(self, domains: Sequence[str] | None = None) -> dict[str, str]:
        host = domains[0] if domains else None
        return self.fetch(tuple(domains or ())).flat(host)

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        return dict(self.headers)


@dataclass(slots=True)
class FirefoxSource:
    """Wrap an existing :class:`~arena_api.auth.firefox_session.SessionManager`.

    Full cookie metadata is used when the wrapped manager exposes
    ``cookie_records()``; otherwise the flat ``load_cookies()`` mapping is.
    """

    manager: SessionManager
    fallback_domain: str = ""

    def fetch(self, domains: Sequence[str]) -> CookieJar:
        targets = tuple(domains) or ((self.fallback_domain,) if self.fallback_domain else ())
        reader = getattr(self.manager, "cookie_records", None)
        if callable(reader):
            try:
                records = reader(list(targets) or None)
            except TypeError:
                records = reader()
            jar = CookieJar.from_firefox_records(records or [])
            if jar:
                return jar
        loader = getattr(self.manager, "load_cookies", None)
        if not callable(loader):
            raise AuthenticationError(
                "the Firefox session manager exposes neither cookie_records() nor load_cookies()"
            )
        try:
            cookies = loader(list(targets) or None)
        except TypeError:
            cookies = loader()
        domain = targets[0] if targets else self.fallback_domain
        return CookieJar.from_mapping(cookies or {}, domain=domain)

    def load_cookies(self, domains: Sequence[str] | None = None) -> dict[str, str]:
        return self.fetch(tuple(domains or ())).flat()

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        """Delegate the header choice to the Firefox manager.

        These describe the browser the cookies actually came from — the real
        version of "matching your session", not a synthesized fingerprint.
        """
        producer = getattr(self.manager, "firefox_headers", None)
        if not callable(producer):
            return {}
        try:
            return dict(producer(navigation=navigation) or {})
        except TypeError:
            return dict(producer() or {})


@dataclass(slots=True)
class CheckpointContext:
    """What a checkpoint is told before it hands a browser to a human."""

    domains: tuple[str, ...]
    reason: str = ""
    attempt: int = 0
    output: Any = None
    interactive: bool = True

    @property
    def host(self) -> str:
        return self.domains[0] if self.domains else ""

    @property
    def url(self) -> str:
        host = self.host
        return f"https://{host}/" if host else ""


class SessionCheckpoint(Protocol):
    """A human verification hand-off: wait for *you*, then re-read cookies.

    Implementations must not interact with a challenge: no clicking, no
    token extraction, no solving services, no patched browser. Their only
    allowed behaviour is to prepare a normal browser/profile for a person and
    report back the cookies the *site* set.
    """

    def __call__(self, context: CheckpointContext) -> CookieJar | Sequence[Any] | None:
        ...


@dataclass(slots=True)
class ManualCheckpoint:
    """Print guidance, wait for Enter, then re-read ``source``.

    The dependency-free checkpoint, and the same contract
    :meth:`arena_api.auth.firefox_session.FirefoxSessionManager.refresh_session`
    already implements.
    """

    source: SessionSource | None = None
    domains: Sequence[str] = ()
    url_template: str = "https://{host}/"
    prompt: str = (
        "Требуется ручная проверка в браузере. Откройте {url}, пройдите проверку "
        "и войдите в аккаунт, затем нажмите Enter для перечитывания сессии..."
    )
    interactive: bool = True
    output: Any = None
    input_fn: Callable[[str], str] = input

    def __call__(self, context: CheckpointContext) -> CookieJar:
        stream = self.output if self.output is not None else (context.output or sys.stdout)
        url = self.url_template.format(host=context.host) if context.host else self.url_template
        message = self.prompt.format(url=url, host=context.host, reason=context.reason)
        print(message, file=stream, flush=True)
        if self.interactive and context.interactive:
            try:
                self.input_fn("")
            except (EOFError, KeyboardInterrupt):
                print("interrupted; the session was not re-read", file=stream, flush=True)
        if self.source is None:
            return CookieJar()
        return self.source.fetch(tuple(self.domains) or context.domains)


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Counters useful for logs, ``/healthz``-style checks, and test reports."""

    refreshes: int = 0
    successful_refreshes: int = 0
    failed_refreshes: int = 0
    checkpoints: int = 0
    probes: int = 0
    saves: int = 0
    last_refresh_at: float | None = None
    last_probe_at: float | None = None
    last_error: str = ""
    last_state: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "refreshes": self.refreshes,
            "successful_refreshes": self.successful_refreshes,
            "failed_refreshes": self.failed_refreshes,
            "checkpoints": self.checkpoints,
            "probes": self.probes,
            "saves": self.saves,
            "last_refresh_at": _iso(self.last_refresh_at),
            "last_probe_at": _iso(self.last_probe_at),
            "last_error": self.last_error,
            "last_state": self.last_state,
        }


class AutoSessionManager:
    """Own a session: keep it in memory, on disk, alive, and honestly refreshed.

    ``AutoSessionManager`` structurally satisfies the
    :class:`~arena_api.auth.firefox_session.SessionManager` protocol, so it can
    be passed straight to ``ArenaClient(session_source=...)``::

        from arena_api import ArenaClient
        from arena_api.auth import AutoSessionManager, FirefoxSource, SessionStore

        manager = AutoSessionManager(
            FirefoxSource(FirefoxSessionManager(domains=("arena.ai",))),
            domains=("arena.ai",),
            store=SessionStore(Path.home() / ".cache/arena-api/session.json"),
            policy=RefreshPolicy(cookie_ttl=900.0, min_interval=60.0),
        )
        client = ArenaClient(session_source=manager)

    ``probe=`` opts into liveness checks; ``checkpoint=`` opts into a human
    verification hand-off. Both are off by default, and neither can solve a
    challenge: when a check is required and nothing can satisfy it, callers get
    :class:`~arena_api.exceptions.ChallengeRequiredError`.
    """

    def __init__(
        self,
        source: SessionSource | None = None,
        *,
        domains: Sequence[str] = (),
        store: SessionStore | Path | str | None = None,
        policy: RefreshPolicy | None = None,
        probe: LivenessProbe | None = None,
        checkpoint: SessionCheckpoint | None = None,
        interactive: bool = True,
        output: Any = None,
        clock: Callable[[], float] = _now,
        sleep: Callable[[float], None] = time.sleep,
        allow_bootstrap_from_store: bool = True,
    ) -> None:
        if source is None and store is None:
            raise ValueError("provide a cookie source, a store to read, or both")
        self.source = source
        self.domains = self._normalize_domains(domains)
        self.store = self._normalize_store(store)
        self.policy = policy or RefreshPolicy()
        self.probe = probe
        self.checkpoint = checkpoint
        self.interactive = bool(interactive)
        self.output = output if output is not None else sys.stdout
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._jar = CookieJar()
        self._fetched_at: float | None = None
        self._last_refresh_at: float | None = None
        self._last_probe: ProbeOutcome | None = None
        self._headers: dict[str, str] = {}
        self._stats = SessionStats()
        self._closed = False
        if self.store is not None and allow_bootstrap_from_store:
            self.load_from_store(silent=True)

    # -- setup helpers ------------------------------------------------------ #

    @staticmethod
    def _normalize_domains(domains: Sequence[str]) -> tuple[str, ...]:
        result: list[str] = []
        for item in domains or ():
            host = _normalize_host(item)
            if host and host not in result:
                result.append(host)
        return tuple(result)

    @staticmethod
    def _normalize_store(store: SessionStore | Path | str | None) -> SessionStore | None:
        if store is None or isinstance(store, SessionStore):
            return store
        return SessionStore(Path(store))

    @property
    def primary_host(self) -> str:
        return self.domains[0] if self.domains else ""

    # -- state -------------------------------------------------------------- #

    @property
    def jar(self) -> CookieJar:
        """A defensive copy of the current jar."""
        with self._lock:
            return CookieJar().update(self._jar)

    @property
    def cookies(self) -> dict[str, str]:
        with self._lock:
            return self._jar.flat()

    @property
    def last_probe(self) -> ProbeOutcome | None:
        with self._lock:
            return self._last_probe

    @property
    def stats(self) -> SessionStats:
        with self._lock:
            return self._stats

    @property
    def expires_at(self) -> float | None:
        with self._lock:
            return self._jar.earliest_expiry()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        with self._lock:
            count = len(self._jar)
        return (
            f"<AutoSessionManager source={type(self.source).__name__} "
            f"domains={list(self.domains)} cookies={count} store="
            f"{self.store.path if self.store else None}>"
        )

    # -- SessionManager protocol ------------------------------------------- #

    def load_cookies(self, domains: Sequence[str] | None = None) -> dict[str, str]:
        """Return the current cookies, refreshing them if the policy says so.

        Without ``domains`` the whole jar is returned (the source was already
        scoped when the manager was built); with it, the first entry is used as
        the host to match cookie domains against, exactly like a browser
        matching a request URL. Expired cookies are always skipped, and when one
        name exists at several scopes the most specific one wins.

        """
        scoped = self._normalize_domains(domains) if domains else ()
        jar = self.ensure()
        return jar.flat(scoped[0] if scoped else None)

    def cookie_records(self) -> list[dict[str, Any]]:
        """Full records (name/value/domain/path/expiry/flags), store-shaped."""
        with self._lock:
            return [record.as_dict() for record in self._jar]

    def headers(self, *, navigation: bool = False) -> dict[str, str]:
        """Headers contributed by the underlying source, if any.

        Never synthesized here: an HTTP client that has nothing honest to say
        identifies itself as ``arena-api/<version>``, per
        :data:`arena_api.constants.DEFAULT_HEADERS`.
        """
        with self._lock:
            headers = dict(self._headers)
        if headers:
            return headers
        producer = getattr(self.source, "firefox_headers", None)
        if callable(producer):
            try:
                return dict(producer(navigation=navigation) or {})
            except TypeError:
                return dict(producer() or {})
        return {}

    def firefox_headers(self, *, navigation: bool = False) -> dict[str, str]:
        return self.headers(navigation=navigation)

    def refresh_session(self, reason: BaseException | None = None) -> dict[str, str] | None:
        """The ``session_refresher`` hook shape the clients install.

        Returns fresh cookies, or ``None`` when no session could be obtained —
        the caller then surfaces the original error instead of retrying forever.
        """
        try:
            jar = self.refresh(reason=reason)
        except (AuthenticationError, ChallengeRequiredError):
            return None
        return jar.flat()

    def to_curl_cffi(self) -> dict[str, dict[str, str]]:
        """Domain-grouped cookies for ``curl_cffi``'s jar."""
        with self._lock:
            grouped: dict[str, dict[str, str]] = {}
            for record in self._jar:
                grouped.setdefault(record.domain or "", {})[record.name] = record.value
        return grouped

    def to_storage_state(self, path: Path | str | None = None) -> dict[str, Any]:
        """Playwright-shaped storage state, optionally written to ``path``."""
        with self._lock:
            document = self._jar.to_storage_state()
        if path is not None:
            target = Path(path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        return document

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        """Stop the background refresher and persist the current jar."""
        if self._closed:
            return
        self._closed = True
        self.stop_background_refresh()
        if self.store is not None:
            try:
                self.save_to_store()
            except SessionStoreError:
                pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def load_from_store(self, *, silent: bool = False) -> CookieJar:
        if self.store is None:
            return CookieJar()
        try:
            jar = self.store.load()
        except SessionStoreError:
            if silent:
                return CookieJar()
            raise
        if not jar:
            return CookieJar()
        with self._lock:
            self._jar.update(jar)
            self._fetched_at = self._clock()
        return jar

    def save_to_store(self, *, metadata: Mapping[str, Any] | None = None) -> Path | None:
        if self.store is None:
            return None
        with self._lock:
            jar = CookieJar().update(self._jar)
        path = self.store.save(jar, metadata=metadata)
        with self._lock:
            self._stats = _replace_stats(self._stats, saves=self._stats.saves + 1)
        return path

    # -- the main entry points --------------------------------------------- #

    def ensure(self, *, force: bool = False) -> CookieJar:
        """Return a usable jar, touching the source only when the policy asks.

        Nothing happens while the cached jar is inside ``cookie_ttl``, not
        expiring, and — when liveness probing is enabled — recently probed OK.
        """
        now = self._clock()
        with self._lock:
            has_cache = bool(self._jar)
            expired = not has_cache or self._jar.is_expired(
                now, margin=self.policy.expiry_margin
            )
            stale = self.policy.is_stale(self._fetched_at, now)
            outcome = self._last_probe
        probe_due = (
            self.policy.liveness_interval is not None
            and has_cache
            and (outcome is None or (now - outcome.checked_at) >= self.policy.liveness_interval)
        )
        if not force and not expired and not stale:
            if not probe_due:
                return self.jar
            result = self.check()
            if not (result.needs_human or result.state == "expired"):
                return self.jar
        return self.refresh()

    def check(self) -> ProbeOutcome:
        """Run the configured liveness probe against the current jar."""
        if self.probe is None:
            raise AuthenticationError(
                "no liveness probe configured; pass probe= to enable check()"
            )
        with self._lock:
            jar = CookieJar().update(self._jar)
        return self._record_probe(self.probe(jar))

    def refresh(self, *, reason: BaseException | str | None = None) -> CookieJar:
        """Re-read the session under :class:`RefreshPolicy`, with backoff.

        Raises :class:`~arena_api.exceptions.AuthenticationError` when no
        cookies can be obtained and
        :class:`~arena_api.exceptions.ChallengeRequiredError` when the origin
        demands a human verification that no checkpoint can wait out.
        """
        detail = str(reason) if reason is not None else ""
        with self._refresh_lock:
            now = self._clock()
            with self._lock:
                throttled = bool(self._jar) and self.policy.should_wait(self._last_refresh_at, now)
                cached = CookieJar().update(self._jar)
            if throttled and not self._requires_forced_refresh(reason):
                return cached
            attempts = int(self.policy.max_attempts)
            last_error: BaseException | None = None
            for attempt in range(attempts):
                if attempt:
                    self._sleep(self.policy.next_delay(attempt - 1))
                with self._lock:
                    prompts_before = self._stats.checkpoints
                try:
                    jar = self._refresh_once(reason=detail, attempt=attempt)
                except ChallengeRequiredError as exc:
                    last_error = exc
                    with self._lock:
                        already_asked = self._stats.checkpoints > prompts_before
                        self._stats = _replace_stats(
                            self._stats,
                            last_state="challenge",
                            last_error=str(exc),
                        )
                    # Retrying is pointless once a human was asked (they are the
                    # only thing that can change the answer) or when a retry would
                    # prompt them a second time in the same call.
                    if (
                        not self.policy.refresh_on_challenge
                        or self.checkpoint is None
                        or already_asked
                        or attempt + 1 >= attempts
                    ):
                        raise
                    continue
                except (AuthenticationError, SessionStoreError) as exc:
                    last_error = exc
                    continue
                except Exception as exc:  # noqa: BLE001 - source is caller code
                    last_error = AuthenticationError(
                        f"cookie source failed: {type(exc).__name__}: {exc}"
                    )
                    last_error.__cause__ = exc
                    continue
                with self._lock:
                    self._jar = jar
                    self._fetched_at = self._clock()
                    self._last_refresh_at = self._fetched_at
                    self._stats = _replace_stats(
                        self._stats,
                        refreshes=self._stats.refreshes + 1,
                        successful_refreshes=self._stats.successful_refreshes + 1,
                        last_refresh_at=self._fetched_at,
                        last_error="",
                        last_state="alive",
                    )
                if self.store is not None and jar:
                    try:
                        self.save_to_store()
                    except SessionStoreError as exc:
                        print(f"session not persisted: {exc}", file=self.output, flush=True)
                return jar
            with self._lock:
                self._stats = _replace_stats(
                    self._stats,
                    refreshes=self._stats.refreshes + 1,
                    failed_refreshes=self._stats.failed_refreshes + 1,
                    last_refresh_at=self._clock(),
                    last_error=str(last_error) if last_error else "no cookies",
                    last_state="expired",
                )
            if isinstance(last_error, ChallengeRequiredError):
                raise last_error
            raise AuthenticationError(
                "could not obtain a usable session from "
                f"{type(self.source).__name__ if self.source else 'the session store'}"
                + (f": {last_error}" if last_error else "")
            )

    def _requires_forced_refresh(self, reason: BaseException | str | None) -> bool:
        if isinstance(reason, ChallengeRequiredError):
            return True
        return isinstance(reason, str) and looks_like_challenge(reason)

    def _refresh_once(self, *, reason: str, attempt: int) -> CookieJar:
        jar = CookieJar()
        if self.source is not None:
            jar.update(self.source.fetch(list(self.domains)))
            headers = getattr(self.source, "firefox_headers", None)
            if callable(headers):
                try:
                    produced = dict(headers(navigation=False) or {})
                except TypeError:
                    produced = dict(headers() or {})
                with self._lock:
                    self._headers = produced
        jar.prune_expired(self._clock())
        if not jar and self.checkpoint is not None:
            jar.update(self._run_checkpoint(reason=reason, attempt=attempt))
            jar.prune_expired(self._clock())
        if not jar and self.store is not None:
            jar.update(self.store.load())
        if not jar:
            site = self.primary_host or "the target site"
            raise AuthenticationError(
                f"no session cookies for {site}. Sign in in a normal browser"
                + (" (profile or storage_state)" if self.source is not None else "")
                + " and try again."
            )
        if self.policy.verify_after_refresh and self.probe is not None:
            return self._verify_fresh_session(jar, attempt=attempt)
        return jar

    def _verify_fresh_session(self, jar: CookieJar, *, attempt: int) -> CookieJar:
        """Probe a just-read jar; hand the browser to a human at most once.

        A ``challenge`` verdict means the origin wants a person, so the only
        legal recovery is a checkpoint that *waits* for that person. If the
        session is still walled after the hand-off, the caller gets
        :class:`~arena_api.exceptions.ChallengeRequiredError` rather than a jar
        that is known not to work.
        """
        probe = self.probe
        if probe is None:  # pragma: no cover - guarded by the caller
            return jar
        outcome = self._record_probe(probe(jar))
        if outcome.state == "expired":
            raise AuthenticationError(f"the service rejected this session: {outcome}")
        if not outcome.needs_human:
            return jar
        if self.checkpoint is None:
            raise ChallengeRequiredError(_CHALLENGE_HINT)
        refreshed = self._run_checkpoint(reason="challenge", attempt=attempt)
        if not refreshed:
            raise ChallengeRequiredError(_CHALLENGE_HINT)
        merged = CookieJar().update(jar).update(refreshed)
        merged.prune_expired(self._clock())
        recheck = self._record_probe(probe(merged))
        if recheck.needs_human:
            raise ChallengeRequiredError(_CHALLENGE_HINT)
        if recheck.state == "expired":
            raise AuthenticationError(
                f"the session is still rejected after the manual check: {recheck}"
            )
        return merged

    def _record_probe(self, outcome: ProbeOutcome) -> ProbeOutcome:
        with self._lock:
            self._last_probe = outcome
            self._stats = _replace_stats(
                self._stats,
                probes=self._stats.probes + 1,
                last_probe_at=outcome.checked_at,
                last_state=outcome.state,
            )
        return outcome

    def _run_checkpoint(self, *, reason: str, attempt: int) -> CookieJar:
        checkpoint = self.checkpoint
        if checkpoint is None:
            return CookieJar()
        context = CheckpointContext(
            domains=self.domains or ("",),
            reason=reason,
            attempt=attempt,
            output=self.output,
            interactive=self.interactive,
        )
        with self._lock:
            self._stats = _replace_stats(
                self._stats, checkpoints=self._stats.checkpoints + 1
            )
        payload = checkpoint(context)
        if payload is None:
            return CookieJar()
        if isinstance(payload, CookieJar):
            jar = payload
        elif isinstance(payload, Mapping):
            if "cookies" in payload:
                jar = CookieJar.from_storage_state(payload)
            else:
                jar = CookieJar.from_mapping(payload, domain=self.primary_host)
        else:
            jar = CookieJar(payload)
        return self._scope_domainless(jar)

    def _scope_domainless(self, jar: CookieJar) -> CookieJar:
        """Give domain-less records from a source/checkpoint a real scope.

        A producer that only reports ``{"name": "value"}`` cannot express
        precedence, and an empty domain would outrank nothing while still being
        merged into the jar. Inheriting the scope of the record it replaces (or
        the manager's primary host) keeps ``Cookie`` header construction
        browser-correct and makes fresh values win over stale ones. The caller's
        jar is left untouched; a re-scoped copy is returned.
        """
        if not jar or all(record.domain for record in jar):
            return jar
        with self._lock:
            current = CookieJar().update(self._jar)
        scoped = CookieJar()
        for record in jar:
            if not record.domain:
                template = current.get(record.name)
                record = replace(
                    record,
                    domain=template.domain if template is not None else self.primary_host,
                )
            scoped.add(record)
        return scoped

    # -- background refresh ------------------------------------------------- #

    def start_background_refresh(
        self, interval: float = 600.0, *, probe_first: bool = False
    ) -> threading.Thread:
        """Refresh the session in a daemon thread every ``interval`` seconds.

        Failures are logged and retried; the thread never converts a transient
        error into a raised exception in the main thread.
        """
        if self._closed:
            raise RuntimeError("manager is closed")
        if interval <= 0:
            raise ValueError("interval must be positive")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
        probe = self.probe if probe_first else None

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    if probe is not None:
                        outcome = self.check()
                        if outcome.alive or outcome.is_transient:
                            continue
                    self.refresh()
                except Exception as exc:  # noqa: BLE001 - keep the thread alive
                    print(f"background session refresh failed: {exc}", file=self.output, flush=True)

        thread = threading.Thread(
            target=loop, name=f"arena-api-session-refresh-{id(self):x}", daemon=True
        )
        self._thread = thread
        thread.start()
        return thread

    def stop_background_refresh(self, *, timeout: float = 5.0) -> bool:
        """Signal the refresher to stop and join it; ``True`` if it stopped."""
        thread = self._thread
        if thread is None:
            return True
        self._stop.set()
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
            self._stop.clear()
        return stopped


def _replace_stats(stats: SessionStats, **changes: Any) -> SessionStats:
    return replace(stats, **changes)


#: Documented alias for the name used in the original design notes. ``AutoCookieManager``
#: owns cookie lifetime (store/probe/refresh); acquiring a session still happens where a
#: person does it — see :mod:`arena_api.auth.verify`.
AutoCookieManager = AutoSessionManager
