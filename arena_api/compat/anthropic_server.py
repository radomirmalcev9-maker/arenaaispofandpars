"""Anthropic-compatible HTTP surface backed by Arena Direct Chat.

Claude Code and the Claude Agent SDK talk to ``api.anthropic.com`` with the
Messages API.  This module serves that same wire protocol from a loopback
socket, so pointing ``ANTHROPIC_BASE_URL`` at it makes Claude Code run on the
models your own ``arena_api`` session has access to::

    arena-api --claude --port 8082
    ANTHROPIC_BASE_URL=http://127.0.0.1:8082 claude

What is implemented:

* ``POST /v1/messages`` -- streaming (``text/event-stream``) and non-streaming,
  with ``system``, multi-turn ``messages``, ``stop_sequences`` and ``usage``.
* ``POST /v1/messages/count_tokens`` and ``GET /v1/models`` -- what the client
  probes before it starts talking.
* **Tool use emulation.**  Arena's Direct Chat answers with plain text, so
  ``tools`` are rendered into the prompt as a strict call format and the
  answer is parsed back into ``tool_use`` blocks with ``stop_reason:
  "tool_use"``.  Without this Claude Code cannot act on the machine at all.

Answer generation goes through the same two engines the CLI uses: the warm
daemon (a browser already sitting on a loaded chat page) and the in-process
:class:`~arena_api.direct.DirectArenaClient` for the first, cold request or
when no daemon is wanted.  Neither the Anthropic protocol nor the session
handling is reimplemented here -- this module only translates.

Everything runs on the loopback interface, so the session cookies the library
already discovered are what authenticates the requests: no key is needed.
Set ``ANTHROPIC_API_KEY`` to any non-empty value because Claude Code insists on
one.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from ..constants import DEFAULT_BASE_URL

#: Port Claude Code's ecosystem expects for a local shim.
DEFAULT_PORT = 8082
DEFAULT_HOST = "127.0.0.1"

#: Model name reported when the request does not ask for a specific Arena
#: model; also what ``/v1/models`` advertises first.
DEFAULT_MODEL = "arena-auto"

ANTHROPIC_VERSION = "2023-06-01"

#: Engines, in order of preference for the default ``"warm"`` setting.
ENGINES = ("warm", "http", "echo")

#: ``emulate`` renders tool definitions into the prompt and parses the answer;
#: ``off`` ignores ``tools`` entirely and answers as plain text.
TOOLS_MODES = ("emulate", "off")

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?P<body>.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE
)
_FENCED_RE = re.compile(r"```[a-zA-Z_]*\s*(?P<body>\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _estimate_tokens(text: str) -> int:
    """Rough token count (~4 characters per token, never zero)."""
    return max(1, len(text) // 4)


def _new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _new_tool_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _as_text(value: Any) -> str:
    """Flatten the many shapes an Anthropic ``content`` field can take."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        # A bare block, or the ``{"text": ...}`` shape some SDKs send.
        inner = value.get("text")
        return inner if isinstance(inner, str) else ""
    if isinstance(value, Sequence):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, Mapping):
                kind = str(item.get("type") or "")
                if kind == "image":
                    parts.append("[image omitted]")
                    continue
                text = _as_text(item.get("text", item.get("content", "")))
                if text:
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def _system_text(payload: Mapping[str, Any]) -> str:
    """The ``system`` field is a string or a list of text blocks."""
    system = payload.get("system")
    if system is None:
        return ""
    return _as_text(system).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str) and item]


# --------------------------------------------------------------------------- #
# Prompt rendering
# --------------------------------------------------------------------------- #


