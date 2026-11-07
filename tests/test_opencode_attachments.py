"""OpenCode fixes: real attachment uploads and exact model selection."""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from typing import Any

import pytest

from arena_api import _direct_page as dp
from arena_api.attachments import Attachment, from_bytes
from arena_api.exceptions import ModelUnavailableError

PNG = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class Response:
    def __init__(self, payload: Any, *, ok: bool = True, status: int = 200) -> None:
        self.payload = payload
        self.ok = ok
        self.status = status

    async def json(self) -> Any:
        return self.payload


class StorageAPI:
    """Minimal stand-in for Playwright's APIRequestContext."""

    def __init__(self, *, fail_status: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_status = fail_status

    async def post(self, url: str, *, data: dict[str, Any]) -> Response:
        self.calls.append(("post", url, data))
        if self.fail_status:
            return Response({}, ok=False, status=500)
        if "generate-upload-url" in url:
            return Response({
                "uploadUrl": "https://arena.ai/upload/presigned",
                "key": "/chat-images/shot.png",
            })
        return Response({"url": "https://arena.ai/files/signed/shot.png?token=abc"})

    async def put(self, url: str, *, data: bytes, headers: dict[str, str]) -> Response:
        self.calls.append(("put", url, data, headers))
        if self.fail_status:
            return Response({}, ok=False, status=503)
        return Response({})


class StoragePage:
    url = "https://arena.ai/text/direct?model_a=max"

    def __init__(self, api: Any) -> None:
        self.request = api


def _inline_png() -> Attachment:
    return from_bytes(PNG, "shot.png")


def test_inline_attachment_is_uploaded_through_arena_storage() -> None:
    api = StorageAPI()
    prepared = asyncio.run(dp._prepare_arena_attachments(StoragePage(api), [_inline_png()]))
    assert prepared == [{
        "name": "shot.png",
        "contentType": "image/png",
        "url": "https://arena.ai/files/signed/shot.png?token=abc",
        "size": len(PNG),
    }]
    assert api.calls[0][:2] == (
        "post", "https://arena.ai/api/storage/generate-upload-url"
    )
    assert api.calls[0][2] == {
        "fileName": "shot.png", "contentType": "image/png"
    }
    assert api.calls[1][0] == "put"
    assert api.calls[1][2] == PNG
    assert api.calls[1][3] == {"Content-Type": "image/png"}
    assert api.calls[2][2] == {"key": "chat-images/shot.png"}


def test_browser_fetch_upload_is_preferred_over_api_context() -> None:
    class BrowserPage:
        url = "https://arena.ai/text/direct"

        def __init__(self, api: Any) -> None:
            self.request = api
            self.scripts: list[str] = []

        async def evaluate(self, script: str) -> dict[str, str]:
            self.scripts.append(script)
            return {"url": "https://arena.ai/files/from-browser.png?sig=1"}

    api = StorageAPI()
    page = BrowserPage(api)
    prepared = asyncio.run(dp._prepare_arena_attachments(page, [_inline_png()]))
    assert prepared == [{
        "name": "shot.png", "contentType": "image/png",
        "url": "https://arena.ai/files/from-browser.png?sig=1",
        "size": len(PNG),
    }]
    assert api.calls == []
    assert "generate-upload-url" in page.scripts[0]
    assert "get-signed-url" in page.scripts[0]
    assert b"image-bytes" not in page.scripts[0].encode()
    assert base64.b64encode(PNG).decode() in page.scripts[0]


def test_existing_url_attachments_are_passed_through() -> None:
    att = Attachment("shot.png", "image/png", "https://example.com/a.png", 12)
    api = StorageAPI()
    prepared = asyncio.run(dp._prepare_arena_attachments(StoragePage(api), [att]))
    assert prepared == [att.to_ai_sdk()]
    assert api.calls == []


def test_missing_request_context_keeps_inline_data_url() -> None:
    class NoAPI:
        url = "https://arena.ai/text/direct"

    prepared = asyncio.run(dp._prepare_arena_attachments(NoAPI(), [_inline_png()]))
    assert prepared[0]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(prepared[0]["url"].split(",", 1)[1]) == PNG


def test_failed_upload_falls_back_to_inline_data_url() -> None:
    api = StorageAPI(fail_status=True)
    prepared = asyncio.run(dp._prepare_arena_attachments(StoragePage(api), [_inline_png()]))
    assert prepared[0]["url"].startswith("data:image/png;base64,")
    assert len(api.calls) == 1


def test_identical_images_are_uploaded_only_once() -> None:
    api = StorageAPI()
    page = StoragePage(api)
    first = asyncio.run(dp._prepare_arena_attachments(page, [_inline_png()]))
    second = asyncio.run(dp._prepare_arena_attachments(page, [_inline_png()]))
    assert first == second
    assert sum(call[0] == "put" for call in api.calls) == 1


# --------------------------------------------------------------------------- #
# Composer: patch shape and model selection fallback
# --------------------------------------------------------------------------- #


class ComposerLocator:
    def __init__(self, selector: str, events: dict[str, int]) -> None:
        self.selector = selector
        self.events = events

    @property
    def first(self) -> "ComposerLocator":
        return self

    async def focus(self) -> None:
        return None

    async def fill(self, value: str) -> None:
        return None

    async def press(self, key: str) -> None:
        self.events["submitted"] = 1

    async def click(self) -> None:
        self.events["submitted"] = 1


class ComposerPage:
    url = "https://arena.ai/text/direct"

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.events = {"submitted": 0}

    def locator(self, selector: str) -> ComposerLocator:
        return ComposerLocator(selector, self.events)

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, script: str) -> Any:
        self.scripts.append(script)
        if "composer_nonempty" in script:
            return True
        if "data-arena-composer" in script and "data-arena-send" not in script:
            return {"found": True, "tag": "textarea", "x": 400, "y": 700}
        if "data-arena-send" in script:
            return {"found": True, "confidence": "explicit", "x": 650, "y": 700}
        return None


