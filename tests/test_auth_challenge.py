"""Tests for challenge detection and diagnostics (no browser required)."""

from __future__ import annotations

import asyncio
import json

import pytest

from arena_api.auth.challenge import (
    ChallengeHandler,
    ChallengeKind,
    ChallengeObservation,
    ChallengeSignal,
    _classify,
    inspect_page,
    inspect_response,
)
from arena_api.exceptions import ChallengeRequiredError

CLOUDFLARE_HTML = """
<!doctype html><html><head><title>Just a moment...</title></head>
<body><div class="cf-browser-verification">
Checking if the site connection is secure
<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></iframe>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
</div></body></html>
"""

TURNSTILE_HTML = """
<!doctype html><html><body>
<form action="/login" method="post">
  <div class="cf-turnstile" data-sitekey="0x4AAAAAAA"></div>
</form>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
</body></html>
"""

CLEAN_HTML = """
<!doctype html><html><head><title>Dashboard</title></head>
<body><h1>Welcome back</h1><nav><a href="/settings">Settings</a></nav></body></html>
"""


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeResponse:
    def __init__(self, url: str, status: int, headers: dict, text: str) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._text = text

    async def text(self) -> str:
        return self._text


class FakeChallengePage:
    """Page stand-in: frames, DOM scan results, HTML, screenshot capture."""

    def __init__(
        self,
        html: str = CLEAN_HTML,
        scan: dict | None = None,
        frames: list[FakeFrame] | None = None,
        url: str = "https://example.test/app",
    ) -> None:
        self._html = html
        self._scan = scan or {}
        self.frames = frames or []
        self.url = url
        self.screenshots: list[str] = []
        self.listeners: dict[str, list] = {}

    async def evaluate(self, script: str, arg=None):
        return {
            "selectors": self._scan.get("selectors", []),
            "frames": self._scan.get("frames", []),
            "text": self._scan.get("text", ""),
            "title": self._scan.get("title", ""),
        }

    async def content(self) -> str:
        return self._html

    async def screenshot(self, path: str, full_page: bool = False) -> bytes:
        self.screenshots.append(path)
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        return b""

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)


# --------------------------------------------------------------------------- #
# Signal reduction
# --------------------------------------------------------------------------- #


def test_classify_returns_none_for_no_signals() -> None:
    assert _classify([]) is ChallengeKind.NONE


def test_classify_prefers_the_specific_widget_over_generic_text() -> None:
    signals = [
        ChallengeSignal("dom-text", "marker 'just a moment'", ChallengeKind.INTERSTITIAL),
        ChallengeSignal("dom", "matched .cf-turnstile", ChallengeKind.TURNSTILE),
    ]
    assert _classify(signals) is ChallengeKind.TURNSTILE


def test_classify_falls_back_to_unknown() -> None:
    signals = [ChallengeSignal("dom", "matched x", ChallengeKind.UNKNOWN)]
    assert _classify(signals) is ChallengeKind.UNKNOWN


# --------------------------------------------------------------------------- #
# Response inspection
# --------------------------------------------------------------------------- #


def test_clean_response_is_not_a_challenge() -> None:
    observation = inspect_response(
        200, {"content-type": "text/html; charset=utf-8"}, CLEAN_HTML
    )
    assert observation.kind is ChallengeKind.NONE
    assert observation.is_challenge is False
    assert observation.markers == ()


def test_bare_403_without_markers_is_not_reported_as_a_challenge() -> None:
    """A status code alone is never enough: 403 is also a plain auth failure."""
    observation = inspect_response(403, {"content-type": "text/html"}, CLEAN_HTML)
    assert observation.kind is ChallengeKind.NONE


def test_cf_mitigated_header_is_decisive() -> None:
    observation = inspect_response(
        403,
        {"cf-mitigated": "challenge", "server": "cloudflare", "content-type": "text/html"},
        CLEAN_HTML,
        url="https://example.test/",
    )
    assert observation.kind is ChallengeKind.CLOUDFLARE_MANAGED
    assert observation.is_challenge
    assert any(signal.source == "header" for signal in observation.signals)


