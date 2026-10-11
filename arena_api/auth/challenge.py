"""Detection and diagnostics for browser verification challenges.

What this module does
---------------------
In a synthetic-monitoring or E2E pipeline you need to know, unambiguously and
early, when the site under test served an interactive verification page
instead of the page you meant to test. A challenge that is silently clicked
through is worse than a challenge that fails the build: it hides the fact that
your monitor is being rate-shaped, it hides a misconfigured WAF rule, and it
produces green runs that measure nothing.

So this module answers three questions and stops:

* **Is this a challenge?** -- :func:`inspect_response` for the fast HTTP path
  (``curl_cffi``/``httpx``), :func:`inspect_page` for the Playwright path.
* **What kind, and what gave it away?** -- a :class:`ChallengeObservation`
  carrying every matched signal with its source (header / status / DOM
  selector / frame URL / body text).
* **What do we do about it?** -- :meth:`ChallengeHandler.raise_for` raises
  :class:`~arena_api.exceptions.ChallengeRequiredError` with a remediation
  guide and paths to captured screenshots, HTML and metadata.

What this module deliberately does not do
-----------------------------------------
It does not solve challenges. Specifically, there is no:

* Turnstile checkbox localisation or synthetic click;
* challenge ``iframe`` bounding-box targeting and pointer playback;
* interception or download of the accessibility audio stream;
* speech-to-text transcription of that stream and token submission;
* ``cf_clearance`` minting, harvesting, or refresh automation.

Those are circumvention of an access control the site owner deployed on
purpose. They are also operationally fragile: any of them converts a loud,
actionable failure into a silent, intermittent one. The remediation guide in
:data:`REMEDIATION_TEMPLATE` lists the durable fixes -- allowlisting,
Cloudflare's published Turnstile *test* keys for CI, an exception rule on a
zone you own, or a real service credential instead of a borrowed browser
session.

The detection logic itself is standard and safe: it reads response headers,
status codes, frame URLs and public DOM markers. Nothing here interacts with
the challenge widget beyond observing that it is present.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

from ..exceptions import ChallengeRequiredError

__all__ = [
    "CLOUDFLARE_FRAME_HOSTS",
    "ChallengeHandler",
    "ChallengeKind",
    "ChallengeObservation",
    "ChallengeSignal",
    "HCAPTCHA_SELECTORS",
    "RECAPTCHA_SELECTORS",
    "REMEDIATION_TEMPLATE",
    "STATUS_TEXT_MARKERS",
    "TURNSTILE_SELECTORS",
    "inspect_page",
    "inspect_response",
]

TURNSTILE_SELECTORS: tuple[str, ...] = (
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='turnstile']",
    ".cf-turnstile",
    "div[data-sitekey]",
    "[data-cf-turnstile-response]",
)

HCAPTCHA_SELECTORS: tuple[str, ...] = (
    "iframe[src*='hcaptcha.com']",
    "iframe[src*='hcaptcha']",
    ".h-captcha",
    "[data-hcaptcha-widget-id]",
)

RECAPTCHA_SELECTORS: tuple[str, ...] = (
    "iframe[src*='google.com/recaptcha']",
    "iframe[src*='recaptcha']",
    ".g-recaptcha",
    "[data-recaptcha-widget-id]",
)

#: Frame hosts that indicate a Cloudflare interstitial rather than page content.
CLOUDFLARE_FRAME_HOSTS: tuple[str, ...] = (
    "challenges.cloudflare.com",
    "cf-assets.com",
    "cdn-cgi/challenge-platform",
)

#: Substrings matched case-insensitively against the visible body text.
STATUS_TEXT_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "checking if the site connection is secure",
    "verify you are human",
    "verify that you are human",
    "attention required",
    "enable javascript and cookies to continue",
    "performance & security by cloudflare",
    "performance and security by cloudflare",
    "please complete the security check",
    "one more step",
    "unusual traffic from your",
    "ddos protection by",
)

#: Headers worth keeping verbatim in a captured observation.
_DETAIL_HEADERS: tuple[str, ...] = (
    "cf-mitigated",
    "cf-ray",
    "cf-chl-out",
    "cf-cache-status",
    "server",
    "retry-after",
    "content-type",
    "x-arena-challenge",
    "location",
)

_INTERESTING_HEADER_FRAGMENTS: tuple[str, ...] = ("cf-", "challenge", "turnstile")

#: Status codes that, on their own, are only a hint -- paired with a marker.
_SUSPECT_STATUS: frozenset[int] = frozenset({401, 403, 429, 503})


class ChallengeKind(str, Enum):
    """Classification of a detected verification screen."""

    NONE = "none"
    TURNSTILE = "turnstile"
    CLOUDFLARE_MANAGED = "cloudflare_managed"
    RECAPTCHA = "recaptcha"
    HCAPTCHA = "hcaptcha"
    INTERSTITIAL = "interstitial"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


class ChallengeSignal(NamedTuple):
    """A single piece of evidence that a challenge is being served."""

    source: str
    detail: str
    kind: ChallengeKind


@dataclass(frozen=True)
class ChallengeObservation:
    """Result of scanning a page or response for a verification challenge."""

    url: str
    kind: ChallengeKind
    signals: tuple[ChallengeSignal, ...]
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    frame_urls: tuple[str, ...] = ()
    html_path: str | None = None
    screenshot_path: str | None = None
    detected_at: str = ""

    @property
    def is_challenge(self) -> bool:
        """Whether anything was actually detected."""
        return self.kind is not ChallengeKind.NONE

    @property
    def markers(self) -> tuple[str, ...]:
        """Deduplicated human-readable list of what matched."""
        seen: list[str] = []
        for signal in self.signals:
            label = f"{signal.source}: {signal.detail}"
            if label not in seen:
                seen.append(label)
        return tuple(seen)

    def summary(self) -> str:
        """One-line description suitable for a CI log."""
        markers = ", ".join(self.markers) or "no markers"
        status = "?" if self.status is None else str(self.status)
        return f"{self.kind.value} challenge at {self.url} (status {status}; {markers})"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form for artefact capture."""
        return {
            "url": self.url,
            "kind": self.kind.value,
            "status": self.status,
            "detected_at": self.detected_at,
            "signals": [
                {"source": s.source, "detail": s.detail, "kind": s.kind.value}
                for s in self.signals
            ],
            "headers": dict(self.headers),
            "frame_urls": list(self.frame_urls),
            "html_path": self.html_path,
            "screenshot_path": self.screenshot_path,
        }