def _patch_objects(page: ComposerPage) -> list[dict[str, Any]]:
    found = []
    for script in page.scripts:
        marker = "window.__arenaBodyPatch ="
        if marker in script:
            literal = script.split(marker, 1)[1].split(";", 1)[0].strip()
            if literal != "null":
                found.append(json.loads(literal))
    return found


def test_unconfirmed_picker_label_forces_exact_uuid_in_request(monkeypatch) -> None:
    async def unconfirmed(_page, model_id, *, category=None):
        raise dp._ModelSelectionUnconfirmed(
            "Arena did not confirm selection of model 'shown-name'."
        )

    monkeypatch.setattr(dp, "_select_model_in_picker", unconfirmed)
    page = ComposerPage()
    assert asyncio.run(dp._submit_prompt_via_composer(
        page, "hello", model_id_override=UUID,
        has_submitted=lambda: page.events["submitted"] == 1,
    ))
    patches = _patch_objects(page)
    assert patches[-1]["modelAId"] == UUID
    assert patches[-1]["requirePatch"] is True


def test_missing_option_under_forced_category_retries_unfiltered(monkeypatch) -> None:
    seen: list[str | None] = []

    async def select(_page, model_id, *, category=None):
        seen.append(category)
        if category is not None:
            raise ModelUnavailableError("option not mounted")
        return None

    monkeypatch.setattr(dp, "_select_model_in_picker", select)
    page = ComposerPage()
    assert asyncio.run(dp._submit_prompt_via_composer(
        page, "hello", model_id_override=UUID, model_picker_category="text",
        has_submitted=lambda: page.events["submitted"] == 1,
    ))
    assert seen == ["text", None]
    assert not _patch_objects(page)


def test_composer_uses_uploaded_attachment_urls(monkeypatch) -> None:
    async def upload(_page, attachments, *, mark=None):
        return [{"name": "shot.png", "contentType": "image/png",
                 "url": "https://arena.ai/files/signed/shot.png", "size": len(PNG)}]

    monkeypatch.setattr(dp, "_prepare_arena_attachments", upload)
    page = ComposerPage()
    assert asyncio.run(dp._submit_prompt_via_composer(
        page, "describe", attachments=[_inline_png()],
        has_submitted=lambda: page.events["submitted"] == 1,
    ))
    assert _patch_objects(page)[-1]["experimental_attachments"][0]["url"] == (
        "https://arena.ai/files/signed/shot.png"
    )


# --------------------------------------------------------------------------- #
# Picker confirmation: exact UUID and Radix state, not just visible text
# --------------------------------------------------------------------------- #


