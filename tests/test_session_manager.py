from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from arena_api.auth import verify as verify_mod
from arena_api.auth.manager import (
    AuthenticationError,
    AutoSessionManager,
    CallableSource,
    ChallengeRequiredError,
    CheckpointContext,
    CookieJar,
    CookieRecord,
    FirefoxSource,
    HttpLivenessProbe,
    ManualCheckpoint,
    PlaywrightStorageSource,
    ProbeOutcome,
    RefreshPolicy,
    SessionManager,
    SessionStore,
    SessionStoreError,
    StaticSource,
    looks_like_challenge,
)

FUTURE = time.time() + 3600
PAST = time.time() - 3600


# --------------------------------------------------------------------------- #
# CookieRecord / CookieJar
# --------------------------------------------------------------------------- #


def test_record_accepts_firefox_and_playwright_shapes() -> None:
    firefox = CookieRecord.from_any(
        {
            "name": "sessionid",
            "value": "abc",
            "host": ".arena.ai",
            "path": "/",
            "expiry": FUTURE,
            "isSecure": 1,
            "isHttpOnly": 1,
        }
    )
    assert firefox.domain == "arena.ai"
    assert firefox.secure and firefox.http_only
    assert not firefox.is_expired()

    playwright = CookieRecord.from_any(
        {
            "name": "sessionid",
            "value": "abc",
            "domain": ".arena.ai",
            "expires": -1,
            "httpOnly": True,
            "sameSite": "lax",
        }
    )
    assert playwright.expires is None
    assert playwright.same_site == "Lax"
    assert playwright.as_dict()["expires"] == -1.0


def test_record_expiry_coercion() -> None:
    assert CookieRecord.from_any({"name": "a", "value": "1", "expires": "0"}).expires is None
    micro = CookieRecord.from_any({"name": "a", "value": "1", "expires": int(FUTURE * 1e6)})
    assert micro.expires == pytest.approx(FUTURE, abs=2)
    iso = CookieRecord.from_any(
        {
            "name": "a",
            "value": "1",
            "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FUTURE)),
        }
    )
    assert iso.expires == pytest.approx(FUTURE, abs=2)


def test_record_requires_name_and_sanitizes_path() -> None:
    with pytest.raises(ValueError):
        CookieRecord(name="", value="x")
    record = CookieRecord(name="a", value="b", domain=".WWW.Example.COM", path="acct")
    # Only the leading dot is dropped: www-scope is a real, narrower scope.
    assert record.domain == "www.example.com"
    assert record.path == "/acct"
    with pytest.raises(TypeError):
        CookieRecord.from_any(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_jar_dedupes_by_domain_path_name_and_keeps_insertion_order() -> None:
    jar = CookieJar()
    jar.add({"name": "s", "value": "old", "domain": "arena.ai", "path": "/"})
    jar.add({"name": "s", "value": "new", "domain": "arena.ai", "path": "/"})
    jar.add({"name": "s", "value": "other", "domain": "sub.arena.ai", "path": "/"})
    assert len(jar) == 2
    assert jar.flat("sub.arena.ai") == {"s": "other"}, "the most specific scope wins"
    assert jar.flat("arena.ai") == {"s": "new"}
    assert jar.flat() == {"s": "other"}, "unscoped picks the narrowest scope too"
    assert jar.names == ("s",)
    assert jar.domains() == ("arena.ai", "sub.arena.ai")
    assert jar.names == ("s",)


def test_jar_skips_expired_and_prunes() -> None:
    jar = CookieJar(
        [
            {"name": "live", "value": "1", "expires": FUTURE},
            {"name": "dead", "value": "2", "expires": PAST},
            {"name": "session", "value": "3"},
        ]
    )
    assert jar.flat() == {"live": "1", "session": "3"}, "expiry is enforced by default"
    assert not jar.is_expired()
    assert jar.prune_expired() == 1
    assert jar.names == ("live", "session")
    only_dead = CookieJar([{"name": "dead", "value": "x", "expires": PAST}])
    assert only_dead.is_expired()
    assert only_dead.flat() == {}
    assert CookieJar().is_expired(), "an empty jar is never usable"


def test_cookie_header_orders_by_path_length_and_honours_secure_and_expiry() -> None:
    jar = CookieJar(
        [
            {"name": "root", "value": "r", "domain": "app.test", "path": "/"},
            {"name": "deep", "value": "d", "domain": "app.test", "path": "/api/v1"},
            {"name": "dup", "value": "specific", "domain": "app.test", "path": "/api"},
            {"name": "dup", "value": "generic", "domain": "app.test", "path": "/"},
            {
                "name": "secure_only",
                "value": "s",
                "domain": "app.test",
                "path": "/",
                "secure": True,
            },
            {"name": "gone", "value": "g", "domain": "app.test", "path": "/", "expires": PAST},
        ]
    )
    header = jar.cookie_header("app.test", scheme="https")
    assert header.split("; ")[:2] == ["deep=d", "dup=specific"]
    assert "secure_only=s" in header
    assert "gone" not in header
    http_header = jar.cookie_header("app.test", scheme="http")
    assert "secure_only" not in http_header
    assert jar.cookie_header("elsewhere.test") == ""


def test_jar_expiry_helpers_and_membership() -> None:
    jar = CookieJar(
        [
            {"name": "a", "value": "1", "domain": "arena.ai", "expires": FUTURE + 500},
            {"name": "b", "value": "2", "domain": "arena.ai", "expires": FUTURE + 100},
        ]
    )
    assert jar.earliest_expiry() == pytest.approx(FUTURE + 100)
    assert jar.has_any(["b"])
    assert not jar.has_any(["c"])
    assert jar.value("b") == "2"
    assert jar.get("b", "arena.ai") is not None
    assert jar.get("b", "other.com") is None
    with pytest.raises(ValueError, match="non-empty"):
        CookieJar([{"value": "no name"}])


def test_storage_state_roundtrip_produces_playwright_document() -> None:
    jar = CookieJar([{"name": "s", "value": "v", "domain": "arena.ai", "secure": True}])
    document = jar.to_storage_state()
    assert set(document) == {"cookies", "origins", "arena_api"}
    assert document["cookies"][0]["name"] == "s"
    assert CookieJar.from_storage_state(document).flat() == {"s": "v"}
    assert CookieJar.from_storage_state({"cookies": [{"nope": 1}]}).flat() == {}


# --------------------------------------------------------------------------- #
# Challenge classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body,expected",
    [
        ("<html>Just a Moment...</html>", True),
        ('<div class="cf-turnstile"></div>', True),
        ("captcha-delivery.com", True),
        ('{"ok": true}', False),
        ("", False),
        (None, False),
        (b"<title>Attention Required! | Cloudflare</title>cf-chl-xxx", True),
    ],
)
def test_looks_like_challenge(body: Any, expected: bool) -> None:
    assert looks_like_challenge(body) is expected


