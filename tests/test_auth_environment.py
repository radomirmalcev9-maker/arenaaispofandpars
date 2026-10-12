"""Tests for the determinism init script and browser-environment wiring."""

from __future__ import annotations

import json

import pytest

from arena_api.auth.environment import (
    ANIMATION_FREEZE_CSS,
    DEFAULT_INIT_SCRIPT,
    HIDE_WEBDRIVER_SCRIPT,
    DeterminismOptions,
    build_init_script,
)
from arena_api.auth.browser import (
    SYNTHETIC_MONITOR_HEADER,
    BrowserEnvironment,
    BrowserEnvironmentOptions,
)

# Marker strings that would indicate a stealth / anti-detection patch.
STEALTH_MARKERS = (
    "navigator.webdriver",
    "navigator.plugins",
    "navigator.mimeTypes",
    "UNMASKED_VENDOR_WEBGL",
    "UNMASKED_RENDERER_WEBGL",
    "getParameter",
    "toDataURL",
    "permissions.query",
    "window.chrome",
    "Notification.permission",
    "oncdpconnection",
)


def test_default_script_is_stable_and_non_empty() -> None:
    assert len(DEFAULT_INIT_SCRIPT) > 500
    assert build_init_script(DeterminismOptions()) == DEFAULT_INIT_SCRIPT


def test_script_contains_no_stealth_or_fingerprint_spoofing() -> None:
    script = build_init_script(
        DeterminismOptions(
            freeze_animations=True,
            finish_web_animations=True,
            deterministic_random=True,
            anchor_clock=True,
            identify_automation=True,
        )
    )
    for marker in STEALTH_MARKERS:
        assert marker not in script, f"init script must not contain {marker!r}"


def test_the_default_script_stays_honest() -> None:
    """The webdriver mask is opt-in, so it must never leak into the default.

    ``test_script_contains_no_stealth_or_fingerprint_spoofing`` already checks
    the markers; this pins the reason: the mask exists, and is downloadable on
    purpose (the sign-in window), not by accident.
    """
    assert "webdriver" in HIDE_WEBDRIVER_SCRIPT
    assert "webdriver" not in DEFAULT_INIT_SCRIPT
    assert "webdriver" not in build_init_script(DeterminismOptions(identify_automation=False))


def test_the_webdriver_mask_survives_a_refusing_delete() -> None:
    """A hardened build can throw on ``delete``; the script must still mask."""
    assert "try {" in HIDE_WEBDRIVER_SCRIPT
    assert "Object.defineProperty" in HIDE_WEBDRIVER_SCRIPT
    assert "configurable: true" in HIDE_WEBDRIVER_SCRIPT


def test_script_freezes_animations_and_caret() -> None:
    script = build_init_script(DeterminismOptions(freeze_animations=True))
    assert "animation-duration" in script
    assert "transition-duration" in script
    assert "caret-color" in script
    assert "0.001s" in script


def test_freeze_animations_can_be_disabled() -> None:
    script = build_init_script(DeterminismOptions(freeze_animations=False))
    assert "animation-duration" not in script


def test_animation_freeze_css_keeps_event_listeners_alive() -> None:
    # Zeroing durations (rather than disabling) keeps transitionend firing.
    assert "animation: none" not in ANIMATION_FREEZE_CSS
    assert "0.001s" in ANIMATION_FREEZE_CSS


def test_deterministic_random_installs_seeded_prng() -> None:
    script = build_init_script(
        DeterminismOptions(deterministic_random=True, random_seed=4242)
    )
    assert "Math.random = function" in script
    assert "(4242)" in script


def test_random_can_be_left_alone() -> None:
    script = build_init_script(DeterminismOptions(deterministic_random=False))
    assert "Math.random = function" not in script


def test_clock_anchor_uses_explicit_instant() -> None:
    script = build_init_script(
        DeterminismOptions(anchor_clock=True, frozen_time="2026-01-01T00:00:00Z")
    )
    assert "PatchedDate" in script
    assert "2026-01-01T00:00:00Z" in script
    # Timers must keep working: performance.now() is deliberately untouched.
    assert "performance.now" not in script


def test_clock_anchor_defaults_to_epoch() -> None:
    script = build_init_script(DeterminismOptions(anchor_clock=True))
    assert "PatchedDate" in script
    assert "(0)" in script


def test_clock_not_anchored_by_default() -> None:
    assert "PatchedDate" not in DEFAULT_INIT_SCRIPT