class PickerNode:
    def __init__(self, page: "Picker", kind: str) -> None:
        self.page, self.kind = page, kind

    @property
    def first(self) -> "PickerNode":
        return self

    def locator(self, selector: str) -> "PickerNode":
        return PickerNode(self.page, "name" if "font-mono" in selector else "option")

    async def wait_for(self, *, state: str, timeout: int) -> None:
        return None

    async def click(self, *, timeout: int) -> None:
        return None

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:
        return None

    async def get_attribute(self, name: str) -> Any:
        return self.page.attributes.get((self.kind, name))

    async def text_content(self) -> str:
        return "internal-name" if self.kind == "name" else self.page.selected

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True


class Picker:
    def __init__(self, attributes: dict[tuple[str, str], Any], selected: str) -> None:
        self.attributes = attributes
        self.selected = selected

    def locator(self, selector: str) -> PickerNode:
        return PickerNode(self, "dialog" if '[role="dialog"]' in selector else "trigger")


def test_radix_selected_option_confirms_without_text_match(monkeypatch) -> None:
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(dp.asyncio, "sleep", no_wait)
    page = Picker(
        {("option", "aria-selected"): "true"},
        selected="A completely different label",
    )
    asyncio.run(dp._select_model_in_picker(page, UUID))


def test_trigger_uuid_attribute_confirms_without_text_match(monkeypatch) -> None:
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(dp.asyncio, "sleep", no_wait)
    page = Picker({("trigger", "data-value"): UUID}, selected="Some badge New")
    asyncio.run(dp._select_model_in_picker(page, UUID))


def test_still_rejects_an_unconfirmed_selection(monkeypatch) -> None:
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(dp.asyncio, "sleep", no_wait)
    page = Picker({}, selected="another-model-high")
    with pytest.raises(dp._ModelSelectionUnconfirmed, match="did not confirm"):
        asyncio.run(dp._select_model_in_picker(page, UUID))


# --------------------------------------------------------------------------- #
# The fetch patch must survive a Request object and never drop an image
# --------------------------------------------------------------------------- #


def test_anthropic_sdk5_data_url_image_blocks_are_accepted() -> None:
    from arena_api.compat import anthropic_server as shim

    encoded = base64.b64encode(PNG).decode()
    payload = {"model": "text/auto", "max_tokens": 100,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": "describe"},
                   {"type": "image", "source": {
                       "type": "url", "url": f"data:image/png;base64,{encoded}"}},
               ]}]}
    request = shim.translate_request(payload)
    assert len(request.attachments) == 1
    assert request.attachments[0].content_type == "image/png"
    assert base64.b64decode(request.attachments[0].url.split(",", 1)[1]) == PNG

    payload["messages"][0]["content"][1] = {
        "type": "image", "media_type": "image/png", "data": encoded
    }
    assert len(shim.translate_request(payload).attachments) == 1


def test_remote_image_url_is_still_rejected() -> None:
    from arena_api.compat import anthropic_server as shim

    payload = {"model": "text/auto", "messages": [{"role": "user", "content": [
        {"type": "image", "source": {
            "type": "url", "url": "https://attacker.test/pic.png"}},
    ]}]}
    with pytest.raises(shim.BadRequest, match="inline base64"):
        shim.translate_request(payload)