def test_cloudflare_html_403_is_a_challenge_but_json_is_not() -> None:
    assert looks_like_challenge("<html>hi</html>", {"cf-mitigated": "challenge", "server": "cloudflare"})
    assert not looks_like_challenge('{"detail":"nope"}', {"server": "cloudflare"})


# --------------------------------------------------------------------------- #
# SessionStore
# --------------------------------------------------------------------------- #


def test_store_saves_with_private_permissions_and_reloads(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "session.json"
    store = SessionStore(target)
    jar = CookieJar([{"name": "s", "value": "v", "domain": "arena.ai", "expires": FUTURE}])
    store.save(jar, metadata={"who": "pytest"})
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    payload = json.loads(target.read_text())
    assert payload["arena_api"]["who"] == "pytest"
    reloaded = store.load()
    assert reloaded.flat() == {"s": "v"}
    assert reloaded.get("s").domain == "arena.ai"  # type: ignore[union-attr]


def test_store_missing_file_is_empty_and_garbage_raises(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "absent.json")
    assert not store.exists()
    assert len(store.load()) == 0
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(SessionStoreError, match="not valid JSON"):
        SessionStore(broken).load()
    (tmp_path / "list.json").write_text("[1,2]")
    with pytest.raises(SessionStoreError, match="JSON object"):
        SessionStore(tmp_path / "list.json").load()


def test_store_leaves_backup_and_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "session.json"
    store = SessionStore(target)
    store.save(CookieJar({"a": "1"}))
    store.save(CookieJar({"b": "2"}))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["session.json", "session.json.bak"]
    assert target.read_text().count('"b"') == 1
    store.clear()
    assert not target.exists() and not Path(str(target) + ".bak").exists()


def test_store_prunes_expired_on_load(tmp_path: Path) -> None:
    target = tmp_path / "session.json"
    SessionStore(target).save(
        CookieJar([{"name": "a", "value": "1", "expires": PAST}, {"name": "b", "value": "2"}])
    )
    assert SessionStore(target).load().names == ("b",)


# --------------------------------------------------------------------------- #
# RefreshPolicy
# --------------------------------------------------------------------------- #


def test_policy_validates_its_numbers() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RefreshPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="backoff_multiplier"):
        RefreshPolicy(backoff_multiplier=0.5)
    with pytest.raises(ValueError, match="jitter"):
        RefreshPolicy(jitter=1.5)
    with pytest.raises(ValueError, match="cookie_ttl"):
        RefreshPolicy(cookie_ttl=0)
    with pytest.raises(ValueError, match="min_interval"):
        RefreshPolicy(min_interval=-1)


def test_policy_backoff_is_capped_and_bounded() -> None:
    policy = RefreshPolicy(retry_backoff=1.0, backoff_multiplier=2.0, backoff_cap=3.0, jitter=0.0)
    assert [policy.next_delay(i) for i in range(5)] == [1.0, 2.0, 3.0, 3.0, 3.0]
    jittered = RefreshPolicy(retry_backoff=1.0, jitter=0.5)
    assert all(1.0 <= jittered.next_delay(0) <= 1.5 for _ in range(200))


def test_policy_timing_helpers() -> None:
    policy = RefreshPolicy(cookie_ttl=10.0, min_interval=5.0)
    assert policy.should_wait(100.0, now=103.0)
    assert not policy.should_wait(100.0, now=106.0)
    assert not policy.should_wait(None, now=0.0)
    assert policy.is_stale(100.0, now=109.9) is False
    assert policy.is_stale(100.0, now=110.0)
    assert policy.is_stale(None)
    assert RefreshPolicy(cookie_ttl=None).is_stale(1.0) is False


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def test_static_and_callable_sources_accept_many_shapes() -> None:
    assert StaticSource({"s": "v"}).fetch(["arena.ai"]).flat() == {"s": "v"}
    assert repr(StaticSource({"s": "v"})).startswith("<StaticSource 1")
    expired = StaticSource([{"name": "s", "value": "v", "expires": PAST}])
    assert expired.fetch([]).flat(now=time.time()) == {}
    assert expired.load_cookies() == {}
    records = [{"name": "s", "value": "v", "host": "arena.ai"}]
    assert CallableSource(lambda domains: records).fetch([]).flat() == {"s": "v"}
    assert CallableSource(lambda domains: {"cookies": records}).fetch([]).flat() == {"s": "v"}
    empty = CallableSource(lambda domains: None).fetch([])
    assert len(empty) == 0 and not empty