def render_transcript(messages: Sequence[Mapping[str, Any]]) -> str:
    """Render Anthropic messages as one plain-text transcript.

    Arena gets a single prompt, so the conversation history, the tool results
    of the previous turns and the assistant's own tool calls all have to be
    visible in it -- otherwise Claude Code's agent loop restarts from scratch
    every turn.
    """
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            body = content
        else:
            parts: list[str] = []
            for block in content if isinstance(content, Sequence) else []:
                if not isinstance(block, Mapping):
                    continue
                kind = str(block.get("type") or "text")
                if kind == "text":
                    text = _as_text(block.get("text", ""))
                    if text:
                        parts.append(text)
                elif kind == "tool_use":
                    name = str(block.get("name") or "tool")
                    raw_input = block.get("input")
                    rendered = json.dumps(
                        raw_input if isinstance(raw_input, Mapping) else {},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    tool_id = str(block.get("id") or "")
                    parts.append(f"[tool_use id={tool_id} name={name}] {rendered}")
                elif kind == "tool_result":
                    tool_id = str(block.get("tool_use_id") or "")
                    body_text = _as_text(block.get("content", ""))
                    is_error = bool(block.get("is_error"))
                    label = "tool_result(error)" if is_error else "tool_result"
                    parts.append(f"[{label} id={tool_id}]\n{body_text}")
                elif kind == "image":
                    parts.append("[image omitted]")
            body = "\n".join(part for part in parts if part)
        body = body.strip()
        if not body:
            continue
        lines.append(f"[{role}]\n{body}")
    return "\n\n".join(lines)


def _tool_signature(tool: Mapping[str, Any]) -> str:
    schema = tool.get("input_schema")
    props: list[str] = []
    required: list[str] = []
    if isinstance(schema, Mapping):
        raw_props = schema.get("properties")
        if isinstance(raw_props, Mapping):
            for key, spec in raw_props.items():
                kind = ""
                enum = None
                if isinstance(spec, Mapping):
                    kind = str(spec.get("type") or "")
                    raw_enum = spec.get("enum")
                    if isinstance(raw_enum, Sequence) and not isinstance(
                        raw_enum, (str, bytes)
                    ):
                        enum = [str(item) for item in raw_enum][:12]
                if enum:
                    props.append(f'"{key}": {"|".join(enum)}')
                elif kind:
                    props.append(f'"{key}": {kind}')
                else:
                    props.append(f'"{key}": any')
        raw_required = schema.get("required")
        if isinstance(raw_required, Sequence) and not isinstance(
            raw_required, (str, bytes)
        ):
            required = [str(item) for item in raw_required]
    signature = "{" + ", ".join(props) + "}"
    if required:
        signature += " (required: " + ", ".join(required) + ")"
    return signature


def render_tool_instructions(
    tools: Sequence[Mapping[str, Any]],
    tool_choice: Mapping[str, Any] | None = None,
) -> str:
    """Describe the tools and the exact call format Arena has to answer with."""
    if not tools:
        return ""
    lines = [
        "### Tools",
        "You can act by calling tools. A tool call is ONLY recognised when it is",
        "written as this exact block, at the end of your message, outside any",
        "markdown fence and without additional commentary inside it:",
        "",
        '<tool_call>{"name": "<tool name>", "arguments": {"<argument>": <value>}}</tool_call>',
        "",
        "Rules:",
        "1. Emit a tool call only when you need information or an action.",
        "2. One block per call; several blocks are allowed when calls are independent.",
        "3. `arguments` must be a JSON object matching the tool's schema exactly.",
        "4. A file path or other value shown above must be copied verbatim.",
        "5. When no tool is needed, answer normally -- never invent the result of",
        "   a tool you did not call, and never write pseudo tool output.",
        "6. After a tool call you will receive `[tool_result ...]`. Continue from it.",
        "",
        "Available tools:",
    ]
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        description = str(tool.get("description") or "").strip()
        if len(description) > 900:
            description = description[:900].rstrip() + "..."
        lines.append(f"- {name}{_tool_signature(tool)}")
        if description:
            lines.append(f"    {description}")
    choice = tool_choice if isinstance(tool_choice, Mapping) else {}
    choice_type = str(choice.get("type") or "").strip().lower()
    if choice_type == "any":
        lines.extend(["", "You MUST call one of the tools in this turn."])
    elif choice_type == "tool":
        forced = str(choice.get("name") or "")
        if forced:
            lines.extend(["", f"You MUST call the tool `{forced}` in this turn."])
    return "\n".join(lines)


def render_prompt(
    *,
    system: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    tool_choice: Mapping[str, Any] | None,
    max_chars: int,
) -> str:
    """Assemble the single prompt Arena is asked to answer."""
    sections: list[str] = []
    if system:
        sections.append("### Instructions\n" + system)
    transcript = render_transcript(messages)
    if transcript:
        sections.append("### Conversation\n" + transcript)
    tool_block = render_tool_instructions(tools, tool_choice)
    if tool_block:
        # Last, so the format is the most recent thing the model read.
        sections.append(tool_block)
    prompt = "\n\n".join(sections).strip()
    if max_chars > 0 and len(prompt) > max_chars:
        head = int(max_chars * 0.75)
        tail = max_chars - head
        prompt = (
            prompt[:head]
            + "\n\n[... middle of the context omitted (too long) ...]\n\n"
            + prompt[-tail:]
        )
    return prompt


# --------------------------------------------------------------------------- #
# Tool-call parsing
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {"input": raw}
        if isinstance(decoded, Mapping):
            return {str(key): value for key, value in decoded.items()}
        return {"input": decoded}
    return {}


def _tool_call_from_obj(
    obj: Any, *, require_arguments: bool = False
) -> ToolCall | None:
    """Interpret one decoded object as a tool call.

    ``require_arguments`` is used for the least trusted shape (a bare JSON
    object in prose): there, ``{"name": "..."}`` alone is far more likely to be
    an example than a call, so an argument field is mandatory.
    """
    if not isinstance(obj, Mapping):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str) or not name.strip():
        return None
    for key in ("arguments", "input", "args", "parameters"):
        if key in obj:
            return ToolCall(name.strip(), _coerce_arguments(obj[key]))
    if require_arguments:
        return None
    return ToolCall(name.strip(), {})


