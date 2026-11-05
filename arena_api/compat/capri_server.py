"""Authenticated, tailnet-only Arena server for the patched Capri Android app.

The underlying Anthropic shim intentionally does not authenticate requests, so
this wrapper binds it to loopback, adds a separate random pairing secret, and
uses *foreground* Tailscale Serve (HTTPS, never Funnel) for remote access.
Neither the secret nor Arena cookies are written to the repository or URLs.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from . import anthropic_server as shim

DEFAULT_PORT = 8082
DEFAULT_HTTPS_PORT = 8443
# Capri sends each request as a new Arena conversation; file answered chats
# away so they do not accumulate in the user's Arena sidebar.
DEFAULT_ARCHIVE = True
DEFAULT_KEY_FILE = Path("state/capri-key.txt")
MAX_BODY_BYTES = 4_000_000
_PAIR_SECRET = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
# Keep this in sync with tools/capri/ArenaBridge.smali. MagicDNS normally has
# exactly <device>.<tailnet>.ts.net; reject URLs/userinfo/ports here.
_TS_HOST = re.compile(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\Z")


class CapriError(RuntimeError):
    """An actionable, non-secret startup or transport error."""


class _CapriHandler(shim._Handler):
    """The same protocol as the shim, but only after pairing authentication."""

    server_version = "arena-api-capri"

    def _authorized(self) -> bool:
        values = self.headers.get_all("x-api-key", [])
        try:
            supplied = values[0].encode("ascii") if len(values) == 1 else b""
        except UnicodeEncodeError:
            supplied = b""
        expected = self.server.capri_token.encode("ascii")  # type: ignore[attr-defined]
        if hmac.compare_digest(supplied, expected):
            return True
        # Do not keep a connection with an unread POST body alive.
        self.close_connection = True
        self._send_error_json(401, "authentication_error", "Invalid pairing key")
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._authorized():
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._authorized():
            super().do_POST()

    def do_OPTIONS(self) -> None:  # noqa: N802 - no cross-origin browser access
        self.close_connection = True
        self._send_error_json(405, "invalid_request_error", "OPTIONS is not supported")

    def _read_json(self) -> dict[str, Any]:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise shim.BadRequest("one Content-Length is required")
        try:
            length = int(lengths[0])
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            raise shim.BadRequest("invalid or oversized request body")
        return super()._read_json()


def _valid_token(token: str) -> bool:
    return bool(_PAIR_SECRET.fullmatch(token))


def load_or_create_token(path: Path = DEFAULT_KEY_FILE) -> str:
    """Persist a random secret locally, so the phone need not re-pair on restart.

    POSIX creation is owner-only. On Windows the file inherits the user's
    directory ACL; users should keep that directory private. The ignored state/
    directory is never committed, and a malformed existing key is not replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="ascii").strip()
    else:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as out:
            out.write(token + "\n")
    if not _valid_token(token):
        raise CapriError(f"Invalid pairing key file: {path}. Stop the server and remove it to rotate the key.")
    return token


def create_server(
    *,
    token: str,
    port: int = DEFAULT_PORT,
    engine: Any = None,
    engine_name: str = "warm",
    session: str = "auto",
    default_model: str = shim.DEFAULT_MODEL,
    extra_models: Sequence[str] = (),
    archive: bool = DEFAULT_ARCHIVE,
    verbose: bool = False,
) -> shim._Server:
    """Build an authenticated loopback-only server (``engine`` is for tests)."""
    if not _valid_token(token):
        raise ValueError("pairing key must be 32-128 URL-safe ASCII characters")
    server = shim.create_server(
        host="127.0.0.1",
        port=port,
        engine=engine,
        engine_name=engine_name,
        session=session,
        default_model=default_model,
        extra_models=extra_models,
        tools_mode="off",
        archive=archive,
        verbose=verbose,
    )
    server.capri_token = token  # type: ignore[attr-defined]
    server.RequestHandlerClass = _CapriHandler
    return server


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be a number") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def find_tailscale() -> str:
    """Find the Windows installer location as well as a CLI already in PATH."""
    explicit = os.environ.get("ARENA_CAPRI_TAILSCALE")
    if explicit:
        return explicit
    found = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if found:
        return found
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidate = Path(root) / "Tailscale" / "tailscale.exe"
            if candidate.is_file():
                return str(candidate)
    raise CapriError("Tailscale CLI not found. Install Tailscale and sign in on this PC.")