def test_callable_source_can_supply_headers() -> None:
    def headers(navigation: bool = False) -> dict[str, str]:
        return {"navigation": "yes" if navigation else "no"}

    source = CallableSource(lambda domains: {"a": "1"}, headers_factory=headers)
    assert source.firefox_headers(navigation=True) == {"navigation": "yes"}


def test_playwright_storage_source_caches_by_mtime(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [{"name": "s", "value": "v", "domain": "arena.ai"}]}))
    source = PlaywrightStorageSource(state)
    assert source.load_cookies(["arena.ai"]) == {"s": "v"}
    first = source._last_mtime
    source.fetch(["arena.ai"])
    assert source._last_mtime == first
    time.sleep(0.01)
    state.write_text(json.dumps({"cookies": [{"name": "s", "value": "v2", "domain": "arena.ai"}]}))
    os.utime(state, (time.time() + 5, time.time() + 5))
    assert source.load_cookies(["arena.ai"]) == {"s": "v2"}
    source.path.unlink()
    with pytest.raises(AuthenticationError, match="not readable"):
        source.fetch(["arena.ai"])
    state.write_text("{oops")
    with pytest.raises(AuthenticationError, match="not valid JSON"):
        source.fetch(["arena.ai"])


def test_firefox_source_prefers_records_and_falls_back() -> None:
    class WithRecords:
        def cookie_records(self, domains=None):
            return [{"name": "s", "value": "v", "host": ".arena.ai", "expiry": FUTURE}]

        def load_cookies(self, domains=None):  # pragma: no cover - must not run
            raise AssertionError("records path should win")

        def firefox_headers(self, *, navigation=False):
            return {"User-Agent": "Firefox/1.0", "navigation": str(navigation)}

    manager = FirefoxSource(WithRecords())
    assert manager.fetch(["arena.ai"]).flat() == {"s": "v"}
    assert manager.firefox_headers(navigation=True) == {
        "User-Agent": "Firefox/1.0",
        "navigation": "True",
    }

    class LegacyOnly:
        def load_cookies(self):  # no domains argument at all
            return {"s": "legacy"}

    legacy = FirefoxSource(LegacyOnly(), fallback_domain="arena.ai")
    assert legacy.fetch([]).flat() == {"s": "legacy"}
    assert legacy.firefox_headers() == {}

    class Broken:
        pass

    with pytest.raises(AuthenticationError, match="neither cookie_records"):
        FirefoxSource(Broken()).fetch(["arena.ai"])


# --------------------------------------------------------------------------- #
# AutoSessionManager
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def counting_source(jar: dict[str, str] | None = None) -> tuple[CallableSource, dict[str, int]]:
    calls = {"n": 0}

    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        calls["n"] += 1
        return dict(jar if jar is not None else {"session": f"v{calls['n']}"})

    return CallableSource(factory), calls


def test_ensure_caches_within_ttl_and_rereads_after_it() -> None:
    clock = FakeClock()
    source, calls = counting_source()
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        policy=RefreshPolicy(cookie_ttl=60.0, min_interval=0.0, liveness_interval=None),
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert manager.ensure().flat() == {"session": "v1"}
    assert manager.ensure().flat() == {"session": "v1"}
    assert calls["n"] == 1
    clock.advance(59)
    manager.ensure()
    assert calls["n"] == 1
    clock.advance(2)
    assert manager.ensure().flat() == {"session": "v2"}
    assert calls["n"] == 2
    assert manager.stats.successful_refreshes == 2


def test_min_interval_throttles_forced_refreshes_but_challenge_bypasses_it() -> None:
    clock = FakeClock()
    source, calls = counting_source()
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        policy=RefreshPolicy(cookie_ttl=None, min_interval=30.0),
        clock=clock,
        sleep=lambda seconds: None,
    )
    manager.refresh()
    manager.refresh()
    assert calls["n"] == 1
    manager.refresh(reason=ChallengeRequiredError("turnstile"))
    assert calls["n"] == 2


def test_refresh_retries_with_backoff_then_raises() -> None:
    attempts: list[float] = []

    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        attempts.append(1)
        raise RuntimeError("cookies.sqlite is locked")

    sleeps: list[float] = []
    manager = AutoSessionManager(
        CallableSource(factory),
        domains=("arena.ai",),
        policy=RefreshPolicy(max_attempts=3, retry_backoff=0.5, jitter=0.0),
        sleep=sleeps.append,
    )
    with pytest.raises(AuthenticationError, match="profile locked|cookie source failed"):
        manager.refresh()
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]
    assert manager.stats.failed_refreshes == 1
    assert "locked" in manager.stats.last_error


def test_transient_source_failure_can_succeed_on_retry() -> None:
    state = {"n": 0}

    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("locked")
        return {"session": "recovered"}

    manager = AutoSessionManager(
        CallableSource(factory),
        domains=("arena.ai",),
        policy=RefreshPolicy(max_attempts=2, retry_backoff=0.0, jitter=0.0),
        sleep=lambda seconds: None,
    )
    assert manager.refresh().flat() == {"session": "recovered"}
    assert manager.stats.successful_refreshes == 1


def test_refresh_session_hook_returns_none_instead_of_raising() -> None:
    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        raise RuntimeError("nope")

    manager = AutoSessionManager(
        CallableSource(factory),
        domains=("arena.ai",),
        policy=RefreshPolicy(max_attempts=1),
        sleep=lambda seconds: None,
    )
    assert manager.refresh_session(RuntimeError("403")) is None
    assert isinstance(manager, SessionManager)


