"""Local authenticated Anthropic bridge and launcher for OpenCode.

No Arena credentials are exposed to OpenCode. Tool calls are translated by the
existing Anthropic shim; model inventory is read from the user's Arena session.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from . import anthropic_server as shim
from .capri_server import _CapriHandler, load_or_create_token
from ..attachments import Attachment
from ..direct import _has_capability, model_chat_score
from ..models import ModelInfo

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "state"
KEY_PATH = STATE / "opencode-key.txt"
CONFIG_PATH = STATE / "opencode-provider.json"
DEFAULT_PORT = 8083
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


def model_entries(models: Sequence[ModelInfo]) -> dict[str, dict[str, Any]]:
    """Advertise selectable text models only, using exact Arena UUIDs.

    Unknown capability metadata is not a guarantee that the model works. Arena
    remains authoritative and a failed selection is returned to the caller.
    """
    result: dict[str, dict[str, Any]] = {
        "text/auto": {"name": "Arena Auto", "tool_call": True, "limit": {"context": 32768, "output": 4096}},
    }
    for item in sorted(models, key=lambda m: (model_chat_score(m), m.display_name or "", m.id)):
        if not item.available or not _UUID.fullmatch(item.id):
            continue
        if item.metadata.get("userSelectable") is False or model_chat_score(item) >= 50_000:
            continue
        name = item.display_name or item.id
        entry: dict[str, Any] = {
            "name": f"text/{name}", "tool_call": True,
            "limit": {"context": 32768, "output": 4096},
        }
        caps = item.metadata.get("capabilities")
        if isinstance(caps, Mapping) and _has_capability(caps.get("inputCapabilities"), "image"):
            entry["attachment"] = True
            entry["modalities"] = {"input": ["text", "image"], "output": ["text"]}
        result[f"text/{item.id}"] = entry
    return result


def provider_config(
    models: Sequence[ModelInfo], port: int, *, api_key: str = "{env:ARENA_OPENCODE_API_KEY}"
) -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": "arena-local/text/auto",
        "provider": {
            "arena-local": {
                "npm": "@ai-sdk/anthropic",
                "name": "Arena Local",
                "options": {
                    "baseURL": f"http://127.0.0.1:{port}/v1",
                    "apiKey": api_key,
                },
                "models": model_entries(models),
            }
        },
    }


class OpenCodeEngine(shim.ArenaEngine):
    """Keep tool translation but never silently substitute another model."""

    def __init__(self, *, engine: str = "http", session: str = "auto", archive: bool = True) -> None:
        super().__init__(engine=engine, session=session, archive=archive, tools_mode="emulate")

    def _open(
        self, prompt: str, model: str | None, attachments: Sequence[Attachment] | None = None,
    ) -> tuple[Any, str]:
        if model is None or model == "text/auto":
            arena_model = None
        # Older generated configurations used photo/ for image input. Keep
        # those IDs working, but don't advertise them as image generators.
        elif model.startswith(("text/", "photo/")) and _UUID.fullmatch(model.split("/", 1)[1]):
            arena_model = model.split("/", 1)[1]
        else:
            raise shim.EngineError(f"Unknown model {model!r}; refresh the OpenCode model list.")
        kwargs: dict[str, Any] = {"model": arena_model}
        if attachments:
            kwargs["attachments"] = attachments
        stream = self._primary.answer(prompt, **kwargs)
        try:
            first = next(stream)
        except StopIteration:
            self._close_stream(stream)
            return iter(()), ""
        except Exception as exc:
            self._close_stream(stream)
            if attachments:
                raise shim.EngineError("image request failed; check the model and session") from exc
            raise shim.EngineError(str(exc)) from exc
        return stream, first

    def discover_models(self) -> list[ModelInfo]:
        if self.engine == "echo":
            return []
        # _HttpEngine reuses this authenticated client for subsequent requests.
        return self._primary._get_client().get_models(refresh=True)


def create_server(*, token: str, port: int = DEFAULT_PORT, engine: Any = None) -> Any:
    from .capri_server import _valid_token
    if not _valid_token(token):
        raise ValueError("invalid local API key")
    server = shim.create_server(host="127.0.0.1", port=port, engine=engine or OpenCodeEngine(),
                                default_model="text/auto")
    server.capri_token = token  # type: ignore[attr-defined]
    server.RequestHandlerClass = _CapriHandler
    return server


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only our generated file is replaced, never the user's existing opencode.json.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Arena provider for OpenCode")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--session", default="auto", help="browser cookie source (default: auto)")
    parser.add_argument("--no-archive", action="store_true", help="keep chats in the sidebar")
    parser.add_argument("--desktop", "--server-only", dest="mode", action="store_const", const="desktop", default="cli", help="serve for the OpenCode desktop app without starting the CLI")
    parser.add_argument("--cli", dest="mode", action="store_const", const="cli", help="start the OpenCode CLI together with the server")
    parser.add_argument("--engine", choices=("http", "echo"), default="http", help=argparse.SUPPRESS)
    args, opencode_args = parser.parse_known_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    opencode = (shutil.which("opencode") or shutil.which("opencode.cmd")) if args.mode == "cli" else None
    if args.mode == "cli" and not opencode:
        print("OpenCode not found in PATH. Install it first: https://opencode.ai/docs/", file=sys.stderr)
        return 1
    token = load_or_create_token(KEY_PATH)
    engine = OpenCodeEngine(engine=args.engine, session=args.session, archive=not args.no_archive)
    try:
        server = create_server(token=token, port=args.port, engine=engine)
    except OSError as exc:
        print(f"Cannot bind 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        engine.close()
        return 1
    try:
        try:
            models = engine.discover_models()
            print(f"Arena catalog: {len(model_entries(models)) - 1} text models.", flush=True)
        except Exception as exc:
            models = []
            print(f"Could not load the Arena catalog: {exc}\nOnly text/auto is listed; restart after signing in.", file=sys.stderr, flush=True)
        # The desktop app is a separate process and cannot inherit environment
        # variables set by a batch file. Use an absolute file reference instead.
        key_reference = (
            "{file:" + KEY_PATH.resolve().as_posix() + "}"
            if args.mode == "desktop" else "{env:ARENA_OPENCODE_API_KEY}"
        )
        _write_config(CONFIG_PATH, provider_config(models, args.port, api_key=key_reference))
        print(f"Local API: http://127.0.0.1:{args.port}/v1", flush=True)
        print(f"Generated OpenCode config: {CONFIG_PATH}", flush=True)
        print(f"Private API key file: {KEY_PATH} (keep it secret)", flush=True)
        print("Tools are emulated; image generation and web search are not implemented.", flush=True)
        if args.mode == "desktop":
            print("Desktop mode: copy the generated provider into your OpenCode config, then restart the app.", flush=True)
            print("Keep this window open while using OpenCode. Press Ctrl+C to stop the API.", flush=True)
            server.serve_forever(poll_interval=0.2)
            return 0
        assert opencode is not None
        env = os.environ.copy()
        env["ARENA_OPENCODE_API_KEY"] = token
        if env.get("OPENCODE_CONFIG"):
            print("Note: OPENCODE_CONFIG is replaced for this OpenCode process; global/project configs still merge.", flush=True)
        env["OPENCODE_CONFIG"] = str(CONFIG_PATH)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        try:
            # On Windows opencode.cmd is a batch script and needs cmd.exe.
            if os.name == "nt" and opencode.lower().endswith((".cmd", ".bat")):
                return subprocess.call([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", opencode, *opencode_args], env=env)
            return subprocess.call([opencode, *opencode_args], env=env)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