def parse_tool_calls(text: str) -> list[tuple[int, int, ToolCall]]:
    """Find tool calls in an answer, with the span each one occupies.

    Three shapes are accepted, in decreasing order of trust: the fenced block
    the prompt asks for, a markdown ```json fence (models like to add one),
    and finally a bare JSON object that names a tool.  Everything else is left
    alone so prose that merely mentions ``{"name": ...}`` is not executed.
    """
    spans: list[tuple[int, int, ToolCall]] = []
    for match in _TOOL_CALL_RE.finditer(text):
        body = match.group("body")
        try:
            decoded = json.loads(body)
        except (TypeError, ValueError):
            decoded = None
        call = _tool_call_from_obj(decoded)
        if call is None:
            # Tolerate a stray fence inside the tag.
            inner = _FENCED_RE.search(body)
            if inner is not None:
                try:
                    call = _tool_call_from_obj(json.loads(inner.group("body")))
                except (TypeError, ValueError):
                    call = None
        if call is not None:
            spans.append((match.start(), match.end(), call))
    if spans:
        return spans

    for match in _FENCED_RE.finditer(text):
        try:
            call = _tool_call_from_obj(json.loads(match.group("body")))
        except (TypeError, ValueError):
            call = None
        if call is not None:
            spans.append((match.start(), match.end(), call))
    if spans:
        return spans

    for match in _JSON_OBJ_RE.finditer(text):
        try:
            call = _tool_call_from_obj(
                json.loads(match.group(0)), require_arguments=True
            )
        except (TypeError, ValueError):
            call = None
        if call is not None:
            spans.append((match.start(), match.end(), call))
    return spans


def build_content_blocks(
    text: str, *, allow_tools: bool
) -> tuple[list[dict[str, Any]], str | None]:
    """Turn an answer into Anthropic ``content`` blocks.

    Returns the blocks and the ``stop_reason`` that belongs to them.
    """
    spans = parse_tool_calls(text) if allow_tools else []
    if not spans:
        return [{"type": "text", "text": text}], "end_turn"
    blocks: list[dict[str, Any]] = []
    cursor = 0
    for start, end, call in spans:
        chunk = text[cursor:start]
        if chunk.strip():
            blocks.append({"type": "text", "text": chunk.strip()})
        blocks.append(
            {
                "type": "tool_use",
                "id": _new_tool_id(),
                "name": call.name,
                "input": call.arguments,
            }
        )
        cursor = end
    tail = text[cursor:]
    if tail.strip():
        blocks.append({"type": "text", "text": tail.strip()})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks, "tool_use"


# --------------------------------------------------------------------------- #
# Request translation
# --------------------------------------------------------------------------- #


class BadRequest(ValueError):
    """A request the shim cannot answer as sent."""


@dataclass(slots=True)
class MessagesRequest:
    model: str
    arena_model: str | None
    stream: bool
    max_tokens: int
    temperature: float | None
    top_p: float | None
    stop_sequences: list[str]
    tools: list[Mapping[str, Any]]
    tool_choice: Mapping[str, Any] | None
    prompt: str
    tools_mode: str

    @property
    def allows_tools(self) -> bool:
        if self.tools_mode != "emulate" or not self.tools:
            return False
        choice = self.tool_choice if isinstance(self.tool_choice, Mapping) else {}
        return str(choice.get("type") or "auto").lower() != "none"


#: Names that mean "let Arena choose", not a model of its own.
_AUTO_ALIASES = frozenset(
    {"", "auto", "arena", "default", "any", "arena-auto", "auto:arena"}
)


def resolve_arena_model(name: str | None) -> str | None:
    """Map a request's model name to an Arena model (``None`` = Arena picks).

    ``arena:<model>`` is accepted so a request can be explicit that the name is
    an Arena one; ``arena-auto`` and the usual "auto"/"default" spellings mean
    the session's own default model, which is what a bare Claude Code run wants.
    """
    if not isinstance(name, str):
        return None
    candidate = name.strip()
    if candidate.lower().startswith("arena:"):
        candidate = candidate.split(":", 1)[1].strip()
    if not candidate or candidate.lower() in _AUTO_ALIASES:
        return None
    return candidate


