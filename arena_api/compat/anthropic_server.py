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

import contextlib
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path
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

#: Firefox is the default session source: it is the browser this library reads
#: best (the profile, its cookies and its TLS fingerprint), so Claude Code talks
#: to Arena as the already signed-in Firefox user instead of starting a fresh
#: anonymous profile.  ``--session auto`` restores the discovery ladder.
DEFAULT_SESSION = "firefox"

#: Claude Code starts a fresh conversation for every request and never looks at
#: Arena's sidebar, so each answered chat is archived once its transcript is
#: complete -- otherwise a few hundred chats pile up in the list.  ``--no-archive``
#: keeps them.
DEFAULT_ARCHIVE = True

_AUTO_SOURCES = frozenset({"", "auto", "any", "default"})


def _is_auto_source(session: str | None) -> bool:
    return not isinstance(session, str) or session.strip().lower() in _AUTO_SOURCES


def _note(log: Any, message: str) -> None:
    """Report something through the caller's log, or stderr as a last resort."""
    if callable(log):
        try:
            log(message)
            return
        except Exception:  # noqa: BLE001
            pass
    _log_quiet(message)


def _session_argv(
    *,
    session: str | None = None,
    profile: str | None = None,
    profile_root: str | None = None,
    archive: bool | None = None,
    fast_model: bool | None = None,
) -> list[str]:
    """Turn the session knobs into the CLI arguments the daemon parses."""
    argv: list[str] = []
    if isinstance(session, str) and session.strip() and not _is_auto_source(session):
        argv.extend(["--session", session.strip()])
    if isinstance(profile, str) and profile.strip():
        argv.extend(["--profile", profile.strip()])
    if isinstance(profile_root, str) and profile_root.strip():
        argv.extend(["--profile-root", profile_root.strip()])
    if archive:
        argv.append("--archive-after-answer")
    if fast_model is True:
        argv.append("--fast-model")
    elif fast_model is False:
        argv.append("--no-fast-model")
    return argv

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
        "To call a tool, write exactly this and nothing else inside the block:",
        '<tool_call>{"name": "<tool>", "arguments": {"<arg>": <value>}}</tool_call>',
        "Rules: call only when you need information or an action; `arguments` is a",
        "JSON object matching the schema; copy paths and commands verbatim; answer",
        "normally when no tool is needed; never invent or write tool output; after",
        "a call you receive `[tool_result ...]` -- continue from it.",
        "",
        "Available tools:",
    ]
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        description = str(tool.get("description") or "").strip()
        # Claude Code declares a dozen-plus tools; a 900-character description
        # each would spend the whole context on documentation.
        if len(description) > 300:
            description = description[:300].rstrip() + "..."
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
    if (
        '"name"' not in text
        and '"tool"' not in text
        and '"tool_name"' not in text
        and "<tool_call" not in text.lower()
    ):
        return []
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
    """Answers through the warm daemon: a browser already on a chat page.

    The daemon rebuilds its own options from the forwarded ``argv``, so the
    session source travels as a real ``--session`` argument: the daemon then
    reads the same browser profile the shim was told to use.
    """

    name = "warm"

    def __init__(
        self,
        *,
        base_url: str,
        session: str | None = None,
        profile: str | None = None,
        profile_root: str | None = None,
        archive: bool | None = None,
        fast_model: bool | None = None,
    ) -> None:
        self._base_url = base_url
        self._argv = _session_argv(
            session=session,
            profile=profile,
            profile_root=profile_root,
            archive=archive,
            fast_model=fast_model,
        )
        self._conn: Any = None
        self._cached_state: Any = None

    def _ensure_state(self, *, force_refresh: bool = False) -> Any:
        from .. import warm

        if not force_refresh and self._cached_state is not None:
            return self._cached_state
        state = warm.ensure_daemon(
            payload={"argv": list(self._argv), "base_url": self._base_url},
            base_url=self._base_url,
        )
        self._cached_state = state
        return state

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        from .. import warm

        if (
            warm.ensure_daemon is warm._DEFAULT_ENSURE_DAEMON
            and warm.ask_stream is warm._DEFAULT_ASK_STREAM
        ):
            if self._conn is None:
                self._conn = warm.WarmConnection()
            yield from self._conn.ask_stream(
                prompt,
                ensure_state=self._ensure_state,
                model=model,
            )
            return

        state = warm.ensure_daemon(
            payload={"argv": list(self._argv), "base_url": self._base_url},
            base_url=self._base_url,
        )
        yield from warm.ask_stream(prompt, state=state, model=model)

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        self._cached_state = None
        if conn is not None:
            close_fn = getattr(conn, "close", None)
            if callable(close_fn):
                close_fn()