def test_manager_satisfies_protocol_and_exports_conveniences(tmp_path: Path) -> None:
    source = StaticSource(
        [
            {"name": "s", "value": "1", "domain": "arena.ai"},
            {"name": "u", "value": "2", "domain": "www.arena.ai"},
        ],
        headers={"User-Agent": "Firefox/141.0"},
    )
    manager = AutoSessionManager(source, domains=("arena.ai",))
    assert manager.load_cookies() == {"s": "1", "u": "2"}
    # A www-scoped cookie is not sent to the apex host, while an apex cookie is
    # sent to subdomains -- the same asymmetry a browser applies.
    assert manager.load_cookies(["arena.ai"]) == {"s": "1"}
    assert manager.load_cookies(["www.arena.ai"]) == {"s": "1", "u": "2"}
    assert manager.load_cookies(["other.com"]) == {}
    assert manager.firefox_headers() == {"User-Agent": "Firefox/141.0"}
    assert manager.to_curl_cffi() == {
        "arena.ai": {"s": "1"},
        "www.arena.ai": {"u": "2"},
    }
    assert len(manager.cookie_records()) == 2
    target = tmp_path / "state.json"
    document = manager.to_storage_state(target)
    assert document["cookies"][0]["name"] == "s"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text())["origins"] == []
    assert "AutoSessionManager" in repr(manager)


def test_refresh_persists_to_store_and_bootstraps_from_it(tmp_path: Path) -> None:
    target = tmp_path / "session.json"
    store = SessionStore(target)
    clock = FakeClock()
    source, _ = counting_source({"session": "persisted"})
    first = AutoSessionManager(
        source,
        domains=("arena.ai",),
        store=store,
        policy=RefreshPolicy(cookie_ttl=None, min_interval=0.0),
        clock=clock,
        sleep=lambda seconds: None,
    )
    first.refresh()
    assert first.stats.saves == 1
    assert store.load().flat() == {"session": "persisted"}

    restarted = AutoSessionManager(None, domains=("arena.ai",), store=store, clock=clock)
    assert restarted.ensure().flat() == {"session": "persisted"}
    assert restarted.stats.successful_refreshes == 0, "a warm boot is not a refresh"
    assert restarted.jar.names == ("session",)
    clock.advance(10_000)
    assert restarted.ensure().flat() == {"session": "persisted"}, "store-only managers re-read the file"


def test_store_failure_is_logged_not_fatal(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    blocked = tmp_path / "session.json"
    blocked.mkdir()  # a directory where a file must be written: saving cannot work
    source, _ = counting_source({"s": "v"})
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        store=SessionStore(blocked),
        policy=RefreshPolicy(min_interval=0.0),
        sleep=lambda seconds: None,
    )
    assert manager.refresh().flat() == {"s": "v"}
    assert "session not persisted" in capsys.readouterr().out


def test_requires_source_or_store() -> None:
    with pytest.raises(ValueError, match="source"):
        AutoSessionManager()


def test_close_stops_thread_and_persists(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    source, _ = counting_source({"s": "v"})
    manager = AutoSessionManager(
        source, domains=("arena.ai",), store=store, policy=RefreshPolicy(min_interval=0.0),
        sleep=lambda seconds: None,
    )
    manager.close()
    assert store.load().flat() == {}
    with pytest.raises(RuntimeError, match="closed"):
        manager.start_background_refresh(1.0)


def test_background_refresh_loops_until_stopped() -> None:
    counter = {"n": 0}

    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        counter["n"] += 1
        return {"session": f"v{counter['n']}"}

    manager = AutoSessionManager(
        CallableSource(factory),
        domains=("arena.ai",),
        policy=RefreshPolicy(cookie_ttl=0.01, min_interval=0.0),
        sleep=lambda seconds: None,
    )
    thread = manager.start_background_refresh(0.02)
    deadline = time.monotonic() + 2.0
    while counter["n"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert counter["n"] >= 3, "background refresher did not keep polling"
    assert thread.name.startswith("arena-api-session-refresh-")
    assert manager.stop_background_refresh(timeout=2.0) is True
    after = counter["n"]
    time.sleep(0.1)
    assert counter["n"] - after <= 1
    assert manager.stop_background_refresh() is True
    with pytest.raises(ValueError, match="interval"):
        manager.start_background_refresh(0)
    manager.close()


def test_background_refresh_survives_failures(capsys: pytest.CaptureFixture) -> None:
    state = {"n": 0}

    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        state["n"] += 1
        raise RuntimeError("locked")

    manager = AutoSessionManager(
        CallableSource(factory),
        domains=("arena.ai",),
        policy=RefreshPolicy(max_attempts=1),
        sleep=lambda seconds: None,
        output=io.StringIO(),
    )
    thread = manager.start_background_refresh(0.02)
    deadline = time.monotonic() + 2.0
    while state["n"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state["n"] >= 2 and thread.is_alive()
    manager.stop_background_refresh(timeout=2.0)


# --------------------------------------------------------------------------- #
# Probing and the human checkpoint
# --------------------------------------------------------------------------- #


class StubProbe:
    def __init__(self, *states: ProbeOutcome) -> None:
        self.outcomes = list(states)
        self.seen: list[CookieJar] = []

    def __call__(self, jar: CookieJar) -> ProbeOutcome:
        self.seen.append(jar)
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


def test_check_raises_without_probe_and_records_outcome() -> None:
    source, _ = counting_source()
    manager = AutoSessionManager(source, domains=("arena.ai",), sleep=lambda s: None)
    with pytest.raises(AuthenticationError, match="no liveness probe"):
        manager.check()
    probe = StubProbe(ProbeOutcome("alive", "", 200))
    manager.probe = probe
    assert manager.refresh().flat() == {"session": "v1"}
    outcome = manager.check()
    assert outcome.alive and str(outcome) == "alive (HTTP 200)"
    assert outcome.as_dict()["state"] == "alive"
    assert manager.stats.probes == 1
    assert manager.last_probe is outcome


def test_liveness_interval_forces_a_reprobe_then_a_refresh() -> None:
    clock = FakeClock()
    source, calls = counting_source()
    probe = StubProbe(ProbeOutcome("expired", "HTTP 403", 403), ProbeOutcome("alive", "", 200))
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        probe=probe,
        policy=RefreshPolicy(cookie_ttl=None, min_interval=0.0, liveness_interval=10.0),
        clock=clock,
        sleep=lambda s: None,
    )
    manager.ensure()
    assert calls["n"] == 1  # ensure() has no probe verdict yet -> refresh path
    clock.advance(11)
    assert manager.ensure().flat() == {"session": "v2"}
    assert calls["n"] == 2


def test_challenge_without_checkpoint_raises_challenge_error() -> None:
    source, _ = counting_source()
    probe = StubProbe(ProbeOutcome("challenge", "interstitial", 403))
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        probe=probe,
        policy=RefreshPolicy(verify_after_refresh=True, max_attempts=2),
        sleep=lambda s: None,
    )
    with pytest.raises(ChallengeRequiredError, match="does not bypass"):
        manager.refresh()
    assert manager.stats.last_state == "challenge"


def test_checkpoint_waits_for_human_then_recovers_once() -> None:
    source, _ = counting_source()
    prompts: list[CheckpointContext] = []

    class HumanWaiter:
        def __call__(self, context) -> CookieJar:
            prompts.append(context)
            return CookieJar({"session": "after-human-check"})  # no domain on purpose

    probe = StubProbe(
        ProbeOutcome("challenge", "interstitial", 403),
        ProbeOutcome("alive", "", 200),
        ProbeOutcome("alive", "", 200),
    )
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        probe=probe,
        checkpoint=HumanWaiter(),
        policy=RefreshPolicy(verify_after_refresh=True, max_attempts=3),
        sleep=lambda s: None,
    )
    jar = manager.refresh()
    assert jar.flat() == {"session": "after-human-check"}
    assert len(prompts) == 1, "a human must not be prompted repeatedly per refresh"
    assert prompts[0].host == "arena.ai"
    assert prompts[0].url == "https://arena.ai/"
    assert manager.stats.checkpoints == 1


