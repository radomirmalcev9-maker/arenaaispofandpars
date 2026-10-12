"""Impersonation: sending a session with the browser's own TLS/HTTP2 dialect.

Three layers are worth pinning, and only the last one needs the dependency:

* the *policy* -- which fingerprint a client should present, given where the
  session came from, and what happens when the optional extra is missing;
* the *wiring* -- that a client, sync or async, really builds the curl_cffi
  transport when asked (checked against a recording fake);
* the *wire* -- that a real request through that transport still carries the
  headers and cookies we set, and that SSE still arrives incrementally.

The wire tests skip when ``httpx-curl-cffi`` is absent, which is the same
posture the rest of the suite takes towards ``curl_cffi``.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import io
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from arena_api.auth.firefox_session import FirefoxSessionManager
from arena_api.auth.session import BrowserSessionManager
from arena_api.auto import (
    AutoOptions,
    DiscoveredSession,
    explain_impersonation,
    impersonation_profile,
    profile_for_source,
)
from arena_api.client import (
    ArenaClient,
    AsyncArenaClient,
    _resolve_session_source,
    resolve_impersonation,
)
from arena_api.direct import AsyncDirectArenaClient, DirectArenaClient
from arena_api.exceptions import ArenaError
from arena_api.transport import (
    IMPERSONATE_EXTRA,
    ArenaTransport,
    AsyncArenaTransport,
    build_impersonation_transport,
    impersonation_available,
)


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


class RecordingTransport:
    """Stands in for CurlTransport/AsyncCurlTransport."""

    instances: list["RecordingTransport"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingTransport.instances.append(self)

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass

    async def aclose(self) -> None:  # pragma: no cover - nothing to release
        pass


def _visible_module(monkeypatch, name: str, module: types.ModuleType) -> None:
    """Make ``name`` look importable (``importlib.util.find_spec`` agrees)."""
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def fake_curl_cffi(monkeypatch) -> type[RecordingTransport]:
    """Install a fake ``httpx_curl_cffi`` and record what gets built.

    ``curl_cffi`` is faked alongside it: the library only impersonates when
    *both* halves of the optional extra are importable, so a test that wants the
    derived default to fire must not depend on what the machine happens to have.
    """
    RecordingTransport.instances = []
    module = types.ModuleType("httpx_curl_cffi")
    module.CurlTransport = RecordingTransport  # type: ignore[attr-defined]
    module.AsyncCurlTransport = RecordingTransport  # type: ignore[attr-defined]
    _visible_module(monkeypatch, "httpx_curl_cffi", module)
    _visible_module(monkeypatch, "curl_cffi", types.ModuleType("curl_cffi"))
    return RecordingTransport


@pytest.fixture
def hidden_curl_cffi(monkeypatch):
    """Make the optional dependency look absent, whatever the machine has.

    ``sys.modules[name] = None`` is exactly what a failed import leaves behind,
    so both ``import`` and ``find_spec`` treat it as missing.
    """
    for name in ("httpx_curl_cffi", "curl_cffi"):
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def firefox_env(tmp_path, monkeypatch):
    """Firefox sessions that answer without a profile on disk.

    The *class* keeps its identity -- only its two I/O methods are replaced --
    because the client recognises a Firefox source by type, and a test that
    swapped the class for a factory would hide that.
    """
    monkeypatch.setattr(
        FirefoxSessionManager,
        "load_cookies",
        lambda self, domains=None: {"arena-auth-prod-v1.0": "x"},
    )
    monkeypatch.setattr(
        FirefoxSessionManager,
        "firefox_headers",
        lambda self, *, navigation=False: {"User-Agent": "Firefox/157.0.1"},
    )

    def build() -> FirefoxSessionManager:
        return FirefoxSessionManager(domains=("arena.ai",), profiles_root=tmp_path)

    return build


# --------------------------------------------------------------------------- #
# Policy: which fingerprint, and when
# --------------------------------------------------------------------------- #


def test_no_request_means_follow_the_source(fake_curl_cffi) -> None:
    assert resolve_impersonation(None, "firefox") == "firefox"
    assert resolve_impersonation(None, None) is None


def test_the_derived_default_never_breaks_a_working_setup(hidden_curl_cffi) -> None:
    """Without the optional extra, ``None`` must stay ``None``, not raise."""
    assert resolve_impersonation(None, "firefox") is None


def test_an_explicit_profile_is_honoured_even_without_the_dependency(
    hidden_curl_cffi,
) -> None:
    """An explicit choice is not silently dropped -- building it will explain."""
    assert resolve_impersonation("firefox133", "firefox") == "firefox133"


def test_a_caller_supplied_transport_is_left_alone(fake_curl_cffi) -> None:
    """``http_transport=`` is a deliberate choice; nothing is derived over it."""
    assert resolve_impersonation(None, "firefox", http_transport=object()) is None
    # ...but an explicit profile still collides, and says why.
    assert resolve_impersonation("chrome", "firefox", http_transport=object()) == "chrome"


def test_false_opts_out_of_the_derived_default(fake_curl_cffi) -> None:
    assert resolve_impersonation(False, "firefox") is None


def test_firefox_source_is_recognised_by_class_and_by_name(
    firefox_env, fake_curl_cffi
) -> None:
    manager = firefox_env()

    by_name = _resolve_session_source("firefox", "https://arena.ai")
    assert by_name is not None and by_name.impersonate == "firefox"

    by_object = _resolve_session_source(manager, "https://arena.ai")
    assert by_object is not None and by_object.impersonate == "firefox"


def test_a_browser_session_manager_may_declare_its_own_profile(
    tmp_path, fake_curl_cffi
) -> None:
    """``arena_api.auto`` fills the field in from the browser it discovered."""
    manager = BrowserSessionManager(
        base_url="https://arena.ai", storage_path=tmp_path / "cookies.json"
    )
    manager.import_cookies({"arena-auth-prod-v1.0": "x"})
    assert _resolve_session_source(manager, "https://arena.ai").impersonate is None

    manager.impersonate = "chrome"
    resolved = _resolve_session_source(manager, "https://arena.ai")
    assert resolved is not None and resolved.impersonate == "chrome"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("firefox", "firefox"),
        ("browser:chrome", "chrome"),
        ("browser:brave", "chrome"),
        ("browser:edge", "chrome"),
        ("storage", "chrome"),
        ("env", "chrome"),
        ("cookies", "chrome"),
        (None, None),
        ("something-new", None),
    ],
)
def test_auto_maps_a_source_to_a_profile(source, expected) -> None:
    assert profile_for_source(source) == expected


def test_auto_follows_the_discovered_browser(fake_curl_cffi) -> None:
    firefox = DiscoveredSession(
        source="firefox", cookies={"a": "b"}, headers={}, description="Firefox"
    )
    chrome = DiscoveredSession(
        source="browser:chrome", cookies={"a": "b"}, headers={}, description="Chrome"
    )

    assert impersonation_profile(AutoOptions(), firefox) == "firefox"
    assert impersonation_profile(AutoOptions(), chrome) == "chrome"


def test_auto_is_silent_without_a_dependency(hidden_curl_cffi) -> None:
    """``auto`` must never turn a working setup into an error."""
    discovered = DiscoveredSession(
        source="firefox", cookies={"a": "b"}, headers={}, description="Firefox"
    )

    assert impersonation_profile(AutoOptions(), discovered) is None
    assert impersonation_profile(AutoOptions(impersonate="off"), discovered) is None
    # ...an explicit request, however, is not swallowed here.
    assert impersonation_profile(AutoOptions(impersonate="chrome"), discovered) == "chrome"


def test_auto_options_normalise_off_spellings() -> None:
    assert AutoOptions(impersonate="off").impersonate is None
    assert AutoOptions(impersonate=False).impersonate is None  # type: ignore[arg-type]
    assert AutoOptions(impersonate="").impersonate is None
    assert AutoOptions().impersonate == "auto"
    with pytest.raises(ValueError):
        AutoOptions(impersonate=7)  # type: ignore[arg-type]


def test_explain_says_what_the_wire_will_be(fake_curl_cffi) -> None:
    assert explain_impersonation(AutoOptions(impersonate="off")) == "off"
    assert explain_impersonation(AutoOptions(impersonate="chrome")) == "chrome"
    assert "auto" in explain_impersonation(AutoOptions())


def test_explain_names_the_extra_when_it_is_missing(hidden_curl_cffi) -> None:
    text = explain_impersonation(AutoOptions(impersonate="firefox"))
    assert IMPERSONATE_EXTRA in text
    assert "auto" in explain_impersonation(AutoOptions())


# --------------------------------------------------------------------------- #
# Wiring: the transports really build it
# --------------------------------------------------------------------------- #


def test_impersonation_available_follows_the_dependency(fake_curl_cffi) -> None:
    assert impersonation_available() is True


def test_impersonation_is_unavailable_when_hidden(hidden_curl_cffi) -> None:
    assert impersonation_available() is False


def test_building_without_the_dependency_explains_itself(hidden_curl_cffi) -> None:
    with pytest.raises(ArenaError) as error:
        build_impersonation_transport("firefox")

    message = str(error.value)
    assert "httpx-curl-cffi" in message or "curl_cffi" in message
    assert "impersonate" in message


def test_build_rejects_a_non_profile_value(fake_curl_cffi) -> None:
    with pytest.raises(ValueError):
        build_impersonation_transport("")


def test_build_cannot_take_an_ssl_context(fake_curl_cffi) -> None:
    import ssl

    with pytest.raises(ArenaError):
        build_impersonation_transport("firefox", verify=ssl.create_default_context())


def test_sync_transport_uses_the_curl_transport(fake_curl_cffi) -> None:
    transport = ArenaTransport("https://arena.ai", impersonate="chrome")

    assert transport.impersonate == "chrome"
    assert fake_curl_cffi.instances[0].kwargs == {"impersonate": "chrome", "verify": True}
    transport.close()


def test_async_transport_uses_the_async_one(fake_curl_cffi) -> None:
    transport = AsyncArenaTransport("https://arena.ai", impersonate="firefox")

    assert transport.impersonate == "firefox"
    assert fake_curl_cffi.instances[0].kwargs == {"impersonate": "firefox", "verify": True}
    asyncio.run(transport.close())


def test_verify_is_forwarded_to_curl(fake_curl_cffi) -> None:
    transport = ArenaTransport("https://arena.ai", impersonate="chrome", verify="/tmp/ca.pem")

    assert fake_curl_cffi.instances[0].kwargs["verify"] == "/tmp/ca.pem"
    transport.close()


def test_impersonate_and_http_transport_are_mutually_exclusive(
    fake_curl_cffi, tmp_path
) -> None:
    import httpx

    with pytest.raises(ValueError):
        ArenaTransport(
            "https://arena.ai",
            impersonate="chrome",
            http_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )


def test_no_impersonation_keeps_httpx_as_is(fake_curl_cffi) -> None:
    transport = ArenaTransport("https://arena.ai")

    assert transport.impersonate is None
    assert fake_curl_cffi.instances == []
    transport.close()


def test_a_firefox_client_impersonates_by_default(firefox_env, fake_curl_cffi) -> None:
    """``example_firefox.py`` is the case this default exists for."""
    manager = firefox_env()

    with ArenaClient(session_source=manager) as client:
        assert client.transport.impersonate == "firefox"

    with ArenaClient(session_source=manager, impersonate=False) as client:
        assert client.transport.impersonate is None

    with ArenaClient(session_source=manager, impersonate="chrome") as client:
        assert client.transport.impersonate == "chrome"


def test_an_explicit_cookie_client_stays_neutral(fake_curl_cffi) -> None:
    """Without a session source nothing is known about the browser: no guessing."""
    with ArenaClient(cookies={"a": "b"}) as client:
        assert client.transport.impersonate is None


def test_async_and_direct_clients_accept_it(firefox_env, fake_curl_cffi) -> None:
    manager = firefox_env()

    async_clients = [
        AsyncArenaClient(session_source=manager),
        AsyncDirectArenaClient(session_source=manager),
    ]
    sync_clients = [DirectArenaClient(session_source=manager)]
    for client in async_clients:
        assert client.transport.impersonate == "firefox"
        asyncio.run(client.aclose())
    for client in sync_clients:
        assert client.transport.impersonate == "firefox"
        client.close()


def test_a_missing_dependency_reaches_the_caller(firefox_env, hidden_curl_cffi) -> None:
    """An explicit request that cannot be satisfied fails loudly, with the fix."""
    manager = firefox_env()

    with pytest.raises(ArenaError) as error:
        ArenaClient(session_source=manager, impersonate="firefox")

    assert "impersonate" in str(error.value)

    # ...while the derived default just stays out of the way.
    with ArenaClient(session_source=manager) as client:
        assert client.transport.impersonate is None


def test_the_bootstrap_wires_it_through_the_manager(fake_curl_cffi) -> None:
    """``auto_client()`` gets this for free: the manager carries the choice."""
    from arena_api.auto import build_session_manager

    discovered = DiscoveredSession(
        source="firefox",
        cookies={"arena-auth-prod-v1.0": "x"},
        headers={},
        description="Firefox profile",
    )
    options = AutoOptions(output=io.StringIO(), artifacts_dir=None)

    manager = build_session_manager(options, discovered)

    assert manager.impersonate == "firefox"
    assert "impersonate firefox" in options.output.getvalue()  # type: ignore[union-attr]
    with ArenaClient(session_source=manager) as client:
        assert client.transport.impersonate == "firefox"


def test_the_bootstrap_stays_neutral_for_an_unknown_source(fake_curl_cffi) -> None:
    from arena_api.auto import build_session_manager

    discovered = DiscoveredSession(
        source="something-new",
        cookies={"arena-auth-prod-v1.0": "x"},
        headers={},
        description="?",
    )
    options = AutoOptions(output=io.StringIO(), artifacts_dir=None)

    manager = build_session_manager(options, discovered)

    assert manager.impersonate is None
    assert "impersonate" not in options.output.getvalue()  # type: ignore[union-attr]


def test_the_cli_flag_reaches_the_options() -> None:
    from arena_api.auto import _options_from_args, build_arg_parser

    assert _options_from_args(build_arg_parser().parse_args([])).impersonate == "auto"
    parsed = build_arg_parser().parse_args(["--impersonate", "chrome"])
    assert _options_from_args(parsed).impersonate == "chrome"
    assert "--impersonate" in build_arg_parser().format_help()


# --------------------------------------------------------------------------- #
# The wire: a real socket, real curl, our headers
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def wire():
    """Real requests need the real package; everything above does not."""
    pytest.importorskip("httpx_curl_cffi")
    pytest.importorskip("curl_cffi")


SEEN: list[dict[str, Any]] = []
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/157.0.1"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep pytest output clean
        return None

    def do_GET(self) -> None:
        SEEN.append({"path": self.path, "headers": dict(self.headers)})
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            for index in range(3):
                self.wfile.write(f"data: {json.dumps({'i': index})}\n\n".encode())
                self.wfile.flush()
            return
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def live_origin():
    """A loopback HTTP server; no external network is involved."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_an_impersonated_request_keeps_our_identity(wire, live_origin) -> None:
    """curl's own impersonation must not replace the headers we send."""
    with ArenaTransport(
        live_origin,
        headers={"user-agent": USER_AGENT, "accept-language": "en-US,en;q=0.5"},
        cookies={"arena-auth-prod-v1.0": "secret", "cf_clearance": "abc"},
        impersonate="firefox",
    ) as transport:
        assert transport.get_json("/config") == {"ok": True}

    sent = SEEN[-1]["headers"]
    assert sent.get("user-agent") == USER_AGENT
    assert sent.get("accept-language") == "en-US,en;q=0.5"
    assert "arena-auth-prod-v1.0=secret" in sent.get("Cookie", "")
    assert "cf_clearance=abc" in sent.get("Cookie", "")


def test_an_impersonated_stream_is_still_incremental(wire, live_origin) -> None:
    """SSE over the curl transport must not be buffered into one chunk."""
    with ArenaTransport(live_origin, impersonate="firefox") as transport:
        events = list(transport.sse("/stream", params={}, timeout=10))

    assert [payload for _, payload in events] == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_the_async_transport_works_over_the_wire(wire, live_origin) -> None:
    async def scenario() -> Any:
        transport = AsyncArenaTransport(live_origin, impersonate="firefox")
        try:
            return await transport.get_json("/config")
        finally:
            await transport.close()

    assert asyncio.run(scenario()) == {"ok": True}