def translate_request(
    payload: Mapping[str, Any],
    *,
    default_model: str = DEFAULT_MODEL,
    tools_mode: str = "emulate",
    max_prompt_chars: int = 200_000,
) -> MessagesRequest:
    """Validate an Anthropic request and render it as one Arena prompt."""
    messages = payload.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise BadRequest("field 'messages' must be an array")
    if not messages:
        raise BadRequest("field 'messages' must not be empty")
    tools_raw = payload.get("tools")
    tools: list[Mapping[str, Any]] = []
    if isinstance(tools_raw, Sequence) and not isinstance(tools_raw, (str, bytes)):
        tools = [tool for tool in tools_raw if isinstance(tool, Mapping)]
    system = _system_text(payload)
    prompt = render_prompt(
        system=system,
        messages=[item for item in messages if isinstance(item, Mapping)],
        tools=tools if tools_mode == "emulate" else [],
        tool_choice=payload.get("tool_choice")
        if isinstance(payload.get("tool_choice"), Mapping)
        else None,
        max_chars=max_prompt_chars,
    )
    if not prompt:
        raise BadRequest("nothing to answer: every message was empty")
    raw_max_tokens = payload.get("max_tokens")
    max_tokens = raw_max_tokens if isinstance(raw_max_tokens, int) else 4096
    raw_temperature = payload.get("temperature")
    raw_top_p = payload.get("top_p")
    requested = payload.get("model")
    model = (
        requested.strip()
        if isinstance(requested, str) and requested.strip()
        else default_model
    )
    # A request that names the shim's default model means "the model the shim
    # was started with", so it resolves exactly like an explicitly named one.
    return MessagesRequest(
        model=model,
        arena_model=resolve_arena_model(model),
        stream=bool(payload.get("stream")),
        max_tokens=max(1, max_tokens),
        temperature=raw_temperature if isinstance(raw_temperature, (int, float)) else None,
        top_p=raw_top_p if isinstance(raw_top_p, (int, float)) else None,
        stop_sequences=_string_list(payload.get("stop_sequences")),
        tools=tools,
        tool_choice=payload.get("tool_choice")
        if isinstance(payload.get("tool_choice"), Mapping)
        else None,
        prompt=prompt,
        tools_mode=tools_mode,
    )


def apply_stop_sequences(text: str, stops: Sequence[str]) -> tuple[str, str | None]:
    """Cut the answer at the earliest stop sequence."""
    best_index: int | None = None
    best_stop: str | None = None
    for stop in stops:
        if not stop:
            continue
        index = text.find(stop)
        if index >= 0 and (best_index is None or index < best_index):
            best_index = index
            best_stop = stop
    if best_index is None or best_stop is None:
        return text, None
    return text[:best_index], best_stop


# --------------------------------------------------------------------------- #
# Engines: where the answer actually comes from
# --------------------------------------------------------------------------- #


class EngineError(RuntimeError):
    """The answer could not be produced."""


class _EchoEngine:
    """Debug backend: streams the prompt back, no session or browser needed."""

    name = "echo"

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        del model
        body = "echo\n\n" + prompt
        for start in range(0, len(body), 120):
            yield body[start : start + 120]

    def close(self) -> None:
        return None


class _WarmEngine:
    """Answers through the warm daemon: a browser already on a chat page."""

    name = "warm"

    def __init__(self, *, base_url: str) -> None:
        self._base_url = base_url

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        from .. import warm

        state = warm.ensure_daemon(
            payload={"argv": [], "base_url": self._base_url},
            base_url=self._base_url,
        )
        yield from warm.ask_stream(prompt, state=state, model=model)

    def close(self) -> None:
        return None