def test_interstitial_text_markers_are_detected() -> None:
    observation = inspect_response(
        503, {"content-type": "text/html", "server": "cloudflare"}, CLOUDFLARE_HTML
    )
    assert observation.kind is not ChallengeKind.NONE
    assert "checking if the site connection is secure" in "\n".join(
        signal.detail for signal in observation.signals
    )


def test_turnstile_reference_in_body_is_detected() -> None:
    observation = inspect_response(200, {"content-type": "text/html"}, TURNSTILE_HTML)
    assert observation.kind is ChallengeKind.TURNSTILE


def test_hcaptcha_and_recaptcha_are_classified() -> None:
    hcaptcha = inspect_response(
        200, {"content-type": "text/html"}, "<div><iframe src='https://hcaptcha.com/1'></iframe>"
    )
    recaptcha = inspect_response(
        200,
        {"content-type": "text/html"},
        "<div class='g-recaptcha' data-sitekey='x'></div><script src='https://www.google.com/recaptcha/api.js'></script>",
    )
    assert hcaptcha.kind is ChallengeKind.HCAPTCHA
    assert recaptcha.kind is ChallengeKind.RECAPTCHA


def test_rate_limit_is_its_own_kind() -> None:
    observation = inspect_response(
        429, {"retry-after": "30", "content-type": "text/html"}, CLEAN_HTML
    )
    assert observation.kind is ChallengeKind.RATE_LIMIT


def test_non_html_bodies_are_not_scanned_for_text() -> None:
    observation = inspect_response(
        200, {"content-type": "application/json"}, '{"checking your browser": true}'
    )
    assert observation.kind is ChallengeKind.NONE


def test_custom_text_markers_are_honoured() -> None:
    observation = inspect_response(
        200,
        {"content-type": "text/html"},
        "<p>maintenance window</p>",
        extra_text_markers=("maintenance window",),
    )
    assert observation.kind is ChallengeKind.UNKNOWN


def test_observation_keeps_only_challenge_relevant_headers() -> None:
    observation = inspect_response(
        403,
        {
            "cf-mitigated": "challenge",
            "cf-ray": "abc-LHR",
            "server": "cloudflare",
            "set-cookie": "session=secret; Path=/",
        },
        CLOUDFLARE_HTML,
    )
    assert "cf-ray" in observation.headers
    assert "set-cookie" not in observation.headers


def test_summary_and_dict_are_usable_in_reports() -> None:
    observation = inspect_response(
        403, {"cf-mitigated": "challenge", "content-type": "text/html"}, CLOUDFLARE_HTML
    )
    assert "challenge at" in observation.summary()
    payload = observation.to_dict()
    assert json.loads(json.dumps(payload))["kind"] == observation.kind.value


# --------------------------------------------------------------------------- #
# Page inspection
# --------------------------------------------------------------------------- #


def test_clean_page_is_not_a_challenge() -> None:
    page = FakeChallengePage(
        html=CLEAN_HTML,
        scan={"selectors": [], "frames": [], "text": "Welcome back", "title": "Dashboard"},
    )
    observation = asyncio.run(inspect_page(page))
    assert observation.kind is ChallengeKind.NONE


def test_turnstile_selector_in_dom_is_detected() -> None:
    page = FakeChallengePage(
        html=TURNSTILE_HTML,
        scan={
            "selectors": [".cf-turnstile", "div[data-sitekey]"],
            "frames": ["https://challenges.cloudflare.com/turnstile/v0/api.js"],
            "text": "Sign in",
            "title": "Sign in",
        },
    )
    observation = asyncio.run(inspect_page(page))
    assert observation.kind is ChallengeKind.TURNSTILE
    assert any(signal.source == "dom" for signal in observation.signals)


def test_challenge_frame_url_is_detected() -> None:
    page = FakeChallengePage(
        frames=[FakeFrame("https://challenges.cloudflare.com/turnstile/v0/widget.html")]
    )
    observation = asyncio.run(inspect_page(page))
    assert observation.kind is ChallengeKind.TURNSTILE
    assert observation.frame_urls


def test_visible_text_markers_are_detected() -> None:
    page = FakeChallengePage(
        scan={"selectors": [], "frames": [], "text": "Just a moment...", "title": "Just a moment..."}
    )
    observation = asyncio.run(inspect_page(page))
    assert observation.kind is ChallengeKind.INTERSTITIAL


