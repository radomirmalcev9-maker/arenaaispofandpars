"""Security and protocol tests for the Capri/tailnet adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from arena_api.compat import capri_server as capri

TOKEN = "a" * 43
HOST = "laptop.tail1234.ts.net"
TARGET = "http://127.0.0.1:8082"


class FakeEngine:
    engine = "fake"
    tools_mode = "off"
    verbose = False

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.models: list[str | None] = []
        self.closed = False

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        self.prompts.append(prompt)
        self.models.append(model)
        yield "Привет"
        yield " от Arena"

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def running() -> Iterator[tuple[Any, FakeEngine]]:
    engine = FakeEngine()
    server = capri.create_server(token=TOKEN, port=0, engine=engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        engine.close()


def request(
    server: Any,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, bytes, Any]:
    host, port = server.server_address[:2]
    req = urllib.request.Request(
        f"http://{host}:{port}{path}", method=method, headers=headers or {}, data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read(), res.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_binds_only_loopback_and_rejects_missing_or_wrong_key(running: Any) -> None:
    server, engine = running
    assert server.server_address[0] == "127.0.0.1"
    assert request(server, "/v1/models")[0] == 401
    assert request(server, "/v1/models", headers={"x-api-key": "wrong"})[0] == 401
    assert request(server, "/v1/models", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 401
    payload = json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode()
    assert request(server, "/v1/messages", method="POST", data=payload)[0] == 401
    assert engine.prompts == []


def test_non_ascii_key_and_options_are_rejected(running: Any) -> None:
    server, _ = running
    assert request(server, "/v1/models", headers={"x-api-key": "\xff"})[0] == 401
    status, _, headers = request(server, "/v1/models", method="OPTIONS")
    assert status == 405
    assert headers.get("Access-Control-Allow-Origin") is None


def test_capri_model_list_and_streaming_protocol(running: Any) -> None:
    server, engine = running
    headers = {"x-api-key": TOKEN, "anthropic-version": "2023-06-01"}
    status, body, _ = request(server, "/v1/models", headers=headers)
    assert status == 200
    models = json.loads(body)["data"]
    assert models[0]["id"] == "arena-auto"

    data = json.dumps({
        "model": "arena-auto",
        "system": "Ты Капри",
        "messages": [{"role": "user", "content": "Привет!"}],
        "max_tokens": 512,
        "stream": True,
    }, ensure_ascii=False).encode()
    status, body, response_headers = request(
        server, "/v1/messages", method="POST", headers=headers, data=data
    )
    assert status == 200
    assert response_headers["Content-Type"].startswith("text/event-stream")
    events = [json.loads(line[5:].strip()) for line in body.decode().splitlines() if line.startswith("data:")]
    text = "".join(
        e["delta"]["text"] for e in events if e.get("type") == "content_block_delta"
    )
    assert text == "Привет от Arena"
    assert "Ты Капри" in engine.prompts[0]
    assert "Привет!" in engine.prompts[0]
    assert engine.models == [None]


def test_body_limit_rejects_before_reading(running: Any) -> None:
    server, engine = running
    status, body, _ = request(
        server,
        "/v1/messages",
        method="POST",
        headers={"x-api-key": TOKEN, "Content-Length": str(capri.MAX_BODY_BYTES + 1)},
        data=b"{}",
    )
    assert status == 400
    assert b"oversized" in body
    assert engine.prompts == []


def test_pairing_token_is_persistent_private_and_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "state" / "key.txt"
    first = capri.load_or_create_token(path)
    assert capri.load_or_create_token(path) == first
    assert capri._valid_token(first)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("bad\n", encoding="ascii")
    with pytest.raises(capri.CapriError):
        capri.load_or_create_token(path)
    assert path.read_text(encoding="ascii") == "bad\n"


def test_create_server_rejects_weak_key() -> None:
    with pytest.raises(ValueError):
        capri.create_server(token="short", port=0, engine=FakeEngine())


def test_tailscale_status_validation_and_pairing_string() -> None:
    status = {"BackendState": "Running", "Self": {"DNSName": HOST.upper() + "."}}
    assert capri.tailnet_name(status) == HOST
    assert capri.pairing_string(HOST, 8443, TOKEN) == f"arena+https://{HOST}:8443#{TOKEN}"
    for bad in ("outside.example.com", "evil.ts.net", "a.b.ts.net@evil.example", "a.b.ts.net/path"):
        with pytest.raises((ValueError, capri.CapriError)):
            capri.tailnet_name({"BackendState": "Running", "Self": {"DNSName": bad}})
    with pytest.raises(capri.CapriError):
        capri.tailnet_name({"BackendState": "Stopped", "Self": {"DNSName": HOST}})
    with pytest.raises(ValueError):
        capri.pairing_string(HOST, 0, TOKEN)


def route_config(target: str = TARGET, port: int = 8443) -> dict[str, Any]:
    return {
        "TCP": {str(port): {"HTTPS": True}},
        "Web": {f"{HOST}:{port}": {"Handlers": {"/": {"Proxy": target}}}},
    }


def test_first_run_serve_json_null_is_an_empty_config(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SimpleNamespace(returncode=0, stdout="null\n", stderr="")
    monkeypatch.setattr(capri.subprocess, "run", lambda *_a, **_k: result)
    assert capri.tailscale_json("tailscale", "serve", "status") == {}
    with pytest.raises(capri.CapriError):
        capri.tailscale_json("tailscale", "status")


def test_foreground_route_and_existing_port_detection() -> None:
    status = {"Foreground": {"session-id": route_config()}}
    assert capri.serve_port_in_use(status, 8443)
    assert not capri.serve_port_in_use(status, 443)
    assert capri.serve_route_ready(status, HOST, 8443, TARGET)
    assert not capri.serve_route_ready(status, HOST, 8443, "http://127.0.0.1:9999")
    assert not capri.serve_route_ready(status, HOST, 443, TARGET)
    assert not capri.serve_route_ready({"Foreground": {}}, HOST, 8443, TARGET)


def test_funnel_is_never_accepted_even_if_in_another_config() -> None:
    address = f"{HOST}:8443"
    status = {
        "AllowFunnel": {address: True},
        "Foreground": {"our-route": route_config()},
    }
    assert capri.serve_port_in_use(status, 8443)
    with pytest.raises(capri.CapriError, match="Funnel"):
        capri.serve_route_ready(status, HOST, 8443, TARGET)


def test_wait_for_ready_route_and_early_child_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capri, "tailscale_json", lambda *_a: {"Foreground": {"id": route_config()}})
    capri._wait_for_route("tailscale", SimpleNamespace(poll=lambda: None), host=HOST, https_port=8443, target=TARGET)
    with pytest.raises(capri.CapriError, match="stopped"):
        capri._wait_for_route("tailscale", SimpleNamespace(poll=lambda: 1), host=HOST, https_port=8443, target=TARGET)


@pytest.mark.parametrize("exit_race", [False, True])
def test_cli_starts_foreground_serve_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exit_race: bool
) -> None:
    child = SimpleNamespace(ended=False, command=None)
    opened: list[Any] = []
    create_server = capri.create_server

    def track_server(**kwargs: Any) -> Any:
        server = create_server(**kwargs)
        opened.append(server)
        return server

    monkeypatch.setattr(capri, "create_server", track_server)

    def poll() -> int | None:
        return 0 if child.ended else None

    def terminate() -> None:
        child.ended = True
        if exit_race:
            raise ProcessLookupError("already exited")

    def fake_popen(command: list[str]) -> Any:
        child.command = command
        return SimpleNamespace(poll=poll, terminate=terminate, wait=lambda timeout: 0)

    monkeypatch.setattr(capri, "find_tailscale", lambda: "tailscale")
    monkeypatch.setattr(
        capri, "tailscale_json",
        lambda _exe, *args: {"BackendState": "Running", "Self": {"DNSName": HOST}} if args == ("status",) else {},
    )
    monkeypatch.setattr(capri.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(capri, "_wait_for_route", lambda **_kwargs: None)
    monkeypatch.setattr(capri, "_print_pairing", lambda _pairing: (_ for _ in ()).throw(KeyboardInterrupt()))
    args = SimpleNamespace(
        port=0, https_port=8443, key_file=tmp_path / "key", engine="echo", session="auto",
        model="arena-auto", arena_model=[], archive=False, verbose=False,
    )
    assert capri.run(args) == 0
    assert child.command == ["tailscale", "serve", "--https=8443", "http://127.0.0.1:0"]
    assert child.ended
    assert opened[0].socket.fileno() == -1
    assert (tmp_path / "key").is_file()


def test_cli_checks_environment_port(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setenv("ARENA_CAPRI_PORT", "0")
    with pytest.raises(SystemExit) as exc:
        capri.main([])
    assert exc.value.code == 2
    assert "port must be between 1 and 65535" in capsys.readouterr().err