class _HttpEngine:
    """Answers with an in-process Direct Chat client (cold start, no daemon)."""

    name = "http"

    def __init__(self, *, base_url: str) -> None:
        self._base_url = base_url
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is None:
            from ..auto import auto_direct_client

            self._client = auto_direct_client(
                base_url=self._base_url, verification_token="auto", prewarm=True
            )
        return self._client

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        with self._lock:
            client = self._get_client()
            stream = client.stream(prompt, model=model)
            try:
                for chunk in stream:
                    delta = getattr(chunk, "delta", "")
                    if isinstance(delta, str) and delta:
                        yield delta
            finally:
                close_fn = getattr(stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:  # noqa: BLE001 - best effort
                        pass

    def close(self) -> None:
        client = self._client
        self._client = None
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # noqa: BLE001 - best effort
                pass


class ArenaEngine:
    """Serialises prompts and picks the backend, with a graceful fallback."""

    def __init__(
        self,
        *,
        engine: str = "warm",
        base_url: str = DEFAULT_BASE_URL,
        tools_mode: str = "emulate",
        prompt: str = "",
        log: Any = None,
    ) -> None:
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}")
        if tools_mode not in TOOLS_MODES:
            raise ValueError(f"tools_mode must be one of {TOOLS_MODES}")
        self.engine = engine
        self.base_url = base_url
        self.tools_mode = tools_mode
        self.model_hint = prompt
        self.log = log
        self.verbose = False
        self._lock = threading.Lock()
        self._primary: Any
        self._fallback: Any = None
        if engine == "echo":
            self._primary = _EchoEngine()
        elif engine == "http":
            self._primary = _HttpEngine(base_url=base_url)
        else:
            self._primary = _WarmEngine(base_url=base_url)
            # A cold or unavailable daemon must not turn into a 500: the same
            # direct client the CLI falls back to answers instead.
            self._fallback = _HttpEngine(base_url=base_url)

    def _note(self, message: str) -> None:
        log = self.log
        if callable(log):
            try:
                log(message)
                return
            except Exception:  # noqa: BLE001
                pass
        print(f"[arena-api] {message}", file=sys.stderr, flush=True)

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        with self._lock:
            stream = self._primary.answer(prompt, model=model)
            try:
                first = next(stream)
            except StopIteration:
                return
            except Exception as exc:  # noqa: BLE001 - fallback path
                self._note(f"{self._primary.name} engine failed ({exc}); falling back")
                if self._fallback is None:
                    raise EngineError(str(exc)) from exc
                stream = self._fallback.answer(prompt, model=model)
                yield from stream
                return
            yield first
            try:
                yield from stream
            finally:
                close_fn = getattr(stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:  # noqa: BLE001
                        pass

    def close(self) -> None:
        for engine in (self._primary, self._fallback):
            close_fn = getattr(engine, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------- #
# Response shaping
# --------------------------------------------------------------------------- #


def build_message(
    request: MessagesRequest,
    blocks: Sequence[Mapping[str, Any]],
    *,
    stop_reason: str,
    stop_sequence: str | None,
    usage: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "id": _new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": request.model,
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": dict(usage),
    }


def _usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def _address_url(address: Any) -> str:
    """``http://host:port`` for a socket address, whatever shape it has."""
    if isinstance(address, (tuple, list)) and len(address) >= 2:
        return f"http://{address[0]}:{address[1]}"
    text = address.decode("utf-8", "replace") if isinstance(address, bytes) else str(address)
    return f"http://{text}"


def _log_quiet(message: str) -> None:
    print(f"[arena-api] {message}", file=sys.stderr, flush=True)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "arena-api-claude"
    sys_version = ""
    #: A kept-alive connection that nobody reuses must not pin a thread for
    #: the lifetime of the process: socketserver turns this into a socket
    #: timeout, after which the handler drops the connection.
    timeout = 120

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # One line per request is useful; the default one is too noisy with a
        # loopback client that retries health checks.
        engine = getattr(self.server, "engine", None)
        if getattr(engine, "verbose", False):
            _log_quiet(f"{self.address_string()} {format % args}")

    @property
    def engine(self) -> ArenaEngine:
        return self.server.engine  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("request-id", "req_" + uuid.uuid4().hex[:16])
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(
        self, status: int, kind: str, message: str
    ) -> None:
        self._send_json(
            status, {"type": "error", "error": {"type": kind, "message": message}}
        )

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("content-length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        if not body:
            raise BadRequest("empty request body")
        try:
            decoded = json.loads(body)
        except ValueError as exc:
            raise BadRequest(f"invalid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise BadRequest("request body must be a JSON object")
        return decoded

    def _path(self) -> str:
        return urlsplit(self.path).path.rstrip("/") or "/"

    # -- SSE --------------------------------------------------------------- #

    def _begin_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        # One event stream per connection: nothing is ever reused after it, and
        # announcing keep-alive would leave the socket (and the handler thread)
        # waiting for a request that never comes.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("request-id", "req_" + uuid.uuid4().hex[:16])
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _sse(self, event: str, data: Mapping[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self._write_chunk(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))

    def _end_sse(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True

    # -- routes ------------------------------------------------------------ #

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self._path()
        if path in {"/", "/v1", "/info"}:
            self._send_json(200, self._info())
            return
        if path in {"/health", "/healthz"}:
            self._send_json(200, {"ok": True, "engine": self.engine.engine})
            return
        if path == "/v1/models":
            self._send_json(200, self._models_payload())
            return
        if path.startswith("/v1/models/"):
            model_id = path.rsplit("/", 1)[-1]
            self._send_json(200, {"type": "model", "id": model_id, **self._model_entry()})
            return
        self._send_error_json(404, "not_found_error", f"unknown path {path}")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self._path()
        try:
            if path == "/v1/messages":
                self._handle_messages()
                return
            if path == "/v1/messages/count_tokens":
                payload = self._read_json()
                try:
                    request = translate_request(
                        payload,
                        default_model=self.server.default_model,  # type: ignore[attr-defined]
                        tools_mode=self.engine.tools_mode,
                        max_prompt_chars=self.server.max_prompt_chars,  # type: ignore[attr-defined]
                    )
                except BadRequest:
                    # Counting tokens must never fail the client's context math.
                    request = None
                tokens = _estimate_tokens(request.prompt) if request else 0
                self._send_json(200, {"input_tokens": tokens})
                return
            self._send_error_json(404, "not_found_error", f"unknown path {path}")
        except BadRequest as exc:
            self._send_error_json(400, "invalid_request_error", str(exc))
        except EngineError as exc:
            self._send_error_json(502, "api_error", str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return

    # -- payloads ---------------------------------------------------------- #

    def _model_entry(self) -> dict[str, Any]:
        return {
            "display_name": "Arena (Direct Chat)",
            "created_at": "2025-01-01T00:00:00Z",
            "type": "model",
        }

    def _models_payload(self) -> dict[str, Any]:
        ids = [self.server.default_model]  # type: ignore[attr-defined]
        extra = self.server.extra_models  # type: ignore[attr-defined]
        for model in extra:
            if model not in ids:
                ids.append(model)
        return {
            "data": [
                {"type": "model", "id": model_id, **self._model_entry()}
                for model_id in ids
            ],
            "has_more": False,
            "first_id": ids[0],
            "last_id": ids[-1],
        }

    def _info(self) -> dict[str, Any]:
        return {
            "name": "arena-api anthropic shim",
            "engine": self.engine.engine,
            "tools": self.engine.tools_mode,
            "default_model": self.server.default_model,  # type: ignore[attr-defined]
            "base_url": getattr(self.engine, "base_url", None),
            "endpoints": [
                "POST /v1/messages",
                "POST /v1/messages/count_tokens",
                "GET /v1/models",
            ],
            "claude_code": {
                "ANTHROPIC_BASE_URL": _address_url(self.server.server_address),
                "ANTHROPIC_API_KEY": "arena",
            },
        }

    # -- the one endpoint that matters ------------------------------------- #

    def _handle_messages(self) -> None:
        payload = self._read_json()
        request = translate_request(
            payload,
            default_model=self.server.default_model,  # type: ignore[attr-defined]
            tools_mode=self.engine.tools_mode,
            max_prompt_chars=self.server.max_prompt_chars,  # type: ignore[attr-defined]
        )
        if request.stream:
            self._stream_message(request)
        else:
            self._complete_message(request)

    def _complete_message(self, request: MessagesRequest) -> None:
        started = time.monotonic()
        collected: list[str] = []
        for delta in self.engine.answer(request.prompt, model=request.arena_model):
            collected.append(delta)
        text, stop_sequence = apply_stop_sequences(
            "".join(collected), request.stop_sequences
        )
        blocks, inferred = build_content_blocks(
            text, allow_tools=request.allows_tools
        )
        stop_reason = "stop_sequence" if stop_sequence is not None else (inferred or "end_turn")
        message = build_message(
            request,
            blocks,
            stop_reason=stop_reason,
            stop_sequence=stop_sequence,
            usage=_usage(
                _estimate_tokens(request.prompt), _estimate_tokens(text)
            ),
        )
        if self.server.verbose:  # type: ignore[attr-defined]
            _log_quiet(
                f"answered in {time.monotonic() - started:.2f}s "
                f"({len(text)} chars, stop={stop_reason})"
            )
        self._send_json(200, message)

    #: Characters held back while streaming, so a half-written ``<tool_call>``
    #: or a stop sequence split across two deltas is never emitted as text.
    HOLDBACK = 64

    def _stream_message(self, request: MessagesRequest) -> None:
        started = time.monotonic()
        allow_tools = request.allows_tools
        self._begin_sse()
        input_tokens = _estimate_tokens(request.prompt)
        self._sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": _new_message_id(),
                    "type": "message",
                    "role": "assistant",
                    "model": request.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": _usage(input_tokens, 1),
                },
            },
        )
        # The client is watching for the first event: flush the headers now
        # instead of after Arena's first token.
        self._sse("ping", {"type": "ping"})

        buffer = ""
        streamed = 0
        index = 0
        text_open = False
        stopped = False
        stop_sequence: str | None = None
        stop_reason: str = "end_turn"

        def open_text() -> None:
            nonlocal text_open
            if text_open:
                return
            self._sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            text_open = True

        def emit_text(chunk: str) -> None:
            nonlocal streamed
            if not chunk:
                return
            open_text()
            self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": chunk},
                },
            )
            streamed += len(chunk)

        def close_text() -> None:
            nonlocal index, text_open
            if not text_open:
                return
            self._sse("content_block_stop", {"type": "content_block_stop", "index": index})
            text_open = False
            index += 1

        def emit_tool(block: Mapping[str, Any]) -> None:
            nonlocal index
            self._sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": {},
                    },
                },
            )
            self._sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                },
            )
            self._sse(
                "content_block_stop", {"type": "content_block_stop", "index": index}
            )
            index += 1

        try:
            halted = False
            try:
                for delta in self.engine.answer(
                    request.prompt, model=request.arena_model
                ):
                    if not delta:
                        continue
                    buffer += delta
                    if allow_tools and not halted and _may_be_tool_call(buffer):
                        # A call could be forming: stop streaming text and
                        # decide once the answer is complete.
                        halted = True
                    limit = len(buffer)
                    if not halted:
                        limit -= self.HOLDBACK
                    stop_at, matched = self._find_stop(buffer, request.stop_sequences)
                    if matched is not None:
                        stop_sequence = matched
                        if stop_at is not None:
                            limit = min(limit, stop_at)
                        stopped = True
                    if limit > streamed:
                        emit_text(buffer[streamed:limit])
                    if stopped:
                        break
            except (BrokenPipeError, ConnectionResetError):
                raise

            if stop_sequence is not None:
                stop_reason = "stop_sequence"

            if allow_tools:
                spans = parse_tool_calls(buffer)
            else:
                spans = []
            if spans:
                first_start = spans[0][0]
                last_end = spans[-1][1]
                if first_start > streamed:
                    emit_text(buffer[streamed:first_start])
                close_text()
                for _, _, call in spans:
                    emit_tool(
                        {
                            "type": "tool_use",
                            "id": _new_tool_id(),
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                tail = "" if stop_sequence is not None else buffer[last_end:]
                if tail.strip():
                    emit_text(tail.strip())
                close_text()
                stop_reason = "tool_use"
            else:
                if stop_sequence is not None:
                    if stop_at is not None and stop_at > streamed:
                        emit_text(buffer[streamed:stop_at])
                elif not stopped:
                    # Everything still held back is plain text.
                    emit_text(buffer[streamed:])
                close_text()
                if stop_sequence is not None:
                    stop_reason = "stop_sequence"

            self._sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": stop_reason,
                        "stop_sequence": stop_sequence,
                    },
                    "usage": _usage(input_tokens, max(1, streamed // 4)),
                },
            )
            self._sse("message_stop", {"type": "message_stop"})
            self._end_sse()
            if self.server.verbose:  # type: ignore[attr-defined]
                _log_quiet(
                    f"streamed in {time.monotonic() - started:.2f}s "
                    f"({streamed} chars, blocks={index}, stop={stop_reason})"
                )
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001 - the client must see a reason
            try:
                self._sse(
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": str(exc)},
                    },
                )
                self._end_sse()
            except (BrokenPipeError, ConnectionResetError):
                pass

    @staticmethod
    def _find_stop(text: str, stops: Sequence[str]) -> tuple[int | None, str | None]:
        """Earliest stop sequence in ``text``, and which one it is."""
        best: tuple[int, str] | None = None
        for stop in stops:
            if not stop:
                continue
            index = text.find(stop)
            if index >= 0 and (best is None or index < best[0]):
                best = (index, stop)
        if best is None:
            return None, None
        return best[0], best[1]