REMEDIATION_TEMPLATE = """\
arena-api did not solve the challenge and will not bypass it. Fix it at the \
infrastructure level instead:

  1. Allowlist the monitor. Add a WAF skip / bot-management exception rule for
     your CI runner's egress IPs or ASN, or match on the \
"{monitor_header}" request header this client already sends when \
identify_automation is enabled.
  2. Use the vendor's own test credentials in CI. Cloudflare publishes Turnstile
     test sitekey/secret pairs that always return a passing token, so a pipeline
     can exercise the real widget path without evading anything.
  3. Relax the rule on zones you own: disable Bot Fight Mode / managed
     challenges for the staging hostname your suite targets.
  4. Prefer a real credential over a borrowed browser session. For
     machine-to-machine monitoring, an API token or service account is both
     more stable and more honest than replaying a user's cookies.
  5. If you must exercise production, do it at a human-scale cadence and
     identify the traffic so the operator can tune their rule.
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return ""


def _filter_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).lower()
        if lowered in _DETAIL_HEADERS or any(
            fragment in lowered for fragment in _INTERESTING_HEADER_FRAGMENTS
        ):
            kept[str(key)] = str(value)
    return kept


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classify(signals: Sequence[ChallengeSignal]) -> ChallengeKind:
    """Reduce a set of signals to one :class:`ChallengeKind`."""
    if not signals:
        return ChallengeKind.NONE
    # A decisive response header (``cf-mitigated: challenge``) outranks a
    # widget reference found in the body, because managed challenges also
    # embed a challenge iframe.
    priority = (
        ChallengeKind.CLOUDFLARE_MANAGED,
        ChallengeKind.TURNSTILE,
        ChallengeKind.RECAPTCHA,
        ChallengeKind.HCAPTCHA,
        ChallengeKind.RATE_LIMIT,
        ChallengeKind.INTERSTITIAL,
    )
    kinds = {signal.kind for signal in signals}
    for kind in priority:
        if kind in kinds:
            return kind
    return ChallengeKind.UNKNOWN


def _match_text(body: str, markers: Iterable[str]) -> list[str]:
    lowered = body.lower()
    return [marker for marker in markers if marker in lowered]


# --------------------------------------------------------------------------- #
# Fast path: HTTP response inspection (no browser required)
# --------------------------------------------------------------------------- #


def inspect_response(
    status: int | None,
    headers: Mapping[str, Any] | None,
    body: str,
    *,
    url: str = "",
    extra_text_markers: Sequence[str] = (),
) -> ChallengeObservation:
    """Classify an HTTP response as a challenge or not.

    This is the cheap check that runs before any browser is launched: a
    ``curl_cffi`` probe hits the target and this function decides whether the
    stored session is still usable.

    A status code alone is never sufficient -- ``403`` is also a plain
    authorisation failure. Markers must corroborate.
    """
    headers = headers or {}
    body = body or ""
    signals: list[ChallengeSignal] = []

    if _header_value(headers, "cf-mitigated").lower() == "challenge":
        signals.append(
            ChallengeSignal("header", "cf-mitigated: challenge", ChallengeKind.CLOUDFLARE_MANAGED)
        )

    content_type = _header_value(headers, "content-type").lower()
    looks_html = "html" in content_type or not content_type
    probe_body = body[:200_000] if looks_html else ""

    for marker in _match_text(probe_body, STATUS_TEXT_MARKERS):
        signals.append(
            ChallengeSignal("body-text", f"marker {marker!r}", ChallengeKind.INTERSTITIAL)
        )
    for marker in _match_text(probe_body, extra_text_markers):
        signals.append(
            ChallengeSignal("body-text", f"custom marker {marker!r}", ChallengeKind.UNKNOWN)
        )

    body_lower = probe_body.lower()
    if "challenges.cloudflare.com" in body_lower or "turnstile" in body_lower:
        signals.append(
            ChallengeSignal("body-text", "challenges.cloudflare.com reference", ChallengeKind.TURNSTILE)
        )
    if "cdn-cgi/challenge-platform" in body_lower:
        signals.append(
            ChallengeSignal(
                "body-text", "cdn-cgi/challenge-platform", ChallengeKind.CLOUDFLARE_MANAGED
            )
        )
    if "hcaptcha.com" in body_lower:
        signals.append(ChallengeSignal("body-text", "hcaptcha.com", ChallengeKind.HCAPTCHA))
    if "google.com/recaptcha" in body_lower or "grecaptcha" in body_lower:
        signals.append(ChallengeSignal("body-text", "recaptcha", ChallengeKind.RECAPTCHA))

    if status == 429 or _header_value(headers, "retry-after"):
        signals.append(
            ChallengeSignal("status", f"status {status}", ChallengeKind.RATE_LIMIT)
        )

    server = _header_value(headers, "server").lower()
    if status in _SUSPECT_STATUS and server == "cloudflare" and signals:
        signals.append(
            ChallengeSignal(
                "header", f"cloudflare served status {status}", ChallengeKind.CLOUDFLARE_MANAGED
            )
        )

    return ChallengeObservation(
        url=url,
        kind=_classify(signals),
        signals=tuple(signals),
        status=status,
        headers=_filter_headers(headers),
        detected_at=_utc_now(),
    )


# --------------------------------------------------------------------------- #
# Browser path: page inspection
# --------------------------------------------------------------------------- #

_PAGE_SCAN_JS = """
(pairs) => {
  const result = { selectors: [], frames: [], text: '', title: '' };
  for (const selector of pairs) {
    try {
      if (document.querySelector(selector)) result.selectors.push(selector);
    } catch (error) {
      /* invalid selector for this document; ignore */
    }
  }
  result.frames = Array.from(document.querySelectorAll('iframe'))
    .map((frame) => frame.src || frame.getAttribute('data-src') || '')
    .filter(Boolean)
    .slice(0, 50);
  result.title = document.title || '';
  result.text = ((document.body && document.body.innerText) || '').slice(0, 8000);
  return result;
}
"""


async def inspect_page(
    page: Any,
    *,
    capture_dir: str | Path | None = None,
    screenshot: bool = True,
    extra_selectors: Sequence[str] = (),
    extra_text_markers: Sequence[str] = (),
    url: str | None = None,
) -> ChallengeObservation:
    """Scan a Playwright page for a verification challenge.

    Examines frame URLs, DOM selectors and visible text. When a challenge is
    found and ``capture_dir`` is set, a screenshot, the page HTML and a JSON
    metadata file are written there for post-mortem analysis.

    This only *observes*. It does not locate, focus, or interact with any
    widget inside the challenge frame.
    """
    target_url = url if url is not None else str(getattr(page, "url", "") or "")
    signals: list[ChallengeSignal] = []
    frame_urls: list[str] = []

    for frame in getattr(page, "frames", []) or []:
        frame_url = str(getattr(frame, "url", "") or "")
        if frame_url:
            frame_urls.append(frame_url)
        lowered = frame_url.lower()
        for host in CLOUDFLARE_FRAME_HOSTS:
            if host in lowered:
                signals.append(
                    ChallengeSignal("frame", f"{host} in {frame_url}", ChallengeKind.TURNSTILE)
                )
                break
        if "hcaptcha.com" in lowered:
            signals.append(ChallengeSignal("frame", frame_url, ChallengeKind.HCAPTCHA))
        if "recaptcha" in lowered:
            signals.append(ChallengeSignal("frame", frame_url, ChallengeKind.RECAPTCHA))

    scan = {"selectors": [], "frames": [], "text": "", "title": ""}
    try:
        scan = await page.evaluate(_PAGE_SCAN_JS, list(_all_selectors(extra_selectors)))
    except Exception:
        scan = {"selectors": [], "frames": [], "text": "", "title": ""}

    for selector in scan.get("selectors", []):
        signals.append(
            ChallengeSignal("dom", f"matched {selector}", _kind_for_selector(selector))
        )
    for frame_url in scan.get("frames", []):
        if frame_url not in frame_urls:
            frame_urls.append(str(frame_url))

    haystack = "\n".join(
        part for part in (scan.get("title", ""), scan.get("text", "")) if part
    )
    for marker in _match_text(haystack, STATUS_TEXT_MARKERS):
        signals.append(
            ChallengeSignal("dom-text", f"marker {marker!r}", ChallengeKind.INTERSTITIAL)
        )
    for marker in _match_text(haystack, extra_text_markers):
        signals.append(
            ChallengeSignal("dom-text", f"custom marker {marker!r}", ChallengeKind.UNKNOWN)
        )

    observation = ChallengeObservation(
        url=target_url,
        kind=_classify(signals),
        signals=tuple(signals),
        frame_urls=tuple(frame_urls[:50]),
        detected_at=_utc_now(),
    )
    if observation.is_challenge and capture_dir is not None:
        observation = await _capture(page, observation, capture_dir, screenshot)
    return observation


def _all_selectors(extra: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for selector in (
        *TURNSTILE_SELECTORS,
        *RECAPTCHA_SELECTORS,
        *HCAPTCHA_SELECTORS,
        *extra,
    ):
        if selector not in seen:
            seen.append(selector)
    return seen


def _kind_for_selector(selector: str) -> ChallengeKind:
    lowered = selector.lower()
    if "turnstile" in lowered or "cloudflare" in lowered:
        return ChallengeKind.TURNSTILE
    if "hcaptcha" in lowered:
        return ChallengeKind.HCAPTCHA
    if "recaptcha" in lowered:
        return ChallengeKind.RECAPTCHA
    return ChallengeKind.UNKNOWN


async def _capture(
    page: Any, observation: ChallengeObservation, capture_dir: str | Path, screenshot: bool
) -> ChallengeObservation:
    """Write screenshot/HTML/metadata for a detected challenge."""
    directory = Path(capture_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    host = re.sub(r"[^a-z0-9]+", "-", observation.url.lower())[:48].strip("-") or "page"
    base = f"challenge-{stamp}-{host}"

    html_path: str | None = None
    try:
        html_path = str(directory / f"{base}.html")
        Path(html_path).write_text(await page.content(), encoding="utf-8")
    except Exception:
        html_path = None

    screenshot_path: str | None = None
    if screenshot:
        try:
            screenshot_path = str(directory / f"{base}.png")
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            screenshot_path = None

    observation = ChallengeObservation(
        url=observation.url,
        kind=observation.kind,
        signals=observation.signals,
        status=observation.status,
        headers=observation.headers,
        frame_urls=observation.frame_urls,
        html_path=html_path,
        screenshot_path=screenshot_path,
        detected_at=observation.detected_at,
    )
    try:
        (directory / f"{base}.json").write_text(
            json.dumps(observation.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return observation


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass
class ChallengeHandler:
    """Detect, capture and report challenges; never solve them.

    Attach to a page with :meth:`install` to flag challenges as they appear
    during a flow, and call :meth:`check` at assertion points.
    """

    capture_dir: str | Path | None = None
    extra_selectors: tuple[str, ...] = ()
    extra_text_markers: tuple[str, ...] = ()
    screenshot: bool = True
    raise_on_detection: bool = True
    on_detection: Callable[[ChallengeObservation], None] | None = None
    _observations: list[ChallengeObservation] = field(default_factory=list, repr=False)

    @property
    def observations(self) -> tuple[ChallengeObservation, ...]:
        """Every challenge detected so far, oldest first."""
        return tuple(self._observations)

    @property
    def last(self) -> ChallengeObservation | None:
        """Most recent challenge, or ``None``."""
        return self._observations[-1] if self._observations else None

    def clear(self) -> None:
        """Forget all recorded observations."""
        self._observations.clear()

    def note(self, observation: ChallengeObservation) -> ChallengeObservation:
        """Record an observation and fire ``on_detection``, without raising.

        Used by the passive response listener, which runs as a background task
        where raising would be swallowed; the explicit :meth:`check` at an
        assertion point is what surfaces the failure.
        """
        if observation.is_challenge:
            self._observations.append(observation)
            if self.on_detection is not None:
                self.on_detection(observation)
        return observation

    def record(self, observation: ChallengeObservation) -> ChallengeObservation:
        """Store an observation, fire ``on_detection``, then honour ``raise_on_detection``."""
        self.note(observation)
        if observation.is_challenge and self.raise_on_detection:
            self.raise_for(observation)
        return observation

    def check_response(
        self,
        status: int | None,
        headers: Mapping[str, Any] | None,
        body: str,
        *,
        url: str = "",
    ) -> ChallengeObservation:
        """Classify an HTTP response (fast path) and record it."""
        observation = inspect_response(
            status,
            headers,
            body,
            url=url,
            extra_text_markers=self.extra_text_markers,
        )
        return self.record(observation)

    async def check(self, page: Any, *, url: str | None = None) -> ChallengeObservation:
        """Scan a Playwright page and record the result."""
        observation = await inspect_page(
            page,
            capture_dir=self.capture_dir,
            screenshot=self.screenshot,
            extra_selectors=self.extra_selectors,
            extra_text_markers=self.extra_text_markers,
            url=url,
        )
        return self.record(observation)

    async def install(self, page: Any) -> None:
        """Watch a page's responses and frame navigations for challenges.

        Useful for long flows where a challenge may appear mid-run rather than
        on the landing page. Detection is purely observational: the response
        body is read, never modified or replayed.
        """

        async def _on_response(response: Any) -> None:
            try:
                if response.status in _SUSPECT_STATUS or response.status == 200:
                    body = await _safe_body(response)
                    if body:
                        self.check_response(
                            response.status,
                            response.headers,
                            body,
                            url=response.url,
                        )
            except ChallengeRequiredError:
                raise
            except Exception:
                pass

        page.on("response", lambda response: _schedule(_on_response(response)))

    def raise_for(self, observation: ChallengeObservation) -> None:
        """Raise :class:`ChallengeRequiredError` with remediation guidance."""
        artifacts = [
            path
            for path in (observation.screenshot_path, observation.html_path)
            if path
        ]
        detail = "\n".join(
            [
                observation.summary(),
                "",
                REMEDIATION_TEMPLATE.format(monitor_header="X-Synthetic-Monitor"),
            ]
        )
        if artifacts:
            detail += "\nCaptured diagnostics:\n" + "\n".join(
                f"  - {path}" for path in artifacts
            )
        raise ChallengeRequiredError(detail)


async def _safe_body(response: Any) -> str:
    """Read a response body as text, tolerating bodies that cannot be read."""
    try:
        content_type = str(response.headers.get("content-type", "")).lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            return ""
        return await response.text()
    except Exception:
        return ""


def _schedule(coroutine: Any) -> Any:
    """Schedule ``coroutine`` on the running loop without awaiting it."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coroutine.close()
        return None
    return loop.create_task(coroutine)