def test_still_walled_after_checkpoint_keeps_raising() -> None:
    class UselessWaiter:
        def __call__(self, context) -> CookieJar:
            return CookieJar({"session": "still-blocked"})

    manager = AutoSessionManager(
        StaticSource({"session": "x"}),
        domains=("arena.ai",),
        probe=StubProbe(ProbeOutcome("challenge", "interstitial", 403)),
        checkpoint=UselessWaiter(),
        policy=RefreshPolicy(verify_after_refresh=True, max_attempts=2),
        sleep=lambda s: None,
    )
    with pytest.raises(ChallengeRequiredError, match="does not bypass"):
        manager.refresh()
    assert manager.stats.checkpoints == 1, "one human prompt per refresh, never a loop"


def test_expired_probe_verdict_after_refresh_raises_auth_error() -> None:
    manager = AutoSessionManager(
        StaticSource({"session": "stale"}),
        domains=("arena.ai",),
        probe=StubProbe(ProbeOutcome("expired", "HTTP 403", 403)),
        policy=RefreshPolicy(verify_after_refresh=True, max_attempts=2),
        sleep=lambda s: None,
    )
    with pytest.raises(AuthenticationError, match="rejected"):
        manager.refresh()


def test_transient_probe_verdict_keeps_the_session() -> None:
    clock = FakeClock()
    source, calls = counting_source()
    probe = StubProbe(ProbeOutcome("unavailable", "connection reset"))
    manager = AutoSessionManager(
        source,
        domains=("arena.ai",),
        probe=probe,
        policy=RefreshPolicy(cookie_ttl=None, min_interval=0.0, liveness_interval=5.0),
        clock=clock,
        sleep=lambda s: None,
    )
    manager.ensure()
    calls_before = calls["n"]
    clock.advance(6)
    manager.ensure()
    assert calls["n"] == calls_before, "a network hiccup must not invalidate a session"
    assert manager.last_probe is not None and manager.last_probe.is_transient
    assert manager.last_probe.needs_human is False


def test_manual_checkpoint_prints_waits_and_rereads(tmp_path: Path) -> None:
    buffer = io.StringIO()
    source, calls = counting_source()
    fired: list[str] = []

    def fake_input(prompt: str) -> str:
        fired.append(prompt)
        return ""

    checkpoint = ManualCheckpoint(
        source=source,
        domains=("arena.ai",),
        interactive=True,
        input_fn=fake_input,
        output=buffer,
    )
    jar = checkpoint(
        CheckpointContext(domains=("arena.ai",), reason="expired", output=buffer)
    )
    assert jar.flat() == {"session": "v1"}
    assert fired == [""], "the wait is a single prompt, not a polling loop"
    written = buffer.getvalue()
    assert "https://arena.ai/" in written and "Enter" in written

    silent = ManualCheckpoint(source=source, interactive=False, output=buffer)
    before = calls["n"]
    silent(CheckpointContext(domains=("arena.ai",), interactive=True))
    assert calls["n"] == before + 1, "interactive=False must not block on a terminal"


def test_manual_checkpoint_without_source_returns_empty_jar() -> None:
    checkpoint = ManualCheckpoint(prompt="nothing to do", output=io.StringIO())
    assert len(checkpoint(CheckpointContext(domains=(), interactive=False))) == 0


# --------------------------------------------------------------------------- #
# The HTTP probe itself (no sockets: a fake opener)
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body.encode()
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._body[:size] if size and size > 0 else self._body

    def close(self) -> None:
        return None

    def info(self) -> dict[str, str]:
        return dict(self.headers)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def probe_with(status: int, body: str = "", headers: dict[str, str] | None = None):
    from urllib.error import HTTPError

    captured: dict[str, Any] = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        if status >= 400:
            raise HTTPError(request.full_url, status, "denied", headers or {}, FakeResponse(status, body).__enter__())
        return FakeResponse(status, body, headers)

    return HttpLivenessProbe("https://arena.ai/api/me", opener=opener), captured


