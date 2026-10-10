"""HTTP and incremental SSE primitives shared by both client variants.

Only credentials explicitly provided by the caller are used. Browser challenges
are reported as :class:`ChallengeRequiredError`; this transport does not solve
or bypass Cloudflare, Turnstile, CAPTCHA, or other interactive checks.
"""

from __future__ import annotations

import codecs
import json
import math
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import urljoin, urlparse

import httpx

from .constants import DEFAULT_HEADERS, DEFAULT_TIMEOUT
from .exceptions import (
    ArenaError,
    AuthenticationError,
    ChallengeRequiredError,
    ProtocolError,
    QueueTimeoutError,
    RateLimitError,
)

CookieInput: TypeAlias = (
    Mapping[str, Any] | str | Path | Sequence[Mapping[str, Any]] | None
)


_CHALLENGE_MARKERS = (
    "cf-chl-",
    "challenge-platform",
    "turnstile",
    "captcha-delivery",
    "just a moment...",
    "cf-error-details",
)


def parse_cookie_header(value: str) -> dict[str, str]:
    """Parse a DevTools ``Cookie`` request header.

    Cookie values are split at the first equals sign only, so values containing
    ``=`` (common in signed session cookies) are preserved.
    """
    result: dict[str, str] = {}
    for piece in value.split(";"):
        key, separator, cookie_value = piece.strip().partition("=")
        if key and separator:
            result[key] = cookie_value.strip()
    return result