def tailscale_json(executable: str, *args: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [executable, *args, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CapriError("Cannot contact Tailscale. Is it running on this PC?") from exc
    if proc.returncode:
        raise CapriError(f"Tailscale {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise CapriError("Tailscale returned invalid JSON; update Tailscale.") from exc
    # A fresh Tailscale installation normally reports JSON null when it has
    # never had any Serve configuration. That means an empty config, not an
    # error; the ordinary device status must still be an object.
    if data is None and args == ("serve", "status"):
        return {}
    if not isinstance(data, dict):
        raise CapriError("Unexpected Tailscale status; update Tailscale.")
    return data


def tailnet_name(status: Mapping[str, Any]) -> str:
    if status.get("BackendState") != "Running":
        raise CapriError("Tailscale is not connected. Sign in and connect it on the PC.")
    own = status.get("Self")
    name = own.get("DNSName", "") if isinstance(own, Mapping) else ""
    if not isinstance(name, str):
        name = ""
    name = name.rstrip(".").lower()
    if not _TS_HOST.fullmatch(name):
        raise CapriError("No usable Tailscale MagicDNS name. Enable MagicDNS for your tailnet.")
    return name


def _configs(status: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    configs: list[Mapping[str, Any]] = [status]
    foreground = status.get("Foreground")
    if isinstance(foreground, Mapping):
        configs.extend(value for value in foreground.values() if isinstance(value, Mapping))
    return configs


def serve_port_in_use(status: Mapping[str, Any], port: int) -> bool:
    """Do not overwrite somebody else's background or foreground Serve route."""
    suffix = f":{port}"
    for config in _configs(status):
        tcp = config.get("TCP")
        if isinstance(tcp, Mapping) and str(port) in tcp:
            return True
        web = config.get("Web")
        if isinstance(web, Mapping) and any(str(key).lower().endswith(suffix) for key in web):
            return True
        funnel = config.get("AllowFunnel")
        if isinstance(funnel, Mapping) and any(
            value and str(key).lower().endswith(suffix) for key, value in funnel.items()
        ):
            return True
    return False


def serve_route_ready(status: Mapping[str, Any], host: str, port: int, target: str) -> bool:
    """Check the exact HTTPS proxy route and fail closed if Funnel is enabled."""
    address = f"{host}:{port}".lower()
    configs = _configs(status)
    for config in configs:
        funnel = config.get("AllowFunnel")
        if isinstance(funnel, Mapping) and any(
            str(key).lower() == address and bool(value) for key, value in funnel.items()
        ):
            raise CapriError("Tailscale Funnel is enabled for this port; refusing public exposure.")
    for config in configs:
        tcp, web = config.get("TCP"), config.get("Web")
        if not isinstance(tcp, Mapping) or not isinstance(web, Mapping):
            continue
        listener = tcp.get(str(port))
        if not isinstance(listener, Mapping) or listener.get("HTTPS") is not True:
            continue
        for key, entry in web.items():
            if str(key).lower() != address or not isinstance(entry, Mapping):
                continue
            handlers = entry.get("Handlers")
            handler = handlers.get("/") if isinstance(handlers, Mapping) else None
            proxy = handler.get("Proxy") if isinstance(handler, Mapping) else None
            if isinstance(proxy, str) and proxy.rstrip("/") == target.rstrip("/"):
                return True
    return False


def pairing_string(host: str, port: int, token: str) -> str:
    if not _TS_HOST.fullmatch(host) or not 1 <= port <= 65535 or not _valid_token(token):
        raise ValueError("invalid tailnet address, port, or pairing key")
    return f"arena+https://{host}:{port}#{token}"


def _print_pairing(pairing: str) -> None:
    print("\nPaste this PRIVATE pairing string into Capri > Models > Add model > API key:")
    print(pairing)
    print("Do not post it or send it to anyone. Messages are sent through your tailnet.\n")
    try:
        import qrcode
    except ImportError:
        print("Optional: install qrcode to display a scannable QR in this window.")
    else:
        try:
            code = qrcode.QRCode(border=1)
            code.add_data(pairing)
            code.make(fit=True)
            code.print_ascii(invert=True)
        except (OSError, UnicodeError, ValueError):
            print("QR display is unavailable in this terminal; use the text above.")
        else:
            print("Scan the QR on the phone, then paste the text into the API-key field.")
    print("Keep this window, Tailscale, and the PC running. Ctrl+C stops the server.", flush=True)


def _wait_for_route(
    executable: str,
    process: subprocess.Popen[Any],
    *,
    host: str,
    https_port: int,
    target: str,
    timeout: float = 180,
) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise CapriError("Tailscale Serve stopped before the HTTPS route was ready.")
        status = tailscale_json(executable, "serve", "status")
        if serve_route_ready(status, host, https_port, target):
            return
        time.sleep(1)
    raise CapriError("Tailscale Serve did not become ready. Enable MagicDNS and HTTPS certificates and retry.")


def run(args: argparse.Namespace) -> int:
    executable = find_tailscale()
    host = tailnet_name(tailscale_json(executable, "status"))
    status = tailscale_json(executable, "serve", "status")
    if serve_port_in_use(status, args.https_port):
        raise CapriError(f"Tailscale port {args.https_port} is already in use; choose --https-port without changing the existing route.")
    token = load_or_create_token(args.key_file)
    target = f"http://127.0.0.1:{args.port}"
    server = create_server(
        token=token,
        port=args.port,
        engine_name=args.engine,
        session=args.session,
        default_model=args.model,
        extra_models=args.arena_model,
        archive=args.archive,
        verbose=args.verbose,
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    process: subprocess.Popen[Any] | None = None
    try:
        print("Starting private Tailscale HTTPS Serve. If prompted, approve HTTPS in your browser.", flush=True)
        # Foreground Serve is scoped to this child process and disappears when it
        # exits. Never use --bg, `serve reset`, or Funnel, and do not bind the
        # unauthenticated original shim to a network interface.
        process = subprocess.Popen([executable, "serve", f"--https={args.https_port}", target])
        _wait_for_route(process=process, executable=executable, host=host, https_port=args.https_port, target=target)
        _print_pairing(pairing_string(host, args.https_port, token))
        if args.engine == "echo":
            print("Arena chat archiving: not applicable in echo mode.", flush=True)
        else:
            archive_status = "enabled (background)" if args.archive else "disabled"
            print(f"Arena chat archiving: {archive_status}.", flush=True)
        while True:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                status = tailscale_json(executable, "serve", "status")
                if not serve_route_ready(status, host, args.https_port, target):
                    raise CapriError("Tailscale Serve route changed or disappeared; stopping the server.")
            else:
                raise CapriError("Tailscale Serve stopped; stopping the server.")
    except KeyboardInterrupt:
        print("\nStopping Capri server.")
        return 0
    finally:
        try:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            server.engine.close()
            worker.join(timeout=5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=_port, default=os.environ.get("ARENA_CAPRI_PORT", str(DEFAULT_PORT)))
    parser.add_argument("--https-port", type=_port, default=os.environ.get("ARENA_CAPRI_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))
    parser.add_argument("--engine", choices=shim.ENGINES, default="warm", help="echo tests the connection without Arena")
    parser.add_argument("--session", default="auto", help="Arena session source (default: auto)")
    parser.add_argument("--model", default=shim.DEFAULT_MODEL, help="default advertised model (default: arena-auto)")
    parser.add_argument("--arena-model", action="append", default=[], help="advertise another Arena model ID")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE, help="private pairing key file")
    archive = parser.add_mutually_exclusive_group()
    archive.add_argument(
        "--archive", dest="archive", action="store_true", default=DEFAULT_ARCHIVE,
        help="archive each completed Arena chat (default)",
    )
    archive.add_argument(
        "--no-archive", dest="archive", action="store_false",
        help="leave completed Arena chats in the sidebar",
    )
    parser.add_argument("--verbose", action="store_true", help="log request metadata (not the pairing key)")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (CapriError, OSError, ValueError) as exc:
        print(f"[capri] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
