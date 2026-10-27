"""Opt-in latency profiling for the CLI, the warm daemon and the clients.

The Direct Chat path is dominated by a handful of phases that are easy to
measure but hard to guess: reading the browser session, launching the browser,
loading the page, minting a reCAPTCHA v3 assertion, submitting the prompt and
waiting for the first streamed byte.  When a run feels slow, set
``ARENA_PROFILE=1`` and the process prints that breakdown to stderr before it
exits::

    $ ARENA_PROFILE=1 arena-api --prompt "Привет"
    [...answer...]
    [arena-api latency] phase                        delta ms   total ms
    [arena-api latency] cli.start                       0.0        0.0
    [arena-api latency] session.discover              142.7      142.7
    [arena-api latency] browser.launch.wait          1211.0     1353.7
    ...

``ARENA_PROFILE=json`` emits a single machine-readable JSON object instead (one
line, easy to collect in a loop).  Profiling is off by default and costs one
boolean check per call site when disabled.

The module deliberately imports nothing but the standard library: it is used by
the lightweight CLI front-end, which must stay fast to import.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any, TextIO

_LOCK = threading.Lock()
_MARKS: list[tuple[str, float]] = []
_CREATED = time.perf_counter()

_TRUE = {"1", "true", "yes", "on", "y"}
_JSON = {"json", "json-lines", "jsonl"}


def _mode_from_env(environ: MutableMapping[str, str]) -> str:
    raw = str(environ.get("ARENA_PROFILE", "")).strip().lower()
    if raw in _JSON:
        return "json"
    if raw in _TRUE:
        return "1"
    return ""


_MODE = _mode_from_env(os.environ)


def enabled() -> bool:
    """``True`` when ``ARENA_PROFILE`` asked for a timing report."""
    return bool(_MODE)


def mode() -> str:
    """``""`` (off), ``"1"`` (table) or ``"json"``."""
    return _MODE


def reset() -> None:
    """Forget every mark (tests and long-lived daemons)."""
    with _LOCK:
        _MARKS.clear()
    global _CREATED
    _CREATED = time.perf_counter()


def mark(name: str) -> None:
    """Record that ``name`` has just been reached."""
    if not _MODE:
        return
    now = time.perf_counter()
    with _LOCK:
        _MARKS.append((str(name), now))


@contextmanager
def span(name: str) -> Iterator[None]:
    """Mark ``name`` on entry and ``name.end`` (or ``name.error``) on exit."""
    if not _MODE:
        yield
        return
    mark(name)
    try:
        yield
    except BaseException:
        mark(f"{name}.error")
        raise
    else:
        mark(f"{name}.end")


def _snapshot() -> list[tuple[str, float]]:
    with _LOCK:
        return list(_MARKS)


def _rows() -> list[dict[str, Any]]:
    marks = _snapshot()
    if not marks:
        return []
    # Everything is reported from the moment the module was imported, which is
    # the closest thing to "when the CLI started" and makes the first phase
    # (argument parsing, session discovery) readable instead of zero.
    origin = _CREATED
    rows: list[dict[str, Any]] = []
    previous = origin
    for name, moment in marks:
        rows.append(
            {
                "phase": name,
                "at_ms": round((moment - origin) * 1000.0, 1),
                "delta_ms": round((moment - previous) * 1000.0, 1),
            }
        )
        previous = moment
    return rows


def report(prefix: str = "[arena-api latency]") -> str:
    """Human-readable table of the recorded phases (empty when nothing ran)."""
    rows = _rows()
    if not rows:
        return ""
    width = max(len("phase"), *(len(str(row["phase"])) for row in rows))
    lines = [
        f"{prefix} {'phase':<{width}} {'delta ms':>10} {'total ms':>10}",
    ]
    for row in rows:
        lines.append(
            f"{prefix} {row['phase']:<{width}} "
            f"{row['delta_ms']:>10.1f} {row['at_ms']:>10.1f}"
        )
    lines.append(
        f"{prefix} {'total':<{width}} {'':>10} {rows[-1]['at_ms']:>10.1f}"
    )
    return "\n".join(lines)


def as_json(prefix: str = "[arena-api latency]", pretty: bool = False) -> str:
    """One-line (or pretty) JSON report, including the process start offset."""
    rows = _rows()
    payload = {
        "total_ms": rows[-1]["at_ms"] if rows else 0.0,
        "phases": [{"phase": row["phase"], "at_ms": row["at_ms"], "delta_ms": row["delta_ms"]} for row in rows],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    return f"{prefix} {body}" if prefix else body


def dump(
    prefix: str = "[arena-api latency]",
    *,
    stream: TextIO | None = None,
    force: bool = False,
) -> None:
    """Write the report to stderr (only when profiling is enabled)."""
    if not _MODE and not force:
        return
    text = as_json(prefix) if _MODE == "json" else report(prefix)
    if not text:
        return
    target = stream if stream is not None else sys.stderr
    try:
        target.write(text + "\n")
        target.flush()
    except Exception:  # noqa: BLE001 - a report must never break a run
        pass


def elapsed() -> float:
    """Seconds since the module was imported (always measured)."""
    return time.perf_counter() - _CREATED


# One report per process, right before it exits: callers stay free of
# bookkeeping and a fallback from the light CLI front-end into
# ``arena_api.auto`` cannot print the timeline twice.
if _MODE:
    atexit.register(dump)