class _HttpEngine:
    """Answers with an in-process Direct Chat client (cold start, no daemon)."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        session: str | None = None,
        profile: str | None = None,
        profile_root: str | None = None,
        archive: bool | None = None,
        fast_model: bool | None = None,
        log: Any = None,
    ) -> None:
        self._base_url = base_url
        self._session = session
        self._profile = profile
        self._profile_root = profile_root
        self._archive = bool(archive)
        self._fast_model = fast_model
        self._log = log
        self._client: Any = None
        self._lock = threading.Lock()

    def _build(self, session: str | None) -> Any:
        from ..auto import auto_direct_client

        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "verification_token": "auto",
            "prewarm": True,
            "session_source": session,
            "profile_name": self._profile,
            "profiles_root": self._profile_root,
            "archive_after_answer": self._archive,
        }
        if self._fast_model is not None:
            kwargs["fast_model"] = self._fast_model
        return auto_direct_client(**kwargs)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                self._client = self._build(self._session)
            except Exception as exc:  # noqa: BLE001 - only a forced source may fall back
                if not self._session or _is_auto_source(self._session):
                    raise
                # The caller asked for one source; if it has nothing, say so and
                # use whatever session the machine does have instead of dying.
                _note(
                    self._log,
                    f"{self._session} has no usable session ({exc}); "
                    "falling back to automatic discovery",
                )
                self._client = self._build(None)
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


def _looks_like_unknown_model(exc: BaseException) -> bool:
    """Whether an error really means "there is no such model"."""
    text = str(exc).lower()
    if "model" not in text:
        return False
    markers = (
        "not found",
        "unknown",
        "unsupported",
        "invalid",
        "no such",
        "does not exist",
        "could not find",
        "not available",
    )
    return any(marker in text for marker in markers)


class ArenaEngine:
    """Serialises prompts and picks the backend, with a graceful fallback."""

    def __init__(
        self,
        *,
        engine: str = "warm",
        base_url: str = DEFAULT_BASE_URL,
        tools_mode: str = "emulate",
        prompt: str = "",
        session: str | None = DEFAULT_SESSION,
        profile: str | None = None,
        profile_root: str | None = None,
        archive: bool | None = DEFAULT_ARCHIVE,
        fast_model: bool | None = None,
        log: Any = None,
    ) -> None:
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}")
        if tools_mode not in TOOLS_MODES:
            raise ValueError(f"tools_mode must be one of {TOOLS_MODES}")
        self.engine = engine
        self.base_url = base_url
        self.tools_mode = tools_mode
        self.session = session
        self.profile = profile
        self.profile_root = profile_root
        self.archive = bool(archive)
        self.fast_model = fast_model
        self.model_hint = prompt
        self.log = log
        self.verbose = False
        #: Timings of the last answer, for the ``--verbose`` log and the doctor.
        self.last_timings: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._unknown_models: set[str] = set()
        self._primary: Any
        self._fallback: Any = None
        if engine == "echo":
            self._primary = _EchoEngine()
        elif engine == "http":
            self._primary = _HttpEngine(
                base_url=base_url,
                session=session,
                profile=profile,
                profile_root=profile_root,
                archive=archive,
                fast_model=fast_model,
                log=self._note,
            )
        else:
            self._primary = _WarmEngine(
                base_url=base_url,
                session=session,
                profile=profile,
                profile_root=profile_root,
                archive=archive,
                fast_model=fast_model,
            )
            # A cold or unavailable daemon must not turn into a 500: the same
            # direct client the CLI falls back to answers instead.
            self._fallback = _HttpEngine(
                base_url=base_url,
                session=session,
                profile=profile,
                profile_root=profile_root,
                archive=archive,
                fast_model=fast_model,
                log=self._note,
            )

    def _note(self, message: str) -> None:
        log = self.log
        if callable(log):
            try:
                log(message)
                return
            except Exception:  # noqa: BLE001
                pass
        print(f"[arena-api] {message}", file=sys.stderr, flush=True)

    def _open(
        self, prompt: str, model: str | None
    ) -> tuple[Any, str]:
        """Open the best available stream and return it with its first delta.

        Two failures are worth a retry rather than an error page:

        * the client named a model Arena does not have (Claude Code sends its
          own ``claude-sonnet-...`` ids) -- retry with Arena's default model,
          because refusing the whole turn for a name mismatch is useless;
        * the primary engine (the warm daemon) is not there -- fall back to the
          in-process client, which is exactly what the CLI does.
        """
        last_error: BaseException | None = None
        initial_model = (
            None
            if model is not None and model in self._unknown_models
            else model
        )
        for engine in (self._primary, self._fallback):
            if engine is None:
                continue
            active_model = initial_model
            for attempt in (0, 1):
                stream = engine.answer(prompt, model=active_model)
                try:
                    return stream, next(stream)
                except StopIteration:
                    self._close_stream(stream)
                    return engine.answer(prompt, model=active_model), ""
                except Exception as exc:  # noqa: BLE001 - decided below
                    last_error = exc
                    self._close_stream(stream)
                    if (
                        attempt == 0
                        and active_model is not None
                        and _looks_like_unknown_model(exc)
                    ):
                        self._unknown_models.add(active_model)
                        self._note(
                            f"Arena не знает модель {active_model!r} ({exc}); "
                            "отвечаю моделью Arena по умолчанию"
                        )
                        active_model = None
                        initial_model = None
                        continue
                    self._note(f"{engine.name} engine failed ({exc}); falling back")
                    break
        raise EngineError(str(last_error) if last_error else "no engine available")

    @staticmethod
    def _close_stream(stream: Any) -> None:
        close_fn = getattr(stream, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # noqa: BLE001
                pass

    def answer(self, prompt: str, *, model: str | None = None) -> Iterator[str]:
        with self._lock:
            started = time.monotonic()
            stream, first = self._open(prompt, model)
            opened = time.monotonic() - started
            chars = len(first)
            try:
                if first:
                    yield first
                for delta in stream:
                    chars += len(delta)
                    yield delta
            finally:
                self._close_stream(stream)
                self.last_timings = {
                    "prompt_chars": len(prompt),
                    "open_ms": round(opened * 1000.0, 1),
                    "first_delta_ms": round(opened * 1000.0, 1),
                    "total_ms": round((time.monotonic() - started) * 1000.0, 1),
                    "answer_chars": chars,
                }
                if self.verbose:
                    _log_quiet(
                        "prompt {prompt_chars} chars -> open {open_ms} ms, "
                        "answer {answer_chars} chars, total {total_ms} ms".format(
                            **self.last_timings
                        )
                    )

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
            "session": getattr(self.engine, "session", None),
            "archive": getattr(self.engine, "archive", None),
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
        if self.server.verbose:  # type: ignore[attr-defined]
            _log_quiet(
                f"request: {len(request.prompt)} chars, "
                f"{len(request.tools)} tools, stream={request.stream}, "
                f"model={request.model}"
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
            timings = getattr(self.engine, "last_timings", {}) or {}
            _log_quiet(
                f"answered in {time.monotonic() - started:.2f}s "
                f"({len(text)} chars, stop={stop_reason}, "
                f"engine first delta {timings.get('first_delta_ms', '?')} ms)"
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
            max_stop_len = max(
                (len(s) for s in request.stop_sequences if s), default=0
            )
            try:
                for delta in self.engine.answer(
                    request.prompt, model=request.arena_model
                ):
                    if not delta:
                        continue
                    buffer += delta
                    if allow_tools:
                        # Inspect only the unstreamed tail: an ambiguous `````
                        # prefix halts briefly and un-halts as soon as a
                        # non-JSON language tag (e.g. ```python) arrives.
                        halted = _may_be_tool_call(buffer[streamed:])
                    limit = len(buffer)
                    if halted:
                        limit = streamed
                    else:
                        holdback = min(
                            self.HOLDBACK,
                            _tail_holdback_len(
                                buffer[streamed:],
                                allow_tools=allow_tools,
                                stop_sequences=request.stop_sequences,
                            ),
                        )
                        limit -= holdback
                    search_start = (
                        max(0, streamed - max_stop_len) if max_stop_len else 0
                    )
                    stop_at, matched = self._find_stop(
                        buffer, request.stop_sequences, start=search_start
                    )
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
    def _find_stop(
        text: str, stops: Sequence[str], *, start: int = 0
    ) -> tuple[int | None, str | None]:
        """Earliest stop sequence in ``text`` at or after ``start``."""
        if not stops:
            return None, None
        best: tuple[int, str] | None = None
        origin = max(0, start)
        for stop in stops:
            if not stop:
                continue
            index = text.find(stop, origin)
            if index >= 0 and (best is None or index < best[0]):
                best = (index, stop)
        if best is None:
            return None, None
        return best[0], best[1]


def _tail_holdback_len(
    text: str,
    *,
    allow_tools: bool,
    stop_sequences: Sequence[str],
) -> int:
    """Exact number of trailing characters that could begin a tool marker or stop sequence."""
    if not text or (not allow_tools and not stop_sequences):
        return 0
    max_len = 10 if allow_tools else 0
    for stop in stop_sequences:
        if len(stop) - 1 > max_len:
            max_len = len(stop) - 1
    max_k = min(len(text), max_len)
    if max_k <= 0:
        return 0
    for k in range(max_k, 0, -1):
        suffix = text[-k:]
        if allow_tools:
            lowered_suffix = suffix.lower()
            if "<tool_call>".startswith(lowered_suffix) or "```".startswith(
                suffix
            ):
                return k
        for stop in stop_sequences:
            if len(stop) > k and stop.startswith(suffix):
                return k
    return 0


def _may_be_tool_call(text: str) -> bool:
    """True when ``text`` can still turn into a tool call.

    Only the two shapes :func:`parse_tool_calls` trusts while streaming are
    held back: ``<tool_call>`` tags and JSON markdown fences whose body starts
    with ``{`` (or whose fence header is still being streamed, e.g. `````json``).
    Ordinary code fences such as `````python`` return ``False`` immediately so
    long code answers stream live instead of buffering until completion.
    """
    lowered = text.lower()
    if "<tool" in lowered:
        return True
    pos = 0
    while True:
        fence = lowered.find("```", pos)
        if fence < 0:
            return False
        after = text[fence + 3 :]
        close_rel = after.find("```")
        block = after if close_rel < 0 else after[:close_rel]
        # Split optional fence info string (language tag) from body.
        nl = block.find("\n")
        ws_split = nl
        for idx, ch in enumerate(block):
            if ch in " \t\r\n{":
                ws_split = idx
                break
        if ws_split < 0:
            # Header still forming (e.g. "```" or "```js" or "```json").
            lang_prefix = block.lower()
            if "json".startswith(lang_prefix):
                return True
        else:
            lang = block[:ws_split].strip().lower()
            body = block[ws_split:].lstrip()
            if lang in ("", "json"):
                if not body:
                    if close_rel < 0:
                        return True
                elif body.startswith("{"):
                    if close_rel < 0:
                        return True
                    if (
                        '"name"' in body
                        or '"tool"' in body
                        or '"tool_name"' in body
                    ):
                        return True
        if close_rel < 0:
            return False
        pos = fence + 3 + close_rel + 3


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

    def get_request(self) -> tuple[Any, Any]:
        conn, addr = super().get_request()
        with contextlib.suppress(OSError, AttributeError):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn, addr


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
    session: str | None = DEFAULT_SESSION,
    profile: str | None = None,
    profile_root: str | None = None,
    archive: bool | None = DEFAULT_ARCHIVE,
    fast_model: bool | None = None,
    verbose: bool = False,
    log: Any = None,
) -> _Server:
    """Build the HTTP server; ``engine`` overrides everything for tests."""
    resolved = engine if engine is not None else ArenaEngine(
        engine=engine_name,
        base_url=base_url,
        tools_mode=tools_mode,
        session=session,
        profile=profile,
        profile_root=profile_root,
        archive=archive,
        fast_model=fast_model,
        log=log,
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
        "--session",
        default=_env("ARENA_CLAUDE_SESSION", DEFAULT_SESSION) or DEFAULT_SESSION,
        metavar="SOURCE",
        help=(
            f"which browser session to use (default {DEFAULT_SESSION}): firefox, "
            "firefox:<profile>, storage, chrome/edge/..., or auto for the "
            "discovery ladder. Firefox is the profile this library reads best, "
            "and its cookies/TLS fingerprint are what the requests carry"
        ),
    )
    parser.add_argument(
        "--archive",
        dest="archive",
        action="store_true",
        default=_env("ARENA_CLAUDE_ARCHIVE", "1") not in {"0", "false", "no", "off"},
        help=(
            "archive each conversation once its answer is fully received "
            "(default, keeps Arena's sidebar clean; --no-archive keeps the chats)"
        ),
    )
    parser.add_argument(
        "--no-archive",
        dest="archive",
        action="store_false",
        help="keep the conversations in Arena's sidebar",
    )
    parser.add_argument(
        "--fast-model",
        dest="fast_model",
        action="store_true",
        default=None,
        help="prefer low-latency Direct Chat models when auto-selecting (default)",
    )
    parser.add_argument(
        "--no-fast-model",
        dest="fast_model",
        action="store_false",
        help="use Arena's heaviest max model instead of low-latency auto-selection",
    )
    parser.add_argument(
        "--profile",
        default=_env("ARENA_CLAUDE_PROFILE") or None,
        help="Firefox profile name to read (default: the most recent one)",
    )
    parser.add_argument(
        "--profile-root",
        default=_env("ARENA_CLAUDE_PROFILE_ROOT") or None,
        metavar="DIR",
        help="directory holding the Firefox profiles",
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
    parser.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "check the four links (installed package, session, engine, loopback "
            "protocol) and name the first one that breaks"
        ),
    )
    return parser



# --------------------------------------------------------------------------- #
# Doctor: tell the user *which* of the four links is broken
# --------------------------------------------------------------------------- #


class _Report:
    """Collects the doctor's verdicts and prints them as it goes."""

    def __init__(self, out: Any = None) -> None:
        self.lines: list[str] = []
        self.failures = 0
        self._out = out or sys.stdout

    def step(self, title: str) -> None:
        self._write("")
        self._write(title)

    def ok(self, message: str) -> None:
        self._write(f"  ok    {message}")

    def fail(self, message: str, hint: str | None = None) -> None:
        self.failures += 1
        self._write(f"  ОШИБКА {message}")
        if hint:
            for line in hint.splitlines():
                self._write(f"         {line}")

    def note(self, message: str) -> None:
        self._write(f"        {message}")

    def _write(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)


def _answer_once(
    engine: Any, prompt: str, *, limit: int = 400
) -> tuple[str, float, float]:
    """Run one prompt through the engine; return text, first-delta and total time."""
    started = time.monotonic()
    first = 0.0
    parts: list[str] = []
    size = 0
    for delta in engine.answer(prompt):
        if not first:
            first = time.monotonic() - started
        parts.append(delta)
        size += len(delta)
        if size >= limit:
            break
    return "".join(parts), first or (time.monotonic() - started), (
        time.monotonic() - started
    )


def _post_loopback(
    url: str, payload: Mapping[str, Any], *, stream: bool, timeout: float = 120.0
) -> tuple[int, list[tuple[str, Any]]]:
    """Speak the Anthropic protocol to our own port, exactly like a client."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION},
        method="POST",
    )
    if not stream:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, [("http_error", exc.read().decode("utf-8", "replace"))]
        return 200, [("message", data)]
    events: list[tuple[str, Any]] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            name = ""
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    name = line[len("event: ") :]
                elif line.startswith("data: "):
                    try:
                        events.append((name, json.loads(line[len("data: ") :])))
                    except ValueError:
                        events.append((name, line[len("data: ") :]))
                # No early break: `message_stop` is announced by its event
                # line, and its data line is the very thing worth reading.
    except urllib.error.HTTPError as exc:
        return exc.code, [("http_error", exc.read().decode("utf-8", "replace"))]
    return 200, events


def doctor(argv: Sequence[str] | None = None) -> int:
    """``arena-api --claude --doctor``: find out what is actually broken.

    The shim has four links -- the installed package, the session, the engine
    that talks to Arena, and the HTTP/SSE protocol on loopback -- and a failure
    in any of them looks like "Claude Code says something went wrong".  This
    walks them in order and names the first one that breaks, with the fix.
    """
    args = [item for item in (sys.argv[1:] if argv is None else argv) if item != "--claude"]
    parsed, _ = _serve_parser().parse_known_args(args)
    report = _Report()
    report.step("1. Установка")
    from importlib.util import find_spec

    import arena_api

    from .._cli import _package_version

    report.note(f"arena-api {_package_version()}")
    module_dir = str(Path(str(arena_api.__file__)).parent)
    report.note(f"пакет    : {module_dir}")
    report.note(f"python   : {sys.executable}")
    if find_spec("arena_api.compat.anthropic_server") is None:  # pragma: no cover
        report.fail(
            "шим отсутствует в этой сборке",
            "обновите пакет: pip install -U -e \".[auto]\" (из клона репозитория)",
        )
    else:
        report.ok("шим на месте (arena_api.compat.anthropic_server)")

    try:
        from ..auto import AutoOptions, build_arg_parser

        fields = set(getattr(AutoOptions, "__dataclass_fields__", {}))
        has_session = "session_source" in fields and "session" in (
            build_arg_parser().parse_args([]).__dict__
        )
    except Exception as exc:  # noqa: BLE001 - an old package raises here
        has_session = False
        report.fail(f"arena_api.auto не импортируется: {exc}")
    if has_session:
        report.ok("--session / session_source поддержаны")
    else:
        report.fail(
            "эта сборка не знает --session",
            "обновите пакет: pip install -U -e \".[auto]\" — иначе сессия "
            "Firefox не может быть выбрана",
        )

    session = parsed.session
    report.step(f"2. Сессия (источник: {session}"
                + (f", профиль {parsed.profile}" if parsed.profile else "") + ")")
    if not has_session:
        report.fail("шаг пропущен: пакет не умеет --session")
    else:
        from ..auto import explain_sources, find_session_for_doctor

        try:
            found, active = find_session_for_doctor(
                session=session,
                profile=parsed.profile,
                profile_root=parsed.profile_root,
                base_url=parsed.base_url,
            )
        except Exception as exc:  # noqa: BLE001 - this is what the doctor is for
            found, active, failure = None, None, f"{type(exc).__name__}: {exc}"
        else:
            failure = ""
        if found is None:
            lines = [failure] if failure else ["куки не найдены"]
            if active is not None:
                lines.extend(explain_sources(active))
            report.fail("сессия не найдена", "\n".join(lines))
        else:
            report.ok(found.description)
            names = ", ".join(sorted(found.cookies))[:160]
            report.note(f"куки: {names}")

    engine_name = parsed.engine
    report.step(f"3. Движок ответа ({engine_name})")
    engine: Any = None
    try:
        engine = ArenaEngine(
            engine=engine_name,
            base_url=parsed.base_url,
            tools_mode=parsed.tools,
            session=session,
            profile=parsed.profile,
            profile_root=parsed.profile_root,
            archive=parsed.archive,
            log=report.note,
        )
        text, first, total = _answer_once(
            engine, "Ответь одним словом: работает", limit=200
        )
        if text.strip():
            preview = text.strip().replace("\n", " ")[:120]
            report.ok(f'ответ за {total:.1f} с (первый фрагмент {first:.1f} с): "{preview}"')
        else:
            report.fail("движок вернул пустой ответ")
    except Exception as exc:  # noqa: BLE001 - this is the point of the doctor
        report.fail(f"{type(exc).__name__}: {exc}", _engine_hint(exc))

    report.step("4. Протокол на loopback (как это видит Claude Code)")
    if engine is None:
        report.fail("шаг пропущен: движок не создан")
    else:
        server = create_server(
            host="127.0.0.1",
            port=0,
            engine=engine,
            verbose=False,
            default_model=parsed.default_model,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = _address_url(server.server_address)
        try:
            request = {
                "model": parsed.default_model,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "скажи ок"}],
            }
            status, payload_events = _post_loopback(
                f"{base}/v1/messages", request, stream=False
            )
            if status == 200 and payload_events:
                blocks = payload_events[0][1].get("content") or []
                text = "".join(str(block.get("text") or "") for block in blocks)
                report.ok(f"JSON-ответ: {len(text)} символов, stop_reason="
                          f"{payload_events[0][1].get('stop_reason')}")
            else:
                report.fail(f"JSON-ответ: HTTP {status}: {payload_events[:1]}")
            status, events = _post_loopback(
                f"{base}/v1/messages", {**request, "stream": True}, stream=True
            )
            event_names = [name for name, _ in events]
            if status == 200 and "message_stop" in event_names:
                reply = "".join(
                    data["delta"].get("text", "")
                    for name, data in events
                    if name == "content_block_delta" and isinstance(data, dict)
                )
                report.ok(
                    f"SSE: {len(event_names)} событий, {len(reply)} символов, "
                    f"события: {', '.join(event_names[:6])}..."
                )
            else:
                report.fail(f"SSE: HTTP {status}, события: {event_names[:8]}")
            with_tools = {
                **request,
                "tools": [
                    {
                        "name": "Bash",
                        "description": "run a command",
                        "input_schema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
            }
            status, events = _post_loopback(
                f"{base}/v1/messages", {**with_tools, "stream": True}, stream=True
            )
            event_names = [name for name, _ in events]
            kinds = [
                data["content_block"]["type"]
                for name, data in events
                if name == "content_block_start" and isinstance(data, dict)
            ]
            if status == 200 and "message_stop" in event_names:
                report.ok(f"SSE с инструментами: блоки {kinds or ['—']}")
            else:
                report.fail(
                    f"SSE с инструментами: HTTP {status}, "
                    f"события: {event_names[:8]}"
                )
        finally:
            server.shutdown()
            server.server_close()
            engine.close()

    report.step("5. Профиль браузера")
    try:
        from .._direct_page import _direct_browser_profile_dir
        from ..auth.browser import (
            heal_profile_locks,
            profile_is_locked,
            profile_launch_hint,
        )
        from ..auto import AutoOptions as _Options

        profile_dir = _direct_browser_profile_dir(_Options(base_url=parsed.base_url))
        if not profile_dir.exists():
            report.ok(f"{profile_dir} ещё не создан (первый запуск создаст его)")
        else:
            healed = heal_profile_locks(profile_dir)
            if healed:
                report.note(f"сняты устаревшие блокировки: {', '.join(healed)}")
            if profile_is_locked(profile_dir):
                report.fail(
                    f"профиль занят живым процессом: {profile_dir}",
                    "закройте все окна Chrome/Edge (или завершите их в "
                    "диспетчере задач) и повторите; как временный обход — "
                    "arena-api --fresh-profile",
                )
            else:
                size = sum(
                    1 for _ in profile_dir.rglob("*") if _.is_file()
                )
                report.ok(f"{profile_dir} ({size} файлов, свободен)")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        report.fail(f"профиль браузера не проверен: {type(exc).__name__}: {exc}",
                    profile_launch_hint("{профиль}"))

    report.step("ИТОГ")
    if report.failures == 0:
        report.note("Шим отвечает. Запускайте:")
        report.note(f"  arena-api --claude --port {DEFAULT_PORT}")
        report.note(f'  ANTHROPIC_BASE_URL=http://127.0.0.1:{DEFAULT_PORT}')
        report.note("  ANTHROPIC_API_KEY=arena")
        return 0
    report.note(f"Найдено проблем: {report.failures}. Начните с первой — остальные")
    report.note("обычно её следствие.")
    return 1


def _engine_hint(exc: BaseException) -> str:
    """Turn an engine failure into something the user can act on."""
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, TypeError) and "unexpected keyword" in lowered:
        return (
            "похоже, установлена старая сборка arena-api: обновите её "
            "(pip install -U -e \".[auto]\")"
        )
    if "session" in lowered or "cookies" in lowered or "profile" in lowered:
        return "проверьте, что в Firefox открыт arena.ai и вы вошли в аккаунт"
    if "playwright" in lowered or "browser" in lowered:
        return "нужен браузер Playwright: playwright install chromium"
    if "captcha" in lowered or "challenge" in lowered or "403" in lowered:
        return (
            "Arena показала капчу/челлендж: поставьте солвер "
            "(pip install 'arena-api[captcha]') или обновите сессию через "
            "arena-api --login"
        )
    if "401" in lowered or "403" in lowered or "unauthor" in lowered:
        return "сессия отклонена: обновите её (arena-api --login) или --session auto"
    if "timeout" in lowered or "timed out" in lowered:
        return "Arena не ответила вовремя: проверьте прокси/сеть (--proxy, --no-verify)"
    return (
        "подробности выше; для полного лога: set ARENA_WARM_DEBUG=1 и "
        "arena-api --claude --verbose"
    )


def startup_session_note(
    *,
    session: str | None,
    profile: str | None = None,
    profile_root: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[bool, str]:
    """Check the session once, cheaply, before the first request arrives.

    Returns ``(found, text)``.  Reading a browser profile is a file read, so
    this costs milliseconds and turns a confusing failure inside Claude Code
    into a line in the banner: "the session is not there, run --doctor".
    """
    try:
        from ..auto import find_session_for_doctor

        found, _options = find_session_for_doctor(
            session=session,
            profile=profile,
            profile_root=profile_root,
            base_url=base_url,
        )
    except Exception as exc:  # noqa: BLE001 - a banner must never crash
        return False, f"{type(exc).__name__}: {exc}"
    if found is None:
        return False, (
            f"НЕ НАЙДЕНА (источник {session or 'auto'}) — Claude Code получит "
            "ошибку; проверьте: arena-api --claude --doctor"
        )
    return True, found.description


def main(argv: Sequence[str] | None = None) -> int:
    """``arena-api --claude``: run the shim until interrupted."""
    args = [item for item in (sys.argv[1:] if argv is None else argv) if item != "--claude"]
    if "--doctor" in args:
        return doctor(args)
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
        session=parsed.session,
        profile=parsed.profile,
        profile_root=parsed.profile_root,
        archive=parsed.archive,
        fast_model=parsed.fast_model,
        verbose=parsed.verbose,
    )
    bound = server.server_address
    host = str(bound[0])
    port = int(bound[1])
    session_line = parsed.session + (
        f" (profile {parsed.profile})" if parsed.profile else ""
    )
    if parsed.engine != "echo":
        # One cheap file read now beats a cryptic error later: a missing
        # Firefox profile is the most common reason the first prompt fails.
        _ok, note = startup_session_note(
            session=parsed.session,
            profile=parsed.profile,
            profile_root=parsed.profile_root,
            base_url=parsed.base_url,
        )
        session_line += f" -> {note}"
    banner = [
        f"arena-api anthropic shim : http://{host}:{port}",
        f"engine                   : {parsed.engine}",
        f"tools                    : {parsed.tools}",
        f"session                  : {session_line}",
        f"archive after answer     : {'yes' if parsed.archive else 'no'}",
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
    "doctor",
    "main",
    "parse_tool_calls",
    "render_prompt",
    "render_tool_instructions",
    "render_transcript",
    "resolve_arena_model",
    "startup_session_note",
    "translate_request",
]
