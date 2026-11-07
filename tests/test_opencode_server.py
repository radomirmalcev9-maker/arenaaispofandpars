from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from arena_api.compat import anthropic_server as shim
from arena_api.compat import opencode_server as oc
from arena_api.models import ModelInfo

KEY = 'k' * 43
ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


def test_config_uses_session_catalog_not_hardcoded_models():
    models = [
        ModelInfo(ID, 'Example', metadata={'rankByModality': {'chat': 4}}),
        ModelInfo('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'Hidden', metadata={'userSelectable': False}),
        ModelInfo('cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'Image', metadata={'capabilities': {'outputCapabilities': ['image']}}),
        ModelInfo('dddddddd-dddd-4ddd-8ddd-dddddddddddd', 'Offline', available=False),
    ]
    config = oc.provider_config(models, 8083)
    provider = config['provider']['arena-local']
    assert provider['npm'] == '@ai-sdk/anthropic'
    assert provider['options']['apiKey'] == '{env:ARENA_OPENCODE_API_KEY}'
    assert set(provider['models']) == {'text/auto', 'text/' + ID}
    assert provider['models']['text/' + ID]['name'] == 'text/Example'
    assert provider['models']['text/' + ID]['tool_call'] is True


def test_exact_model_forwarded_and_error_never_falls_back():
    engine = oc.OpenCodeEngine(engine='echo')
    seen = []
    class Primary:
        def answer(self, prompt, *, model=None, **kwargs):
            seen.append(model)
            if model:
                raise RuntimeError('model not found')
            yield 'ok'
    engine._primary = Primary()
    with pytest.raises(shim.EngineError, match='model not found'):
        engine._open('test', 'text/' + ID)
    assert seen == [ID]
    _, first = engine._open('test', 'text/auto')
    assert first == 'ok' and seen == [ID, None]
    with pytest.raises(shim.EngineError, match='Unknown model'):
        engine._open('test', 'photo/not-supported')


def test_auth_and_streaming_tool_use():
    class FakeEngine:
        engine = 'fake'
        tools_mode = 'emulate'
        def answer(self, prompt, *, model=None, **kwargs):
            assert 'run' in prompt and model == 'text/' + ID
            yield '<tool_call>{"name":"run","arguments":{"command":"pwd"}}</tool_call>'
        def close(self):
            pass
    server = oc.create_server(token=KEY, port=0, engine=FakeEngine())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f'http://127.0.0.1:{server.server_address[1]}/v1/messages'
        data = json.dumps({'model': 'text/' + ID, 'max_tokens': 1000, 'stream': True,
                           'messages': [{'role': 'user', 'content': 'run pwd'}],
                           'tools': [{'name': 'run', 'input_schema': {'type': 'object'}}]}).encode()
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(url, data=data, headers={'content-type': 'application/json'}))
        assert err.value.code == 401
        request = urllib.request.Request(url, data=data, headers={'content-type': 'application/json', 'x-api-key': KEY})
        with urllib.request.urlopen(request) as response:
            wire = response.read().decode()
        assert 'event: content_block_start' in wire
        assert '"type": "tool_use"' in wire
        assert '"stop_reason": "tool_use"' in wire
    finally:
        server.shutdown()
        server.server_close()
        t.join()


def test_local_key_is_stable(tmp_path):
    path = tmp_path / 'key.txt'
    assert oc.load_or_create_token(path) == oc.load_or_create_token(path)
    assert path.read_text().strip() and len(path.read_text().strip()) >= 32


def test_text_models_advertise_only_explicit_image_input_capability():
    model = ModelInfo(ID, 'Vision', metadata={'capabilities': {
        'inputCapabilities': ['text', 'image'], 'outputCapabilities': ['text']
    }})
    entries = oc.model_entries([model])
    assert entries['text/' + ID]['attachment'] is True
    assert 'photo/' + ID not in entries
    assert entries['text/' + ID]['modalities'] == {'input': ['text', 'image'], 'output': ['text']}


def test_photo_model_uses_same_exact_uuid():
    engine = oc.OpenCodeEngine(engine='echo')
    seen = []
    class Primary:
        def answer(self, prompt, *, model=None, **kwargs):
            seen.append(model)
            yield 'ok'
    engine._primary = Primary()
    assert engine._open('hi', 'photo/' + ID)[1] == 'ok'
    assert seen == [ID]


def test_text_request_forwards_image_bytes_and_conversation():
    import base64
    seen = {}
    class Engine:
        engine = 'fake'
        tools_mode = 'emulate'
        def answer(self, prompt, *, model=None, attachments=None):
            seen.update(prompt=prompt, model=model, attachments=attachments)
            yield 'image seen'
        def close(self):
            pass
    server = oc.create_server(token=KEY, port=0, engine=Engine())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        image = b'\x89PNG\r\n\x1a\n' + b'test'
        payload = {'model': 'text/' + ID, 'max_tokens': 1000,
                   'messages': [{'role': 'user', 'content': [
                       {'type': 'text', 'text': 'describe'},
                       {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                                    'data': base64.b64encode(image).decode()}}]}]}
        request = urllib.request.Request(
            f'http://127.0.0.1:{server.server_address[1]}/v1/messages',
            data=json.dumps(payload).encode(),
            headers={'content-type': 'application/json', 'x-api-key': KEY})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['content'][0]['text'] == 'image seen'
        assert seen['model'] == 'text/' + ID
        assert 'describe' in seen['prompt']
        assert len(seen['attachments']) == 1
        assert base64.b64decode(seen['attachments'][0].url.split(',', 1)[1]) == image
    finally:
        server.shutdown()
        server.server_close()
        t.join()