def test_http_probe_sends_cookie_header_and_identifies_itself() -> None:
    probe, captured = probe_with(200)
    jar = CookieJar(
        [
            {"name": "session", "value": "v", "domain": "arena.ai", "path": "/"},
            {"name": "skipme", "value": "x", "domain": "other.com"},
        ]
    )
    assert probe(jar).state == "alive"
    request = captured["request"]
    assert request.get_header("Cookie") == "session=v"
    assert request.get_header("User-agent") == "arena-api/0.2.0 (session liveness probe)"
    assert captured["timeout"] == 10.0
    with pytest.raises(ValueError, match="absolute http"):
        HttpLivenessProbe("arena.ai/api/me")


def test_http_probe_classifies_walls_and_errors() -> None:
    probe, _ = probe_with(403, "<html>Just a Moment...</html>")
    jar = CookieJar({"session": "v"})
    assert probe(jar).state == "challenge"
    probe, _ = probe_with(403, '{"detail":"permission denied"}')
    assert probe(jar).state == "expired"
    probe, _ = probe_with(429, "")
    assert probe(jar).state == "unavailable"
    probe, _ = probe_with(503, "")
    outcome = probe(jar)
    assert outcome.state == "unavailable" and outcome.status == 503
    assert str(outcome).startswith("unavailable (HTTP 503")

    def broken(request, timeout):
        raise OSError("connection reset")

    flaky = HttpLivenessProbe("https://arena.ai/api/me", opener=broken)
    assert flaky(jar).state == "unavailable"


def test_probe_reuses_live_headers_when_status_is_ok_but_body_is_a_wall() -> None:
    seen: dict[str, Any] = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        return FakeResponse(200, "<script src='https://challenges.cloudflare.com/turnstile.js'>")

    probe = HttpLivenessProbe("https://arena.ai/", opener=opener)
    outcome = probe(CookieJar({"session": "v"}))
    assert outcome.state == "challenge" and seen["url"].endswith("/")
    assert outcome.status == 200, "a wall served with HTTP 200 is still a wall"


# --------------------------------------------------------------------------- #
# verify.py: guards only (no browser is launched here)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", True),
        ("http://localhost:8000/login", True),
        ("127.0.0.42", True),
        ("10.1.2.3", True),
        ("192.168.0.9", True),
        ("172.16.0.1", True),
        ("172.32.0.1", False),
        ("myapp.test", True),
        ("svc.internal", True),
        ("api.example", True),
        ("example.com", False),
        ("arena.ai", False),
        ("", False),
    ],
)
def test_allowed_test_hosts(host: str, expected: bool) -> None:
    assert verify_mod.is_allowed_test_host(host) is expected


def test_allowlist_extends_explicitly_and_via_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert verify_mod.is_allowed_test_host("staging.myapp.dev") is False
    assert verify_mod.is_allowed_test_host("staging.myapp.dev", ("myapp.dev",)) is True
    assert verify_mod.is_allowed_test_host("https://login.myapp.dev/x", ("myapp.dev",)) is True
    assert verify_mod.is_allowed_test_host("evil-myapp.dev", ("myapp.dev",)) is False
    monkeypatch.setenv(verify_mod.ALLOWED_HOSTS_ENV_VAR, "qa.other.app , https://x.third.app")
    assert verify_mod.is_allowed_test_host("qa.other.app") is True
    assert verify_mod.is_allowed_test_host("x.third.app") is True
    monkeypatch.delenv(verify_mod.ALLOWED_HOSTS_ENV_VAR)
    assert verify_mod.is_allowed_test_host("qa.other.app") is False


def test_capture_refuses_unauthorized_origin_before_touching_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> None:  # pragma: no cover - must never run
        raise AssertionError("playwright must not be imported for a refused origin")

    monkeypatch.setattr(verify_mod, "import_playwright", explode)
    with pytest.raises(verify_mod.VerificationError, match="authorized"):
        verify_mod.capture_storage_state(
            "https://arena.ai/login", save_to=Path("/tmp/nope.json")
        )
    with pytest.raises(verify_mod.VerificationError, match="derive a host"):
        verify_mod.capture_storage_state("not-a-url", save_to=Path("/tmp/nope.json"))


def test_capture_validates_timeouts() -> None:
    with pytest.raises(ValueError, match="timeout"):
        verify_mod.capture_storage_state(
            "http://localhost/login", save_to="/tmp/x.json", timeout=0
        )
    with pytest.raises(ValueError, match="poll_interval"):
        verify_mod.capture_storage_state(
            "http://localhost/login", save_to="/tmp/x.json", poll_interval=0
        )