def test_browser_upload_uses_page_fetch_for_all_three_storage_steps() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = dp._browser_upload_js(_inline_png(), PNG)
    harness = """
    (async () => {
      const calls = [];
      global.fetch = async (url, init) => {
        calls.push({url: String(url), method: init.method, body: init.body});
        if (String(url).indexOf('generate-upload-url') !== -1) {
          return {ok: true, json: async () => ({
            uploadUrl: '/upload/presigned', key: '/chat-images/shot.png'
          })};
        }
        if (init.method === 'PUT') return {ok: true, status: 200};
        return {ok: true, json: async () => ({
          url: 'https://arena.ai/files/signed/shot.png'
        })};
      };
      const upload = %s;
      const result = await upload();
      console.log(JSON.stringify({calls, result}));
    })().catch(e => { console.error(e); process.exit(1); });
    """ % script
    result = subprocess.run([node, "-e", harness], check=True, text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    out = json.loads(result.stdout)
    assert [call["method"] for call in out["calls"]] == ["POST", "PUT", "POST"]
    assert out["result"] == {"url": "https://arena.ai/files/signed/shot.png"}
    assert json.loads(out["calls"][0]["body"]) == {
        "fileName": "shot.png", "contentType": "image/png"
    }
    assert json.loads(out["calls"][2]["body"]) == {
        "key": "chat-images/shot.png"
    }


def test_request_object_body_is_patched_and_failure_is_visible() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    att = {"name": "shot.png", "contentType": "image/png",
           "url": "https://arena.ai/files/signed/shot.png", "size": 3}
    patch = dp._build_main_frame_body_patch_js({"experimental_attachments": [att]})
    clear = dp._CLEAR_MAIN_FRAME_BODY_PATCH_JS
    script = f"""
    (async () => {{
      const bodies = [];
      global.window = {{ fetch: async (input, init) => {{
        const req = input instanceof Request ? input : new Request('https://arena.ai/x', {{method: 'POST', body: init.body}});
        bodies.push(JSON.parse(await req.clone().text()));
        return {{ok: true}};
      }} }};
      ({patch})();
      const request = new Request('https://arena.ai/nextjs-api/stream/create-evaluation', {{
        method: 'POST', body: JSON.stringify({{userMessage: {{content: 'describe'}}}})
      }});
      await window.fetch(request);
      let failed = false;
      try {{
        await window.fetch('https://arena.ai/nextjs-api/stream/create-evaluation', {{body: JSON.stringify({{}})}});
      }} catch (e) {{ failed = true; }}
      const state = {{ bodies, failed,
        applied: window.__arenaBodyPatchApplied, error: window.__arenaBodyPatchError }};
      ({clear})();
      console.log(JSON.stringify(state));
    }})().catch(e => {{ console.error(e); process.exit(1); }});
    """
    result = subprocess.run([node, "-e", script], check=True, text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    out = json.loads(result.stdout)
    assert out["bodies"][0]["userMessage"]["experimental_attachments"] == [att]
    assert out["applied"] == 1
    assert out["failed"] is True
    assert "userMessage" in out["error"]


def test_old_warm_daemon_is_not_reused_after_the_attachment_fix() -> None:
    from arena_api import warm

    state = warm.DaemonState(
        pid=4242, port=45000, token="token", base_url="https://arena.ai",
        protocol=warm.PROTOCOL_VERSION - 1,
        api_version=warm.api_version(),
    )
    assert not warm._compatible(state, base_url=state.base_url)
    assert warm.PROTOCOL_VERSION >= 4


def test_image_error_categories_never_echo_the_payload() -> None:
    from arena_api.compat import anthropic_server as shim

    secret = "data:image/png;base64,SECRET-IMAGE-BYTES"
    cases = [
        (RuntimeError(f"Arena request has no userMessage for attachments {secret}"),
         "Arena changed the chat request format"),
        (RuntimeError(f"Arena request body cannot carry attachments {secret}"),
         "Arena rejected the attachment payload"),
        (RuntimeError(f"Arena did not return a file URL {secret}"),
         "Arena could not store the attachment"),
        (shim.EngineError(
            "image request failed: Arena could not select or confirm the requested model"
        ), "Arena could not select or confirm the requested model"),
    ]
    for error, marker in cases:
        detail = shim._safe_image_error(error)
        assert marker in detail
        assert "SECRET-IMAGE-BYTES" not in detail
        assert detail.startswith("image request failed: ")


def test_forced_model_patch_rejects_unpatchable_body() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    patch = dp._build_main_frame_body_patch_js(
        {"modelAId": UUID, "requirePatch": True}
    )
    script = f"""
    (async () => {{
      global.window = {{ fetch: async () => ({{ok: true}}) }};
      ({patch})();
      let failed = false;
      try {{ await window.fetch('https://arena.ai/nextjs-api/stream/create-evaluation', {{}}); }}
      catch (e) {{ failed = true; }}
      console.log(JSON.stringify({{failed, error: window.__arenaBodyPatchError}}));
    }})().catch(e => {{ console.error(e); process.exit(1); }});
    """
    result = subprocess.run([node, "-e", script], check=True, text=True,
                            encoding="utf-8", capture_output=True, timeout=15)
    assert json.loads(result.stdout)["failed"] is True