def _cookies_from_json_value(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        nested = value.get("cookies")
        if isinstance(nested, (list, tuple)):
            return _cookies_from_json_value(nested)
        browser_cookie = value.get("cookie")
        if isinstance(browser_cookie, str):
            return parse_cookie_header(browser_cookie)
        if "name" in value and "value" in value:
            return {str(value["name"]): str(value["value"])}
        return {str(key): str(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        result: dict[str, str] = {}
        for item in value:
            if (
                not isinstance(item, Mapping)
                or "name" not in item
                or "value" not in item
            ):
                continue
            result[str(item["name"])] = str(item["value"])
        return result
    raise ValueError("cookies.json must contain an object or a cookie array")


def normalize_cookies(cookies: CookieInput) -> dict[str, str]:
    """Normalize a cookie mapping, Cookie header, or JSON export.

    Supported forms include ``{"session": "..."}``, a DevTools request
    header (``"session=...; cf_clearance=..."``), a path to a JSON export, a
    browser export with a top-level ``cookie`` field, and the standard
    ``[{"name": ..., "value": ...}]`` cookie array.
    """
    if cookies is None:
        return {}
    if isinstance(cookies, Path):
        path = cookies.expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Cookie file not found: {path}")
        try:
            return _cookies_from_json_value(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Cookie file must contain a JSON cookie array or object"
            ) from exc
    if isinstance(cookies, str):
        path = Path(cookies).expanduser()
        if path.is_file():
            return normalize_cookies(path)
        if "=" not in cookies and path.suffix.lower() == ".json":
            raise FileNotFoundError(f"Cookie file not found: {path}")
        return parse_cookie_header(cookies)
    return _cookies_from_json_value(cookies)


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str:
    if not headers:
        return ""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return ""


def _looks_like_challenge(body: str, headers: Mapping[str, Any] | None = None) -> bool:
    if _header_value(headers, "cf-mitigated").lower() == "challenge":
        return True
    lowered = body.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _retry_after(headers: Mapping[str, Any] | None) -> float | None:
    value = _header_value(headers, "retry-after").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _raise_for_status(
    status: int,
    body: str = "",
    headers: Mapping[str, Any] | None = None,
) -> None:
    """Map HTTP status codes to the public exception hierarchy."""
    if 300 <= status < 400:
        raise ProtocolError(
            f"Arena redirected the request (HTTP {status}); set base_url to the canonical frontend URL."
        )
    if status < 400:
        return
    if _looks_like_challenge(body, headers):
        raise ChallengeRequiredError(
            "The service requested an interactive browser verification. "
            "Refresh or verify the session in a normal browser; arena-api does not bypass challenges."
        )
    if status in (401, 403):
        raise AuthenticationError(f"The supplied session was rejected (HTTP {status}).")
    if status == 429:
        raise RateLimitError(
            "Arena rate limit reached.", retry_after=_retry_after(headers)
        )
    raise ArenaError(f"Arena returned HTTP {status}.")


def _raise_if_challenge_page(
    body: str, headers: Mapping[str, Any] | None = None
) -> None:
    """Detect challenge HTML served with a successful status code."""
    if _looks_like_challenge(body, headers):
        raise ChallengeRequiredError(
            "The service returned a browser verification page. "
            "Complete it in a normal browser and provide a refreshed session."
        )


def _normalize_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute http:// or https:// URL")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query string or fragment")
    return base_url.rstrip("/") + "/"


def _network_error(exc: httpx.RequestError, *, streaming: bool) -> ArenaError:
    if streaming and isinstance(exc, httpx.TimeoutException):
        return QueueTimeoutError("The Arena stream timed out before completing.")
    if isinstance(exc, httpx.TimeoutException):
        return ArenaError("The Arena HTTP request timed out.")
    return ArenaError(f"Arena network request failed ({type(exc).__name__}).")


class SSEParser:
    """Incremental SSE decoder that tolerates arbitrary byte boundaries.

    Handles UTF-8 code points split across reads, LF/CRLF/CR delimiters,
    comments, multiple ``data:`` fields, and an unterminated final event.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._event = "message"
        self._data: list[str] = []
        self._closed = False

    def feed(self, data: bytes | str) -> list[tuple[str, str]]:
        if self._closed:
            raise RuntimeError("cannot feed a closed SSEParser")
        if isinstance(data, bytes):
            self._buffer += self._decoder.decode(data, final=False)
        else:
            self._buffer += data
        return self._parse(final=False)

    def _parse(self, *, final: bool) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        while self._buffer:
            line_end = len(self._buffer)
            delimiter_size = 0
            for delimiter in ("\r", "\n"):
                position = self._buffer.find(delimiter)
                if position >= 0 and position < line_end:
                    line_end = position
                    delimiter_size = 1
            if not delimiter_size:
                break
            # A CR at the end of a chunk may be the first half of CRLF.
            if (
                self._buffer[line_end] == "\r"
                and line_end + 1 == len(self._buffer)
                and not final
            ):
                break
            if self._buffer.startswith("\r\n", line_end):
                delimiter_size = 2
            line = self._buffer[:line_end]
            self._buffer = self._buffer[line_end + delimiter_size :]
            self._consume_line(line, events)
        if final and self._buffer:
            self._consume_line(self._buffer, events)
            self._buffer = ""
        return events

    def _consume_line(self, line: str, events: list[tuple[str, str]]) -> None:
        if not line:
            if self._data:
                events.append((self._event, "\n".join(self._data)))
            self._event, self._data = "message", []
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        # id/retry and unknown fields are intentionally ignored.

    def close(self) -> list[tuple[str, str]]:
        if self._closed:
            return []
        self._buffer += self._decoder.decode(b"", final=True)
        events = self._parse(final=True)
        if self._data:
            events.append((self._event, "\n".join(self._data)))
        self._event, self._data = "message", []
        self._closed = True
        return events


class ArenaTransport:
    """Synchronous HTTP/2-capable transport backed by ``httpx``."""

    def __init__(
        self,
        base_url: str,
        *,
        cookies: CookieInput = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http2: bool = True,
        verify: bool | str = True,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite, positive number")
        self.base_url = _normalize_base_url(base_url)
        merged_headers = dict(DEFAULT_HEADERS)
        if headers:
            merged_headers.update(headers)
        self.session = httpx.Client(
            headers=merged_headers,
            cookies=normalize_cookies(cookies),
            timeout=httpx.Timeout(timeout),
            http2=http2,
            verify=verify,
            follow_redirects=False,
            transport=http_transport,
        )
        self._closed = False

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.session.request(method, self.url(path), **kwargs)
        except httpx.RequestError as exc:
            raise _network_error(exc, streaming=False) from exc
        _check_sync_response(response)
        return response

    def get_json(self, path: str, **kwargs: Any) -> Any:
        response = self._request("GET", path, **kwargs)
        body = response.text
        _raise_if_challenge_page(body, response.headers)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            content_type = response.headers.get("content-type", "unknown")
            hint = (
                " This URL appears to be a web page rather than a JSON endpoint."
                if "html" in content_type.lower()
                else ""
            )
            raise ProtocolError(
                f"Expected JSON from {path} (HTTP {response.status_code}, content-type {content_type}).{hint}"
            ) from exc

    def get_text(self, path: str, **kwargs: Any) -> str:
        response = self._request("GET", path, **kwargs)
        _raise_if_challenge_page(response.text, response.headers)
        return response.text

    def post_json(self, path: str, payload: Mapping[str, Any], **kwargs: Any) -> Any:
        response = self._request("POST", path, json=dict(payload), **kwargs)
        _raise_if_challenge_page(response.text, response.headers)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"Expected JSON from {path}.") from exc

    @contextmanager
    def stream_request(
        self,
        method: str,
        path: str,
        *,
        streaming_timeout: bool = True,
        **kwargs: Any,
    ) -> Iterator[httpx.Response]:
        try:
            with self.session.stream(method, self.url(path), **kwargs) as response:
                _check_sync_response(response, streaming=True)
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type:
                    body = response.read().decode("utf-8", "replace")
                    _raise_if_challenge_page(body, response.headers)
                    raise ProtocolError(
                        f"Expected a streaming response from {path}, received HTML."
                    )
                yield response
        except httpx.RequestError as exc:
            raise _network_error(exc, streaming=streaming_timeout) from exc

    def sse(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
        timeout: float,
    ) -> Iterator[tuple[str, Any]]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite, positive number")
        parser = SSEParser()
        deadline = time.monotonic() + timeout
        with self.stream_request(
            "GET",
            path,
            params=dict(params),
            headers={"accept": "text/event-stream"},
            timeout=httpx.Timeout(timeout),
        ) as response:
            first_chunk = True
            for wire_chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise QueueTimeoutError(
                        "Queue event stream exceeded its configured timeout."
                    )
                if first_chunk:
                    _check_stream_prefix(wire_chunk, response.headers, path)
                    first_chunk = False
                for event, data in parser.feed(wire_chunk):
                    yield event, self._decode_data(data)
            for event, data in parser.close():
                yield event, self._decode_data(data)

    @staticmethod
    def _decode_data(data: str) -> Any:
        if data.strip() == "[DONE]":
            return {"msg": "process_completed"}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data

    def close(self) -> None:
        if not self._closed:
            self.session.close()
            self._closed = True

    def __enter__(self) -> ArenaTransport:  # noqa: PYI034 - Self is not in typing until Python 3.11
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncArenaTransport:
    """Asynchronous HTTP/2-capable transport backed by ``httpx``."""

    def __init__(
        self,
        base_url: str,
        *,
        cookies: CookieInput = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http2: bool = True,
        verify: bool | str = True,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite, positive number")
        self.base_url = _normalize_base_url(base_url)
        merged_headers = dict(DEFAULT_HEADERS)
        if headers:
            merged_headers.update(headers)
        self.session = httpx.AsyncClient(
            headers=merged_headers,
            cookies=normalize_cookies(cookies),
            timeout=httpx.Timeout(timeout),
            http2=http2,
            verify=verify,
            follow_redirects=False,
            transport=http_transport,
        )
        self._closed = False

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.session.request(method, self.url(path), **kwargs)
        except httpx.RequestError as exc:
            raise _network_error(exc, streaming=False) from exc
        _check_sync_response(response)
        return response

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self._request("GET", path, **kwargs)
        _raise_if_challenge_page(response.text, response.headers)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            content_type = response.headers.get("content-type", "unknown")
            hint = (
                " This URL appears to be a web page rather than a JSON endpoint."
                if "html" in content_type.lower()
                else ""
            )
            raise ProtocolError(
                f"Expected JSON from {path} (HTTP {response.status_code}, content-type {content_type}).{hint}"
            ) from exc

    async def get_text(self, path: str, **kwargs: Any) -> str:
        response = await self._request("GET", path, **kwargs)
        _raise_if_challenge_page(response.text, response.headers)
        return response.text

    async def post_json(
        self, path: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> Any:
        response = await self._request("POST", path, json=dict(payload), **kwargs)
        _raise_if_challenge_page(response.text, response.headers)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"Expected JSON from {path}.") from exc

    @asynccontextmanager
    async def stream_request(
        self,
        method: str,
        path: str,
        *,
        streaming_timeout: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[httpx.Response]:
        try:
            async with self.session.stream(
                method, self.url(path), **kwargs
            ) as response:
                await _check_async_response(response)
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type:
                    body = (await response.aread()).decode("utf-8", "replace")
                    _raise_if_challenge_page(body, response.headers)
                    raise ProtocolError(
                        f"Expected a streaming response from {path}, received HTML."
                    )
                yield response
        except httpx.RequestError as exc:
            raise _network_error(exc, streaming=streaming_timeout) from exc

    async def sse(
        self,
        path: str,
        *,
        params: Mapping[str, Any],
        timeout: float,
    ) -> AsyncIterator[tuple[str, Any]]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite, positive number")
        parser = SSEParser()
        deadline = time.monotonic() + timeout
        async with self.stream_request(
            "GET",
            path,
            params=dict(params),
            headers={"accept": "text/event-stream"},
            timeout=httpx.Timeout(timeout),
        ) as response:
            first_chunk = True
            async for wire_chunk in response.aiter_bytes():
                if time.monotonic() >= deadline:
                    raise QueueTimeoutError(
                        "Queue event stream exceeded its configured timeout."
                    )
                if first_chunk:
                    _check_stream_prefix(wire_chunk, response.headers, path)
                    first_chunk = False
                for event, data in parser.feed(wire_chunk):
                    yield event, ArenaTransport._decode_data(data)
            for event, data in parser.close():
                yield event, ArenaTransport._decode_data(data)

    async def close(self) -> None:
        if not self._closed:
            await self.session.aclose()
            self._closed = True

    async def __aenter__(self) -> AsyncArenaTransport:  # noqa: PYI034 - Self is not in typing until Python 3.11
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def _check_sync_response(response: httpx.Response, *, streaming: bool = False) -> None:
    if (
        response.status_code < 400
        and _header_value(response.headers, "cf-mitigated").lower() == "challenge"
    ):
        raise ChallengeRequiredError(
            "The service returned a browser verification challenge; complete it in a normal browser."
        )
    if 300 <= response.status_code < 400:
        _raise_for_status(response.status_code, headers=response.headers)
    if response.status_code >= 400:
        if streaming:
            body = response.read().decode("utf-8", "replace")
        else:
            body = response.text
        _raise_for_status(response.status_code, body, response.headers)


async def _check_async_response(response: httpx.Response) -> None:
    if (
        response.status_code < 400
        and _header_value(response.headers, "cf-mitigated").lower() == "challenge"
    ):
        raise ChallengeRequiredError(
            "The service returned a browser verification challenge; complete it in a normal browser."
        )
    if 300 <= response.status_code < 400:
        _raise_for_status(response.status_code, headers=response.headers)
    if response.status_code >= 400:
        body = (await response.aread()).decode("utf-8", "replace")
        _raise_for_status(response.status_code, body, response.headers)


def _check_stream_prefix(wire: bytes, headers: Mapping[str, Any], path: str) -> None:
    prefix = wire[:4096].decode("utf-8", "replace")
    if prefix.lstrip().lower().startswith(("<!doctype html", "<html")):
        _raise_if_challenge_page(prefix, headers)
        raise ProtocolError(f"Expected an SSE stream from {path}, received HTML.")