def test_import_playwright_gives_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("playwright"):
            raise ImportError("no module named playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "playwright", raising=False)
    monkeypatch.delitem(sys.modules, "playwright.sync_api", raising=False)
    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(verify_mod.PlaywrightNotAvailable, match=r"playwright install chromium"):
        verify_mod.import_playwright()


def test_wait_for_cookie_names_polls_without_interacting() -> None:
    class CookieReader:
        def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
            self.batches = batches
            self.calls = 0

        def cookies(self, url: str) -> list[dict[str, Any]]:
            self.calls += 1
            return self.batches[min(self.calls - 1, len(self.batches) - 1)]

    reader = CookieReader(
        [[], [{"name": "other", "value": "x"}], [{"name": "sessionid", "value": "v"}]]
    )
    found = verify_mod.wait_for_cookie_names(
        reader, "http://localhost/", ["sessionid"], timeout=5.0, poll_interval=0.001
    )
    assert found == {"sessionid": {"name": "sessionid", "value": "v"}}
    assert reader.calls == 3

    empty = CookieReader([[]])
    assert (
        verify_mod.wait_for_cookie_names(
            empty, "http://localhost/", ["sessionid"], timeout=0.01, poll_interval=0.001
        )
        == {}
    )
    assert verify_mod.wait_for_cookie_names(empty, "http://localhost/", [], timeout=0.01) == {}

    class Exploding:
        def cookies(self, url: str) -> list[dict[str, Any]]:
            raise RuntimeError("context closed")

    with pytest.raises(verify_mod.VerificationError, match="cannot read cookies"):
        verify_mod.wait_for_cookie_names(Exploding(), "http://localhost/", ["s"], timeout=0.01)


def test_verification_result_exposes_a_jar() -> None:
    result = verify_mod.VerificationResult(
        storage_state_path=Path("/tmp/s.json"),
        origin="localhost",
        cookie_names=("sessionid",),
        cookies=({"name": "sessionid", "value": "v", "domain": "localhost"},),
        waited_seconds=1.5,
    )
    assert result.cookie_count == 1
    assert result.jar().flat() == {"sessionid": "v"}
    assert result.as_dict()["completed_by"] == "human"
    assert "localhost" in repr(result)


# --------------------------------------------------------------------------- #
# The one browser-facing entry point: it must stay an *unmodified* browser
# --------------------------------------------------------------------------- #


class _FakePage:
    def __init__(self, trace: dict[str, Any]) -> None:
        self.trace = trace

    def goto(self, url: str, **kwargs: Any) -> None:
        self.trace["goto"] = (url, kwargs)

    def add_init_script(self, script: str = "", **kwargs: Any) -> None:
        raise AssertionError(f"stealth injection is not allowed: {script[:60]!r}")

    def evaluate(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the browser must not be scripted by this module")

    def click(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("this module never clicks anything")

    def close(self) -> None:
        self.trace["closed"] = True


class _FakeContext:
    def __init__(self, trace: dict[str, Any], cookies: list[dict[str, Any]]) -> None:
        self.trace = trace
        self._cookies = cookies

    def new_page(self) -> _FakePage:
        return _FakePage(self.trace)

    def cookies(self, url: str = "") -> list[dict[str, Any]]:
        self.trace["cookie_reads"] = self.trace.get("cookie_reads", 0) + 1
        return list(self._cookies)

    def storage_state(self, path: str | None = None, **kwargs: Any) -> dict[str, Any]:
        document = {"cookies": list(self._cookies), "origins": []}
        if path:
            Path(path).write_text(json.dumps(document), encoding="utf-8")
        return document

    def add_init_script(self, script: str = "", **kwargs: Any) -> None:
        raise AssertionError(f"stealth injection is not allowed: {script[:60]!r}")

    def set_extra_http_headers(self, headers: Any) -> None:
        raise AssertionError("headers must not be overridden")

    def close(self) -> None:
        self.trace["closed"] = True


class _FakeBrowser:
    def __init__(self, trace: dict[str, Any], cookies: list[dict[str, Any]]) -> None:
        self.trace = trace
        self._cookies = cookies

    def new_context(self, **kwargs: Any) -> _FakeContext:
        self.trace["context_options"] = kwargs
        return _FakeContext(self.trace, self._cookies)

    def close(self) -> None:
        self.trace["closed"] = True


class _FakeChromium:
    def __init__(self, trace: dict[str, Any], cookies: list[dict[str, Any]]) -> None:
        self.trace = trace
        self._cookies = cookies

    def launch(self, **kwargs: Any) -> _FakeBrowser:
        self.trace["launch"] = kwargs
        return _FakeBrowser(self.trace, self._cookies)


class _FakePlaywright:
    def __init__(self, trace: dict[str, Any], cookies: list[dict[str, Any]]) -> None:
        self.chromium = _FakeChromium(trace, cookies)
        self.trace = trace

    def stop(self) -> None:
        self.trace["stopped"] = True


def install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch, cookies: list[dict[str, Any]]
) -> dict[str, Any]:
    """Patch a recording fake into ``playwright.sync_api``."""
    import types

    trace: dict[str, Any] = {}
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda *a, **k: types.SimpleNamespace(  # type: ignore[attr-defined]
        start=lambda: _FakePlaywright(trace, cookies)
    )
    package = types.ModuleType("playwright")
    package.sync_api = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return trace


def test_capture_uses_a_visible_unmodified_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = [{"name": "sessionid", "value": "v", "domain": "localhost", "path": "/"}]
    trace = install_fake_playwright(monkeypatch, cookies)
    target = tmp_path / "state.json"

    result = verify_mod.capture_storage_state(
        "http://localhost:8000/login",
        save_to=target,
        interactive=False,
        prompt="",
        output=io.StringIO(),
    )

    assert trace["launch"] == {"headless": False}, "must be a real, visible browser"
    assert trace["context_options"] == {}, "no UA/locale/timezone overrides"
    assert trace["goto"][0] == "http://localhost:8000/login"
    assert result.cookie_names == ("sessionid",)
    assert result.jar().flat() == {"sessionid": "v"}
    assert json.loads(target.read_text())["cookies"] == cookies
    assert result.as_dict()["completed_by"] == "human"


def test_capture_can_finish_on_a_cookie_without_any_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookies = [{"name": "sessionid", "value": "v", "domain": "localhost", "path": "/"}]
    trace = install_fake_playwright(monkeypatch, cookies)
    result = verify_mod.capture_storage_state(
        "http://localhost:8000/login",
        save_to=tmp_path / "state.json",
        wait_for_cookie=("sessionid",),
        poll_interval=0.001,
        timeout=5.0,
        interactive=False,
        prompt="",
        output=io.StringIO(),
    )
    assert result.cookies == tuple(cookies)
    assert trace["context_options"] == {}
    assert trace["closed"] is True


def test_checkpoint_hands_over_and_returns_captured_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_playwright(
        monkeypatch, [{"name": "s", "value": "v", "domain": "localhost", "path": "/"}]
    )
    checkpoint = verify_mod.PlaywrightCheckpoint(
        "http://localhost:8000/login",
        storage_state=tmp_path / "state.json",
        wait_for_cookie=("s",),
        poll_interval=0.001,
        interactive=False,
        prompt="",
    )
    jar = checkpoint(
        CheckpointContext(domains=("localhost",), reason="challenge", output=io.StringIO())
    )
    assert jar.flat() == {"s": "v"}
    assert "PlaywrightCheckpoint" in repr(checkpoint)

    refused = verify_mod.PlaywrightCheckpoint(
        "https://third-party.example.com/login", storage_state=tmp_path / "other.json"
    )
    with pytest.raises(verify_mod.VerificationError, match="authorized"):
        refused(CheckpointContext(domains=("third-party.example.com",)))


def test_capture_writes_the_state_file_privately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_playwright(
        monkeypatch, [{"name": "s", "value": "v", "domain": "localhost", "path": "/"}]
    )
    target = tmp_path / "sub" / "state.json"
    verify_mod.capture_storage_state(
        "http://localhost/login",
        save_to=target,
        wait_for_cookie=("nope",),
        timeout=0.05,
        poll_interval=0.01,
        interactive=False,
        prompt="",
        output=io.StringIO(),
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Integration: the manager as a real ``session_source`` for ArenaClient
# --------------------------------------------------------------------------- #


CLIENT_CONFIG = {
    "dependencies": [{"fn_index": 4, "api_name": "chat", "queue": True}],
    "components": [
        {"type": "dropdown", "props": {"label": "Model", "choices": [["m1", "One"]]}}
    ],
}


def _response(request: Any, status: int, body: bytes, content_type: str = "application/json") -> Any:
    import httpx

    return httpx.Response(
        status, request=request, headers={"content-type": content_type}, content=body
    )


def _client(manager: AutoSessionManager, handler: Any) -> Any:
    import httpx

    from arena_api import ArenaClient

    return ArenaClient(
        base_url="https://arena.test",
        session_source=manager,
        http_transport=httpx.MockTransport(handler),
    )


def _manager(tmp_path: Path, jar: dict[str, str]) -> tuple[AutoSessionManager, dict[str, int]]:
    state = {"n": 0}

    def factory(domains: tuple[str, ...]) -> dict[str, str]:
        state["n"] += 1
        return dict(jar) if state["n"] == 1 else {"session": f"refreshed-{state['n']}"}

    manager = AutoSessionManager(
        CallableSource(factory),
        domains=("arena.test",),
        store=SessionStore(tmp_path / "session.json"),
        policy=RefreshPolicy(cookie_ttl=None, min_interval=0.0, max_attempts=2, jitter=0.0),
        sleep=lambda seconds: None,
    )
    return manager, state


def test_client_uses_manager_cookies_and_headers(tmp_path: Path) -> None:
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return _response(request, 200, json.dumps(CLIENT_CONFIG).encode())

    manager, _ = _manager(tmp_path, {"session": "initial"})
    manager = AutoSessionManager(
        StaticSource({"session": "initial"}, headers={"User-Agent": "Firefox/141.0", "Sec-GPC": "1"}),
        domains=("arena.test",),
    )
    client = _client(manager, handler)
    try:
        assert [model.id for model in client.get_models()] == ["m1"]
        assert "session=initial" in seen[0].headers.get("cookie")
        assert seen[0].headers.get("user-agent") == "Firefox/141.0"
        assert seen[0].headers.get("sec-gpc") == "1"
    finally:
        client.close()


def test_challenge_403_asks_the_manager_to_reread_and_retries_once(tmp_path: Path) -> None:
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        if len(seen) == 1:
            return _response(
                request,
                403,
                b"<html><body>Just a Moment...</body></html>",
                "text/html",
            )
        return _response(request, 200, json.dumps(CLIENT_CONFIG).encode())

    manager, state = _manager(tmp_path, {"session": "stale"})
    client = _client(manager, handler)
    try:
        assert [model.id for model in client.get_models()] == ["m1"]
    finally:
        client.close()
    assert len(seen) == 2, "the client retries once with the refreshed session"
    assert "session=refreshed-2" in seen[1].headers.get("cookie")
    assert state["n"] == 2
    # The wall was *detected*, never satisfied: no browser, no clicking.
    assert manager.stats.checkpoints == 0


def test_unrecoverable_challenge_surfaces_to_the_caller(tmp_path: Path) -> None:
    from arena_api import ChallengeRequiredError

    def handler(request: Any) -> Any:
        return _response(
            request, 403, b"<html>cf-chl-gen turnstile</html>", "text/html"
        )

    manager, _ = _manager(tmp_path, {"session": "stale"})
    client = _client(manager, handler)
    try:
        with pytest.raises(ChallengeRequiredError, match="does not bypass|verification"):
            client.get_models()
    finally:
        client.close()
    assert manager.stats.checkpoints == 0
    assert manager.last_probe is None, "no probe was configured: nothing was guessed"


def test_state_file_written_after_refresh_and_is_private(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path, {"session": "initial"})
    manager.refresh()
    target = tmp_path / "session.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    payload = json.loads(target.read_text())
    assert payload["cookies"][0]["name"] == "session"
    assert payload["cookies"][0]["domain"] == "arena.test"