def test_automation_is_identified_by_default() -> None:
    script = DEFAULT_INIT_SCRIPT
    assert "__arenaSynthetic" in script
    assert "arena-api" in script


def test_identify_automation_can_be_disabled() -> None:
    script = build_init_script(DeterminismOptions(identify_automation=False))
    assert "__arenaSynthetic" not in script


def test_hooks_expose_settle_and_font_waiting() -> None:
    script = DEFAULT_INIT_SCRIPT
    assert "__arenaTestHooks" in script
    assert "waitForFonts" in script
    assert "requestAnimationFrame" in script
    assert "performance.mark" in script


def test_extra_hooks_are_injected_as_json() -> None:
    options = DeterminismOptions(extra_hooks={"build": "ci-42", "shard": 3})
    script = build_init_script(options)
    assert '"build": "ci-42"' in script
    assert '"shard": 3' in script


def test_hook_payload_is_json_serialisable() -> None:
    payload = DeterminismOptions(extra_hooks={"a": 1}).to_hook_payload()
    assert json.loads(json.dumps(payload))["random_seed"] == 1337


# --------------------------------------------------------------------------- #
# Browser environment options
# --------------------------------------------------------------------------- #


def test_context_kwargs_cover_locale_timezone_and_viewport() -> None:
    options = BrowserEnvironmentOptions(
        locale="de-DE",
        timezone_id="Europe/Berlin",
        viewport={"width": 1280, "height": 720},
    )
    kwargs = options.context_kwargs()
    assert kwargs["locale"] == "de-DE"
    assert kwargs["timezone_id"] == "Europe/Berlin"
    assert kwargs["viewport"] == {"width": 1280, "height": 720}
    assert kwargs["reduced_motion"] == "reduce"
    # navigator.languages is set by Playwright's locale option, not by a patch.
    assert "add_init_script" not in kwargs


def test_context_kwargs_announce_synthetic_monitoring() -> None:
    kwargs = BrowserEnvironmentOptions().context_kwargs()
    headers = kwargs["extra_http_headers"]
    assert headers[SYNTHETIC_MONITOR_HEADER].startswith("arena-api/")


def test_synthetic_header_can_be_overridden() -> None:
    options = BrowserEnvironmentOptions(
        extra_http_headers={SYNTHETIC_MONITOR_HEADER: "my-monitor/1.0"}
    )
    headers = options.context_kwargs()["extra_http_headers"]
    assert headers[SYNTHETIC_MONITOR_HEADER] == "my-monitor/1.0"


def test_identify_automation_can_be_turned_off() -> None:
    kwargs = BrowserEnvironmentOptions(identify_automation=False).context_kwargs()
    assert SYNTHETIC_MONITOR_HEADER not in kwargs.get("extra_http_headers", {})


def test_no_viewport_mode() -> None:
    kwargs = BrowserEnvironmentOptions(viewport=None).context_kwargs()
    assert kwargs["no_viewport"] is True


def test_launch_args_are_container_safe_by_default() -> None:
    kwargs = BrowserEnvironmentOptions().launch_kwargs()
    assert "--disable-dev-shm-usage" in kwargs["args"]
    assert kwargs["chromium_sandbox"] is True
    assert kwargs["headless"] is True


def test_disabling_the_sandbox_is_opt_in() -> None:
    kwargs = BrowserEnvironmentOptions(no_sandbox=True).launch_kwargs()
    assert kwargs["chromium_sandbox"] is False
    assert "--no-sandbox" not in kwargs["args"]


def test_artifacts_require_a_directory() -> None:
    environment = BrowserEnvironment(BrowserEnvironmentOptions())
    assert environment.artifacts_dir is None
    with pytest.raises(RuntimeError):
        environment._artifact_path("trace.zip")


def test_artifacts_dir_is_created_on_demand(tmp_path) -> None:
    environment = BrowserEnvironment(
        BrowserEnvironmentOptions(artifacts_dir=tmp_path / "nested")
    )
    path = environment._artifact_path("shot.png")
    assert path.parent.is_dir()
    assert path.name == "shot.png"


def test_unstarted_environment_reports_state() -> None:
    environment = BrowserEnvironment()
    assert environment.started is False
    with pytest.raises(RuntimeError):
        _ = environment.browser


def test_stamp_is_utc_and_sortable() -> None:
    first = BrowserEnvironment.stamp()
    second = BrowserEnvironment.stamp()
    assert first.endswith("Z")
    assert first <= second
