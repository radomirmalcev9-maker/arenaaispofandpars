"""HTTP and incremental SSE primitives.

The transport deliberately authenticates only with credentials supplied by its
caller.  It does not solve challenges or obtain browser cookies.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from curl_cffi import requests

from .constants import DEFAULT_HEADERS, DEFAULT_TIMEOUT
from .exceptions import AuthenticationError, ArenaError, ChallengeRequiredError, ProtocolError, QueueTimeoutError, RateLimitError


def parse_cookie_header(value: str) -> dict[str, str]:
    """Parse a DevTools ``Cookie`` request header without losing ``=`` values."""
    result: dict[str, str] = {}
    for piece in value.split(";"):
        key, separator, cookie_value = piece.strip().partition("=")
        if key and separator:
            result[key] = cookie_value
    return result


def normalize_cookies(cookies: Mapping[str, str] | str | Path | list[Mapping[str, Any]] | None) -> dict[str, str]:
    if cookies is None:
        return {}
    if isinstance(cookies, Path):
        cookies = str(cookies)
    if isinstance(cookies, str):
        path = Path(cookies)
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("cookies.json must contain a JSON cookie array or object") from exc
            if isinstance(loaded, Mapping) and isinstance(loaded.get("cookies"), list):
                loaded = loaded["cookies"]
            return normalize_cookies(loaded)
        return parse_cookie_header(cookies)
    if isinstance(cookies, Mapping):
        # Output from the DevTools-console helper in the documentation:
        # {"origin": "...", "userAgent": "...", "cookie": "a=b; c=d"}
        # Treat it as a browser export rather than three literal cookies.
        browser_cookie = cookies.get("cookie")
        if isinstance(browser_cookie, str):
            return parse_cookie_header(browser_cookie)
        return {str(key): str(value) for key, value in cookies.items()}
    return {str(item["name"]): str(item["value"]) for item in cookies if "name" in item and "value" in item}


def _raise_for_status(status: int, body: str = "") -> None:
    if status in (401, 403):
        lowered = body.lower()
        if any(marker in lowered for marker in ("cf-chl", "turnstile", "captcha", "challenge-platform")):
            raise ChallengeRequiredError(
                "Arena requested an interactive browser challenge. Refresh cookies in a normal browser."
            )
        raise AuthenticationError("Arena rejected the browser session (HTTP %s)." % status)
    if status == 429:
        raise RateLimitError("Arena rate limit reached.")
    if status >= 400:
        raise ArenaError("Arena HTTP error %s: %s" % (status, body[:300]))


class SSEParser:
    """Stateful SSE decoder safe for arbitrary byte boundaries and multiline data."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event = "message"
        self._data: list[str] = []

    def feed(self, data: bytes | str) -> list[tuple[str, str]]:
        self._buffer += data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        events: list[tuple[str, str]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if self._data:
                    events.append((self._event, "\n".join(self._data)))
                self._event, self._data = "message", []
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                self._event = value
            elif field == "data":
                self._data.append(value)
        return events

    def close(self) -> list[tuple[str, str]]:
        # A compliant sender ends with a blank line; accepting the final partial
        # record improves resilience to proxies closing after a completed job.
        if self._buffer:
            self.feed("\n")
        if not self._data:
            return []
        event = (self._event, "\n".join(self._data))
        self._event, self._data = "message", []
        return [event]


class ArenaTransport:
    def __init__(self, base_url: str, *, cookies: Mapping[str, str] | str | list[Mapping[str, Any]] | None = None,
                 headers: Mapping[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT,
                 impersonate: str = "chrome124") -> None:
        self.base_url = base_url.rstrip("/") + "/"
        merged = dict(DEFAULT_HEADERS)
        if headers:
            merged.update(headers)
        # Origin/referer must follow a custom self-hosted endpoint.
        origin = self.base_url.rstrip("/")
        if not headers or "origin" not in {key.lower() for key in headers}:
            merged["origin"] = origin
        if not headers or "referer" not in {key.lower() for key in headers}:
            merged["referer"] = origin + "/"
        self.session = requests.Session(headers=merged, cookies=normalize_cookies(cookies), impersonate=impersonate, timeout=timeout)

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def get_json(self, path: str, **kwargs: Any) -> Any:
        response = self.session.get(self.url(path), **kwargs)
        _raise_for_status(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            hint = ""
            if "html" in content_type.lower() or response.text.lstrip().lower().startswith("<!doctype html"):
                hint = " The selected URL is a web frontend, not a Gradio backend."
            raise ProtocolError(
                "Expected JSON response from %s (HTTP %s, content-type %s).%s"
                % (path, response.status_code, content_type, hint)
            ) from exc

    def get_text(self, path: str, **kwargs: Any) -> str:
        """Fetch a text document while applying the package's status mapping."""
        response = self.session.get(self.url(path), **kwargs)
        _raise_for_status(response.status_code, response.text)
        return response.text

    def post_json(self, path: str, payload: Mapping[str, Any], **kwargs: Any) -> Any:
        response = self.session.post(self.url(path), json=dict(payload), **kwargs)
        _raise_for_status(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise ProtocolError("Expected JSON response from %s" % path) from exc

    def sse(self, path: str, *, params: Mapping[str, Any], timeout: float) -> Iterator[tuple[str, Any]]:
        parser, last_activity = SSEParser(), time.monotonic()
        with self.session.stream("GET", self.url(path), params=dict(params), headers={"accept": "text/event-stream"}, timeout=timeout) as response:
            _raise_for_status(response.status_code, response.text)
            for wire_chunk in response.iter_content(chunk_size=1024):
                now = time.monotonic()
                if now - last_activity > timeout:
                    raise QueueTimeoutError("No SSE data received before queue timeout.")
                if not wire_chunk:
                    continue
                last_activity = now
                for event, data in parser.feed(wire_chunk):
                    yield event, self._decode_data(data)
        for event, data in parser.close():
            yield event, self._decode_data(data)

    @staticmethod
    def _decode_data(data: str) -> Any:
        if data == "[DONE]":
            return {"msg": "process_completed"}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data

    def close(self) -> None:
        self.session.close()


class AsyncArenaTransport:
    """Async equivalent of :class:`ArenaTransport` using curl_cffi.AsyncSession."""

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        sync = ArenaTransport(base_url, **kwargs)
        self.base_url = sync.base_url
        self.session = requests.AsyncSession(headers=sync.session.headers, cookies=sync.session.cookies, impersonate=kwargs.get("impersonate", "chrome124"), timeout=kwargs.get("timeout", DEFAULT_TIMEOUT))
        sync.close()

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self.session.get(self.url(path), **kwargs)
        _raise_for_status(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise ProtocolError("Expected JSON response from %s" % path) from exc

    async def get_text(self, path: str, **kwargs: Any) -> str:
        response = await self.session.get(self.url(path), **kwargs)
        _raise_for_status(response.status_code, response.text)
        return response.text

    async def post_json(self, path: str, payload: Mapping[str, Any], **kwargs: Any) -> Any:
        response = await self.session.post(self.url(path), json=dict(payload), **kwargs)
        _raise_for_status(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise ProtocolError("Expected JSON response from %s" % path) from exc

    async def sse(self, path: str, *, params: Mapping[str, Any], timeout: float) -> AsyncIterator[tuple[str, Any]]:
        parser, last_activity = SSEParser(), time.monotonic()
        async with self.session.stream("GET", self.url(path), params=dict(params), headers={"accept": "text/event-stream"}, timeout=timeout) as response:
            _raise_for_status(response.status_code, await response.atext())
            async for wire_chunk in response.aiter_content(chunk_size=1024):
                now = time.monotonic()
                if now - last_activity > timeout:
                    raise QueueTimeoutError("No SSE data received before queue timeout.")
                if not wire_chunk:
                    continue
                last_activity = now
                for event, data in parser.feed(wire_chunk):
                    yield event, ArenaTransport._decode_data(data)
        for event, data in parser.close():
            yield event, ArenaTransport._decode_data(data)

    async def close(self) -> None:
        await self.session.close()
