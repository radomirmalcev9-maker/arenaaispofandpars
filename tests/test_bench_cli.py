"""Tests for ``tools/bench_cli.py`` (the cold-vs-warm benchmark script)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "bench_cli.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("bench_cli", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load()


def _profile(phases: list[tuple[str, float]]) -> str:
    payload = {
        "total_ms": phases[-1][1] if phases else 0.0,
        "phases": [
            {"phase": name, "at_ms": at, "delta_ms": 0.0} for name, at in phases
        ],
    }
    return f"[arena-api latency] {json.dumps(payload)}"


def test_parse_first_token_uses_the_first_answer_phase() -> None:
    stderr = "\n".join(
        [
            "[arena-api] session: Firefox profile ...",
            _profile([("cli.start", 5.0), ("first_byte", 2200.0), ("cli.exit", 9000.0)]),
        ]
    )
    assert bench.parse_first_token(stderr) == pytest.approx(2.2)


def test_parse_first_token_prefers_the_daemon_phase() -> None:
    stderr = _profile([("first_byte", 900.0), ("first_delta", 1500.0)])
    assert bench.parse_first_token(stderr) == pytest.approx(1.5)


def test_parse_first_token_returns_none_without_a_report() -> None:
    assert bench.parse_first_token("[arena-api] nothing here") is None
    assert bench.parse_first_token(_profile([])) is None
    assert bench.parse_first_token("[arena-api latency] {broken json") is None


def test_parse_retry_after_reads_the_server_hint() -> None:
    message = "[arena-api] Arena rate limit reached. Retry in about 42s."
    assert bench.parse_retry_after(message) == pytest.approx(42.0)
    assert bench.parse_retry_after("no hint") is None


def test_run_result_classifies_rate_limits() -> None:
    limited = bench.RunResult(1.0, 1, "[arena-api] Arena rate limit reached.")
    other = bench.RunResult(1.0, 1, "[arena-api] The supplied session was rejected")
    assert limited.ok is False
    assert limited.rate_limited is True
    assert other.rate_limited is False
    assert "Arena rate limit" in limited.reason


def test_run_with_retries_waits_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        bench.RunResult(1.0, 1, "[arena-api] Arena rate limit reached. Retry in about 7s."),
        bench.RunResult(2.0, 0, _profile([("first_delta", 500.0)])),
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(bench, "run_once", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(bench.time, "sleep", sleeps.append)

    result = bench.run_with_retries(
        "python", ["--prompt", "hi"], retries=2, pause=20.0, out=bench.Reporter()
    )
    assert result.ok is True
    assert sleeps == [pytest.approx(7.0)]


def test_run_with_retries_backs_off_without_a_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        bench.RunResult(1.0, 1, "HTTP 429"),
        bench.RunResult(1.0, 1, "HTTP 429"),
        bench.RunResult(1.0, 1, "HTTP 429"),
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(bench, "run_once", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(bench.time, "sleep", sleeps.append)

    result = bench.run_with_retries(
        "python", [], retries=2, pause=10.0, out=bench.Reporter()
    )
    assert result.ok is False
    assert sleeps == [pytest.approx(20.0), pytest.approx(40.0)]


def test_run_with_retries_does_not_retry_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def _once(*_: Any, **__: Any) -> Any:
        calls.append(1)
        return bench.RunResult(1.0, 1, "[arena-api] session was rejected")

    monkeypatch.setattr(bench, "run_once", _once)
    monkeypatch.setattr(bench.time, "sleep", sleeps.append)
    result = bench.run_with_retries(
        "python", [], retries=2, pause=10.0, out=bench.Reporter()
    )
    assert result.ok is False
    assert len(calls) == 1
    assert sleeps == []


def test_main_keeps_going_when_cold_runs_are_rate_limited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user's case: cold runs hit the limit, warm runs still get measured."""

    def _once(_python: str, argv: Any) -> Any:
        argv = list(argv)
        if "--warm-stop" in argv:
            return bench.RunResult(0.1, 0, "")
        if "--no-warm" in argv:
            return bench.RunResult(
                8.8, 1, "[arena-api] Arena rate limit reached. Retry in about 1s."
            )
        return bench.RunResult(3.0, 0, _profile([("first_delta", 1200.0)]))

    monkeypatch.setattr(bench, "run_once", _once)
    monkeypatch.setattr(bench.time, "sleep", lambda _s: None)
    code = bench.main(["--prompt", "hi", "--runs", "1", "--pause", "0", "--retries", "0"])
    output = capsys.readouterr()
    assert code == 0
    assert "warm (daemon reused)" in output.out
    assert "first warm (daemon start)" in output.out
    assert "no warm run succeeded" not in output.err


def test_main_stops_when_the_cold_path_is_broken(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        bench,
        "run_once",
        lambda *a, **k: bench.RunResult(1.0, 1, "[arena-api] no session found for arena.ai"),
    )
    monkeypatch.setattr(bench.time, "sleep", lambda _s: None)
    code = bench.main(["--prompt", "hi", "--runs", "1", "--pause", "0"])
    assert code == 2
    assert "cold path cannot run" in capsys.readouterr().err


def test_main_reports_when_the_first_warm_run_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _once(_python: str, argv: Any) -> Any:
        argv = list(argv)
        if "--warm-stop" in argv:
            return bench.RunResult(0.1, 0, "")
        if "--no-warm" in argv:
            return bench.RunResult(8.0, 0, _profile([("first_byte", 4000.0)]))
        return bench.RunResult(6.0, 1, "[arena-api] Arena rate limit reached.")

    monkeypatch.setattr(bench, "run_once", _once)
    monkeypatch.setattr(bench.time, "sleep", lambda _s: None)
    code = bench.main(
        ["--prompt", "hi", "--runs", "1", "--pause", "0", "--retries", "0"]
    )
    assert code == 3
    assert "rate-limiting this account" in capsys.readouterr().err


def test_main_rejects_nonsense_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        bench.main(["--runs", "0"])
    with pytest.raises(SystemExit):
        bench.main(["--pause", "-1"])
