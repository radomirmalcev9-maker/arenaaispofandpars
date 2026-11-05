"""Screenshot transport: authenticated phone JSON -> real attachment -> engines."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from arena_api import _direct_page
from arena_api.compat import anthropic_server as shim, capri_server as capri

TOKEN = "s" * 43
JPEG = b"\xff\xd8\xff" + b"image-content-not-text" * 20
IMAGE64 = base64.b64encode(JPEG).decode()


def payload(stream: bool = False) -> dict[str, Any]:
    return {"stream": stream, "model": "arena-auto", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What is on my screen?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": IMAGE64}},
    ]}]}


class Engine:
    tools_mode = "off"
    verbose = False
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
    def answer(self, prompt: str, *, model: str | None = None, attachments: Any = None) -> Iterator[str]:
        self.calls.append((prompt, attachments))
        yield "ok"
    def close(self) -> None:
        pass


def post(server: Any, data: dict[str, Any], token: str = TOKEN) -> tuple[int, bytes]:
    url = "http://%s:%s/v1/messages" % server.server_address[:2]
    req = urllib.request.Request(url, method="POST", data=json.dumps(data).encode(), headers={"x-api-key": token})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.parametrize("stream", [False, True])
def test_authenticated_screenshot_reaches_engine_without_base64_in_prompt(stream: bool) -> None:
    engine = Engine()
    server = capri.create_server(token=TOKEN, port=0, engine=engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert post(server, payload(stream), "wrong")[0] == 401
        assert not engine.calls
        status, body = post(server, payload(stream))
        assert status == 200 and b"ok" in body
        prompt, attachments = engine.calls[0]
        assert "What is on my screen?" in prompt and "Image 1 attached" in prompt
        assert IMAGE64 not in prompt and IMAGE64.encode() not in body
        assert len(attachments) == 1
        assert attachments[0].url == "data:image/jpeg;base64," + IMAGE64
        assert attachments[0].size == len(JPEG)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("bad", [
    {"type": "url", "url": "https://attacker.test/pic"},
    {"type": "base64", "media_type": "image/jpeg", "data": "!!!"},
    {"type": "base64", "media_type": "application/pdf", "data": IMAGE64},
    {"type": "base64", "media_type": "image/png", "data": IMAGE64},
    {"type": "base64", "media_type": "image/jpeg", "data": "a" * (5 * 1024 * 1024)},
])
def test_bad_image_never_falls_back_to_text_or_appears_in_error(bad: dict[str, Any]) -> None:
    data = payload()
    data["messages"][0]["content"][1]["source"] = bad
    with pytest.raises(shim.BadRequest) as exc:
        shim.translate_request(data)
    assert IMAGE64 not in str(exc.value)
    assert "a" * 100 not in str(exc.value)


def test_http_fallback_preserves_attachment_and_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    from arena_api import auto, warm
    seen: dict[str, Any] = {}
    class Client:
        def stream(self, prompt: str, **kwargs: Any) -> Iterator[Any]:
            seen["prompt"] = prompt
            seen.update(kwargs)
            yield type("Chunk", (), {"delta": "ok"})()
        def close(self) -> None:
            pass
    def make(**kwargs: Any) -> Client:
        seen["archive"] = kwargs["archive_after_answer"]
        return Client()
    def fail(**kwargs: Any) -> Any:
        raise warm.WarmDaemonUnavailable("offline")
    monkeypatch.setattr(warm, "ensure_daemon", fail)
    monkeypatch.setattr(auto, "auto_direct_client", make)
    translated = shim.translate_request(payload())
    engine = shim.ArenaEngine(engine="warm", session="auto", archive=True)
    try:
        assert "".join(engine.answer(translated.prompt, attachments=translated.attachments)) == "ok"
        assert seen["archive"] is True
        assert seen["attachments"][0].url.endswith(IMAGE64)
        assert IMAGE64 not in seen["prompt"]
    finally:
        engine.close()


def test_composer_patch_writes_actual_nested_json_and_clears() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    att = {"name": "screen.jpg", "contentType": "image/jpeg", "url": "data:image/jpeg;base64,AAAA", "size": 3}
    patch = _direct_page._build_main_frame_body_patch_js({"experimental_attachments": [att]})
    clear = _direct_page._CLEAR_MAIN_FRAME_BODY_PATCH_JS
    script = f'''(async () => {{
      const bodies = [];
      global.window = {{fetch: async (u, i) => {{ bodies.push(JSON.parse(i.body)); return {{ok:true}}; }} }};
      ({patch})();
      await window.fetch('/nextjs-api/stream/create-evaluation', {{body: JSON.stringify({{userMessage: {{content:'hi'}}}})}});
      let failed = false;
      try {{ await window.fetch('/nextjs-api/stream/create-evaluation', {{body:'{{}}'}}); }} catch(e) {{ failed = true; }}
      ({clear})();
      await window.fetch('/nextjs-api/stream/create-evaluation', {{body: JSON.stringify({{userMessage: {{content:'later'}}}})}});
      console.log(JSON.stringify({{bodies, failed}}));
    }})().catch(e => {{ console.error(e); process.exit(1); }});'''
    result = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True, timeout=10)
    out = json.loads(result.stdout)
    assert out["failed"] is True
    assert out["bodies"] == [
        {"userMessage": {"content": "hi", "experimental_attachments": [att]}},
        {"userMessage": {"content": "later"}},
    ]


def test_upstream_errors_do_not_echo_image_into_logs_or_client() -> None:
    messages: list[str] = []
    class Broken:
        name = "broken"
        def answer(self, prompt: str, **kwargs: Any) -> Iterator[str]:
            raise RuntimeError("upstream echoed " + IMAGE64)
            yield ""  # pragma: no cover
        def close(self) -> None:
            pass
    req = shim.translate_request(payload())
    engine = shim.ArenaEngine(engine="echo", log=messages.append)
    engine._primary = Broken()  # type: ignore[assignment]
    try:
        with pytest.raises(shim.EngineError) as err:
            list(engine.answer(req.prompt, attachments=req.attachments))
        assert IMAGE64 not in str(err.value)
        assert IMAGE64 not in " ".join(messages)
    finally:
        engine.close()
