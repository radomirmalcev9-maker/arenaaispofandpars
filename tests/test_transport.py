from __future__ import annotations

import json

import httpx
import pytest

from arena_api.exceptions import (
    AuthenticationError,
    ChallengeRequiredError,
    ProtocolError,
    RateLimitError,
)
from arena_api.transport import (
    ArenaTransport,
    SSEParser,
    _raise_for_status,
    normalize_cookies,
    parse_cookie_header,
)


def test_parse_cookie_header_preserves_equals_and_values() -> None:
    assert parse_cookie_header(" session=abc== ; cf_clearance=xyz; empty= ") == {
        "session": "abc==",
        "cf_clearance": "xyz",
        "empty": "",
    }


def test_normalize_cookies_supports_browser_exports_and_json_files(tmp_path) -> None:
    assert normalize_cookies({"cookie": "session=one; other=two"}) == {
        "session": "one",
        "other": "two",
    }
    assert normalize_cookies({"session": 123}) == {"session": "123"}
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "session", "value": "from-file", "domain": "arena.ai"}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert normalize_cookies(path) == {"session": "from-file"}
    assert normalize_cookies(str(path)) == {"session": "from-file"}


def test_sse_parser_handles_split_utf8_multiline_and_crlf() -> None:
    wire = "event: token\r\ndata: Привет\r\ndata: мир\r\n\r\n: heartbeat\r\n"
    encoded = wire.encode("utf-8")
    parser = SSEParser()
    events = []
    for byte in encoded:
        events.extend(parser.feed(bytes([byte])))
    events.extend(parser.close())
    assert events == [("token", "Привет\nмир")]


def test_sse_parser_flushes_unterminated_final_event() -> None:
    parser = SSEParser()
    assert parser.feed(b'data: {"ok": true}') == []
    assert parser.close() == [("message", '{"ok": true}')]
    assert parser.close() == []


def test_http_status_errors_map_to_public_exceptions() -> None:
    with pytest.raises(ChallengeRequiredError):
        _raise_for_status(403, "", {"cf-mitigated": "challenge"})
    with pytest.raises(ChallengeRequiredError):
        _raise_for_status(503, "<html>Just a moment... challenge-platform</html>")
    with pytest.raises(AuthenticationError):
        _raise_for_status(401)
    with pytest.raises(RateLimitError) as raised:
        _raise_for_status(429, headers={"retry-after": "5"})
    assert raised.value.retry_after == 5


def test_transport_sse_stream_and_cookie_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie") == "session=secret"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'event: process_completed\ndata: {"msg":"process_completed"}\n\n',
        )

    transport = ArenaTransport(
        "https://arena.test",
        cookies={"session": "secret"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        assert list(
            transport.sse("/queue/data", params={"session_hash": "a"}, timeout=2)
        ) == [("process_completed", {"msg": "process_completed"})]
    finally:
        transport.close()


def test_transport_blocks_redirects_to_protect_cookie_credentials() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            request=request,
            headers={"location": "https://other.test/collect"},
        )

    transport = ArenaTransport(
        "https://arena.test", http_transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ProtocolError, match="redirected"):
            transport.get_text("/config")
        assert calls == 1
    finally:
        transport.close()


def test_transport_reports_interactive_challenge_without_retrying() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            headers={"cf-mitigated": "challenge"},
            content=b"challenge",
        )

    transport = ArenaTransport(
        "https://arena.test", http_transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ChallengeRequiredError):
            list(transport.sse("/queue/data", params={}, timeout=1))
        assert calls == 1
    finally:
        transport.close()