def test_windows_launcher_has_diagnostics_and_checks_interpreters():
    script = (oc.ROOT / 'arena-opencode.bat').read_text(encoding='ascii')
    assert '.venv\\Scripts\\python.exe' in script
    assert 'py -3 -c "import arena_api.compat.opencode_server"' in script
    assert 'python -c "import arena_api.compat.opencode_server"' in script
    assert 'set "RESULT=%ERRORLEVEL%"' in script
    assert 'ARENA_OPENCODE_NO_PAUSE' in script
    assert 'pause' in script


def test_desktop_starts_server_without_cli_and_writes_file_key_reference(tmp_path, monkeypatch, capsys):
    key_path = tmp_path / 'state' / 'opencode-key.txt'
    config_path = tmp_path / 'state' / 'opencode-provider.json'
    monkeypatch.setattr(oc, 'KEY_PATH', key_path)
    monkeypatch.setattr(oc, 'CONFIG_PATH', config_path)
    monkeypatch.setattr(oc.shutil, 'which', lambda *_: pytest.fail('Desktop must not look for CLI'))
    seen = []
    class Server:
        def serve_forever(self, **kwargs):
            seen.append('served')
        def server_close(self):
            seen.append('closed')
    monkeypatch.setattr(oc, 'create_server', lambda **kwargs: Server())
    assert oc.main(['--desktop', '--engine', 'echo']) == 0
    assert seen == ['served', 'closed']
    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config['provider']['arena-local']['options']['apiKey'] == '{file:' + key_path.resolve().as_posix() + '}'
    assert key_path.read_text().strip() not in config_path.read_text()
    assert 'Keep this window open' in capsys.readouterr().out


def test_batch_uses_desktop_by_default_and_cli_is_optional():
    script = (oc.ROOT / 'arena-opencode.bat').read_text(encoding='ascii')
    assert '-m arena_api.compat.opencode_server --desktop %*' in script
    assert '--cli' in script


def test_desktop_config_exposes_images_for_auto_and_unknown_but_not_explicit_no():
    unknown = ModelInfo(ID, 'Unknown', metadata={'capabilities': {'inputCapabilities': {'text': True}}})
    no_image_id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    no_image = ModelInfo(no_image_id, 'Text only', metadata={'capabilities': {
        'inputCapabilities': {'text': True, 'image': False}, 'outputCapabilities': {'text': True}
    }})
    entries = oc.model_entries([unknown, no_image])
    assert entries['text/auto']['attachment'] is True
    assert entries['text/' + ID]['modalities']['input'] == ['text', 'image']
    assert 'attachment' not in entries['text/' + no_image_id]


def test_opencode_engine_selects_text_filter_and_does_not_force_fast_auto():
    engine = oc.OpenCodeEngine(engine='http')
    assert engine._primary._text_picker_for_explicit is True
    assert engine._primary._fast_model is False
    engine.close()