def _may_be_tool_call(text: str) -> bool:
    """True when ``text`` can still turn into a tool call.

    Only the two shapes :func:`parse_tool_calls` trusts are considered: the
    ``<tool_call>`` tag and a markdown fence.  A bare JSON object is not a
    reason to stop streaming -- prose full of braces is common, and the call is
    still recognised at the end.
    """
    lowered = text.lower()
    return "<tool" in lowered or "```" in lowered


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], engine: ArenaEngine, **config: Any):
        super().__init__(address, _Handler)
        self.engine = engine
        self.default_model = str(config.get("default_model") or DEFAULT_MODEL)
        self.extra_models = list(config.get("extra_models") or [])
        self.max_prompt_chars = int(config.get("max_prompt_chars") or 200_000)
        self.verbose = bool(config.get("verbose"))


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    engine: Any = None,
    engine_name: str = "warm",
    base_url: str = DEFAULT_BASE_URL,
    default_model: str = DEFAULT_MODEL,
    extra_models: Sequence[str] = (),
    tools_mode: str = "emulate",
    max_prompt_chars: int = 200_000,
    verbose: bool = False,
    log: Any = None,
) -> _Server:
    """Build the HTTP server; ``engine`` overrides everything for tests."""
    resolved = engine if engine is not None else ArenaEngine(
        engine=engine_name, base_url=base_url, tools_mode=tools_mode, log=log
    )
    server = _Server(
        (host, port),
        resolved,
        default_model=default_model,
        extra_models=extra_models,
        max_prompt_chars=max_prompt_chars,
        verbose=verbose,
    )
    with_verbose = getattr(resolved, "__dict__", None)
    if isinstance(with_verbose, dict):
        resolved.verbose = verbose
    return server


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _serve_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="arena-api --claude",
        description=(
            "Serve the Anthropic Messages API on loopback, answered by Arena "
            "Direct Chat. Point ANTHROPIC_BASE_URL at it to run Claude Code on "
            "your own Arena models."
        ),
    )
    parser.add_argument(
        "--host",
        default=_env("ARENA_CLAUDE_HOST", DEFAULT_HOST),
        help=f"interface to bind (default {DEFAULT_HOST}; loopback only unless you mean it)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env("ARENA_CLAUDE_PORT", str(DEFAULT_PORT)) or DEFAULT_PORT),
        help=f"port to listen on (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--engine",
        default=_env("ARENA_CLAUDE_ENGINE", "warm"),
        choices=ENGINES,
        help=(
            "warm: reuse the warm daemon (fastest); http: in-process Direct Chat; "
            "echo: reply with the prompt (wire test, no session needed)"
        ),
    )
    parser.add_argument(
        "--model",
        dest="default_model",
        default=_env("ARENA_CLAUDE_MODEL", DEFAULT_MODEL),
        help=(
            f"model reported and used when the request does not name one "
            f"(default {DEFAULT_MODEL}; arena-auto lets Arena choose)"
        ),
    )
    parser.add_argument(
        "--arena-model",
        action="append",
        default=[],
        help="extra Arena model id to advertise in GET /v1/models (repeatable)",
    )
    parser.add_argument(
        "--base-url",
        default=_env("ARENA_CLAUDE_BASE_URL", DEFAULT_BASE_URL),
        help="Arena frontend to talk to",
    )
    parser.add_argument(
        "--tools",
        default=_env("ARENA_CLAUDE_TOOLS", "emulate"),
        choices=TOOLS_MODES,
        help="emulate: render tool definitions into the prompt (Claude Code needs this)",
    )
    parser.add_argument(
        "--max-prompt-chars",
        type=int,
        default=int(_env("ARENA_CLAUDE_MAX_PROMPT_CHARS", "200000") or 200000),
        help="truncate a rendered prompt beyond this many characters",
    )
    parser.add_argument("--verbose", action="store_true", help="log every request")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``arena-api --claude``: run the shim until interrupted."""
    args = [item for item in (sys.argv[1:] if argv is None else argv) if item != "--claude"]
    parsed, _ = _serve_parser().parse_known_args(args)
    server = create_server(
        host=parsed.host,
        port=parsed.port,
        engine_name=parsed.engine,
        base_url=parsed.base_url,
        default_model=parsed.default_model,
        extra_models=parsed.arena_model,
        tools_mode=parsed.tools,
        max_prompt_chars=parsed.max_prompt_chars,
        verbose=parsed.verbose,
    )
    bound = server.server_address
    host = str(bound[0])
    port = int(bound[1])
    banner = [
        f"arena-api anthropic shim : http://{host}:{port}",
        f"engine                   : {parsed.engine}",
        f"tools                    : {parsed.tools}",
        f"default model            : {parsed.default_model}",
        "",
        "Claude Code:",
        f"  export ANTHROPIC_BASE_URL=http://{host}:{port}",
        "  export ANTHROPIC_API_KEY=arena",
        "  claude",
        "",
        "Ctrl-C to stop.",
    ]
    # Flushed now: a client (or a supervising script) reads the port from here,
    # and block buffering would otherwise hide it until the first request.
    print("\n".join(banner), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
        server.server_close()
        server.engine.close()
    return 0


__all__ = [
    "ArenaEngine",
    "BadRequest",
    "DEFAULT_MODEL",
    "DEFAULT_PORT",
    "MessagesRequest",
    "build_content_blocks",
    "create_server",
    "main",
    "parse_tool_calls",
    "render_prompt",
    "render_tool_instructions",
    "render_transcript",
    "resolve_arena_model",
    "translate_request",
]