def test_detection_captures_screenshot_html_and_metadata(tmp_path) -> None:
    page = FakeChallengePage(
        html=CLOUDFLARE_HTML,
        scan={"selectors": [], "frames": [], "text": "Just a moment", "title": "x"},
    )
    observation = asyncio.run(
        inspect_page(page, capture_dir=tmp_path, screenshot=True)
    )
    assert observation.is_challenge
    assert observation.html_path is not None
    assert observation.screenshot_path is not None
    assert page.screenshots
    metadata = list(tmp_path.glob("challenge-*.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text())["kind"] == observation.kind.value


def test_no_capture_when_no_challenge(tmp_path) -> None:
    page = FakeChallengePage(scan={"selectors": [], "frames": [], "text": "ok", "title": "ok"})
    observation = asyncio.run(inspect_page(page, capture_dir=tmp_path))
    assert observation.is_challenge is False
    assert list(tmp_path.iterdir()) == []


def test_failing_page_scan_does_not_crash() -> None:
    class Broken:
        url = "https://example.test/"
        frames = []

        async def evaluate(self, script, arg=None):
            raise RuntimeError("boom")

    observation = asyncio.run(inspect_page(Broken()))
    assert observation.kind is ChallengeKind.NONE


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #


def test_handler_records_without_raising_when_configured() -> None:
    handler = ChallengeHandler(raise_on_detection=False)
    seen: list[ChallengeObservation] = []
    handler.on_detection = seen.append
    handler.check_response(
        403, {"cf-mitigated": "challenge", "content-type": "text/html"}, CLOUDFLARE_HTML
    )
    assert len(handler.observations) == 1
    assert len(seen) == 1
    assert handler.last is handler.observations[0]


def test_handler_raises_with_remediation_guidance() -> None:
    handler = ChallengeHandler(raise_on_detection=True)
    with pytest.raises(ChallengeRequiredError) as caught:
        handler.check_response(
            403,
            {"cf-mitigated": "challenge", "content-type": "text/html"},
            CLOUDFLARE_HTML,
            url="https://example.test/",
        )
    message = str(caught.value)
    assert "challenge" in message.lower()
    assert "allowlist" in message.lower()
    assert "test sitekey" in message.lower()


def test_handler_clear_resets_history() -> None:
    handler = ChallengeHandler(raise_on_detection=False)
    handler.check_response(
        403, {"cf-mitigated": "challenge", "content-type": "text/html"}, CLOUDFLARE_HTML
    )
    assert handler.observations
    handler.clear()
    assert handler.observations == ()
    assert handler.last is None


def test_handler_check_on_page_raises() -> None:
    page = FakeChallengePage(
        scan={"selectors": [], "frames": [], "text": "Just a moment", "title": "x"}
    )
    handler = ChallengeHandler(raise_on_detection=True)
    with pytest.raises(ChallengeRequiredError):
        asyncio.run(handler.check(page))


def test_installed_response_listener_flags_challenges() -> None:
    handler = ChallengeHandler(raise_on_detection=False)
    page = FakeChallengePage()

    async def scenario() -> None:
        await handler.install(page)
        listener = page.listeners["response"][0]
        listener(
            FakeResponse(
                "https://example.test/",
                403,
                {"cf-mitigated": "challenge", "content-type": "text/html"},
                CLOUDFLARE_HTML,
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert len(handler.observations) == 1
    assert handler.observations[0].kind is ChallengeKind.CLOUDFLARE_MANAGED


def test_installed_listener_ignores_clean_responses() -> None:
    handler = ChallengeHandler()
    page = FakeChallengePage()

    async def scenario() -> None:
        await handler.install(page)
        page.listeners["response"][0](
            FakeResponse(
                "https://example.test/", 200, {"content-type": "text/html"}, CLEAN_HTML
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert handler.observations == ()


def test_installed_listener_ignores_binary_responses() -> None:
    handler = ChallengeHandler()
    page = FakeChallengePage()

    async def scenario() -> None:
        await handler.install(page)
        page.listeners["response"][0](
            FakeResponse(
                "https://example.test/a.png",
                200,
                {"content-type": "image/png"},
                "\x89PNG",
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert handler.observations == ()
