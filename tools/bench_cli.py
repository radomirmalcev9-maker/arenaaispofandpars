#!/usr/bin/env python3
"""Measure the warm-daemon speed-up with the CLI you actually use.

The daemon only removes *overhead*: session discovery, browser launch, page
load, composer warm-up.  How much wall-clock time that is depends on your
machine and profile, so the honest way to claim a factor is to measure it:

    python tools/bench_cli.py --prompt "Ответь одним словом: ок"

The script runs the same prompt with a cold start (``--no-warm``, daemon
stopped) and through the warm daemon, and prints medians plus the resulting
speed-up.  Two numbers are reported:

* **wall** — the whole command, generation included.  This is what you feel,
  but a long answer dilutes the comparison, so keep the prompt short;
* **first token** — the time until the first streamed delta, read from
  ``ARENA_PROFILE=json``.  This is the number the daemon actually improves.

Arena rate-limits repeated evaluations, so every prompt is separated by
``--pause`` seconds (default 20) and a rate-limited run is retried after the
wait the server asked for.  Failures never abort the whole benchmark: the
script keeps going, reports what it managed to measure, and only gives up when
a variant cannot produce a single successful run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence

#: Phase names that mean "the first piece of the answer reached us", in the
#: order they should be preferred when reading a profile report.
FIRST_TOKEN_PHASES = ("first_delta", "first_byte", "direct.wire", "answer.start")

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "too many requests",
    "429",
)
_RETRY_AFTER_RE = re.compile(r"(?:retry|повтор\w*)\D{0,24}([0-9]+(?:\.[0-9]+)?)\s*s", re.I)
_PROFILE_PREFIX = "[arena-api latency]"


class RunResult:
    """One CLI invocation: how long it took and what the profile said."""

    __slots__ = ("wall", "code", "stderr", "first_token")

    def __init__(self, wall: float, code: int, stderr: str) -> None:
        self.wall = wall
        self.code = code
        self.stderr = stderr
        self.first_token = parse_first_token(stderr)

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def rate_limited(self) -> bool:
        lowered = self.stderr.lower()
        return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)

    @property
    def retry_after(self) -> float | None:
        return parse_retry_after(self.stderr)

    @property
    def reason(self) -> str:
        """The one line worth showing for a failed run."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        for line in lines:
            if line.startswith("[arena-api]") or "error" in line.lower():
                return line
        return lines[-1] if lines else f"exit code {self.code}"


def parse_first_token(stderr: str) -> float | None:
    """Time (seconds) until the first streamed token, from a JSON profile line."""
    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_PROFILE_PREFIX):
            continue
        payload = stripped[len(_PROFILE_PREFIX) :].strip()
        try:
            report = json.loads(payload)
        except ValueError:
            continue
        phases = report.get("phases")
        if not isinstance(phases, list):
            continue
        by_name: dict[str, float] = {}
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            name = phase.get("phase")
            at_ms = phase.get("at_ms")
            if isinstance(name, str) and isinstance(at_ms, (int, float)):
                by_name.setdefault(name, float(at_ms))
        for name in FIRST_TOKEN_PHASES:
            if name in by_name:
                return by_name[name] / 1000.0
    return None


def parse_retry_after(stderr: str) -> float | None:
    """Seconds the server asked us to wait, when the message mentions it."""
    match = _RETRY_AFTER_RE.search(stderr)
    if match is None:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:  # pragma: no cover - the regex only matches numbers
        return None


def run_once(python: str, argv: Sequence[str]) -> RunResult:
    env = dict(os.environ)
    env["ARENA_PROFILE"] = "json"
    env.pop("ARENA_WARM_SKIP", None)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [python, "-m", "arena_api", *argv],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as exc:  # pragma: no cover - interpreter missing
        return RunResult(time.monotonic() - started, 127, f"[arena-api] {exc}")
    return RunResult(
        time.monotonic() - started,
        completed.returncode,
        completed.stderr or "",
    )


def run_with_retries(
    python: str,
    argv: Sequence[str],
    *,
    retries: int,
    pause: float,
    out: "Reporter",
) -> RunResult:
    """Run one measured prompt, retrying only when it was rate-limited."""
    attempt = 0
    while True:
        result = run_once(python, argv)
        if result.ok or not result.rate_limited or attempt >= retries:
            return result
        attempt += 1
        wait = result.retry_after
        if wait is None:
            wait = pause * (2**attempt)
        wait = max(wait, 1.0)
        out.note(
            f"    rate limited, retrying in {wait:.0f}s "
            f"(attempt {attempt + 1} of {retries + 1})"
        )
        time.sleep(wait)


class Reporter:
    """Console output that stays readable when runs fail."""

    def note(self, message: str) -> None:
        print(message, flush=True)

    def warn(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _spread(values: list[float]) -> str:
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]:.2f}"
    return f"{min(values):.2f}–{max(values):.2f}"


def _row(name: str, values: list[float], failures: int) -> str:
    median = f"{_median(values):6.2f}" if values else "     -"
    return f"{name:<26}{_spread(values):<18}{median:>8}{failures:>7}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a cold CLI start with the warm daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--prompt",
        default="Ответь ровно одним словом: ок",
        help="prompt used for every run (keep the answer short)",
    )
    parser.add_argument("--model", default=None, help="forwarded --model")
    parser.add_argument("--runs", type=int, default=2, help="runs per variant (default 2)")
    parser.add_argument(
        "--pause",
        type=float,
        default=20.0,
        help=(
            "seconds between prompts, so the account's rate limit is not "
            "tripped (default 20, 0 disables)"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="extra attempts for a rate-limited run (default 2, each waits longer)",
    )
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter used for the runs"
    )
    parser.add_argument(
        "--stop-first",
        action="store_true",
        help="stop an already running daemon before measuring (recommended)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.retries < 0:
        parser.error("--retries must not be negative")
    if args.pause < 0:
        parser.error("--pause must not be negative")

    common = ["--prompt", args.prompt]
    if args.model:
        common += ["--model", args.model]
    out = Reporter()

    if args.stop_first:
        run_once(args.python, ["--warm-stop"])

    print(f"prompt: {args.prompt!r}")
    print(f"runs  : {args.runs} per variant, {args.pause:g}s apart, up to {args.retries} retries")
    print()

    variants: dict[str, list[RunResult]] = {"cold": [], "warm": []}
    first_warm: RunResult | None = None
    first_cold_failure: RunResult | None = None

    def measured(label: str, variant: str, extra: Sequence[str]) -> bool:
        """Run one prompt; ``False`` means this variant cannot continue."""
        nonlocal first_cold_failure
        if args.pause and any(variants.values()):
            time.sleep(args.pause)
        result = run_with_retries(
            args.python, [*common, *extra], retries=args.retries, pause=args.pause, out=out
        )
        if not result.ok:
            if first_cold_failure is None and variant == "cold":
                first_cold_failure = result
            out.warn(f"  {label} failed ({result.reason})")
            if variant == "cold" and not result.rate_limited and not variants["cold"]:
                out.warn(
                    "\n  The cold path cannot run at all — check `arena-api --status` first."
                )
                return False
            # A rate limit on a cold run does not invalidate the warm side
            # (the daemon already paid for the browser); keep both counters.
            if variant == "cold":
                variants["cold"].append(result)
            else:
                variants["warm"].append(result)
            return True
        out.note(f"  {label}: {result.wall:.2f}s")
        variants[variant].append(result)
        return True

    for index in range(args.runs):
        run_once(args.python, ["--warm-stop"])
        if not measured(f"cold run {index + 1}", "cold", ["--no-warm"]):
            return 2

    # The first warm run pays for the daemon start; report it apart.
    if args.pause:
        time.sleep(args.pause)
    first_warm = run_with_retries(
        args.python, common, retries=args.retries, pause=args.pause, out=out
    )
    if not first_warm.ok:
        out.warn(f"  first warm run failed ({first_warm.reason})")
        if first_warm.rate_limited:
            out.warn(
                "\n  Arena is rate-limiting this account right now. Wait a few "
                "minutes (or raise --pause) and run the benchmark again."
            )
        return 3
    out.note(f"  first warm run (daemon start): {first_warm.wall:.2f}s")

    warm_runs = args.runs + 1
    for index in range(warm_runs):
        if not measured(f"warm run {index + 1}", "warm", []):
            return 2

    run_once(args.python, ["--warm-stop"])

    def collect(variant: str, *, include_first: bool) -> list[RunResult]:
        runs = variants[variant]
        if variant == "warm" and include_first and first_warm is not None:
            return [first_warm, *runs]
        return runs

    cold = collect("cold", include_first=False)
    warm = collect("warm", include_first=False)

    print()
    print(f"{'variant':<26}{'runs, s':<18}{'median':>8}{'failed':>7}")
    print("-" * 59)
    print(_row("cold (--no-warm)", [r.wall for r in cold if r.ok], _failures(cold)))
    print(_row("warm (daemon reused)", [r.wall for r in warm if r.ok], _failures(warm)))
    if first_warm is not None:
        print(f"{'first warm (daemon start)':<26}{'-':<18}{first_warm.wall:>8.2f}{0:>7}")

    cold_ttft = [r.first_token for r in cold if r.ok and r.first_token is not None]
    warm_ttft = [r.first_token for r in warm if r.ok and r.first_token is not None]
    if cold_ttft or warm_ttft or (first_warm is not None and first_warm.first_token):
        print()
        print("time to first token (from ARENA_PROFILE=json)")
        print(f"{'variant':<26}{'runs, s':<18}{'median':>8}{'failed':>7}")
        print("-" * 59)
        print(_row("cold (--no-warm)", cold_ttft, 0))
        print(_row("warm (daemon reused)", warm_ttft, 0))
        if first_warm is not None and first_warm.first_token is not None:
            print(f"{'first warm (daemon start)':<26}{'-':<18}{first_warm.first_token:>8.2f}{0:>7}")

    cold_wall = _median([r.wall for r in cold if r.ok])
    warm_wall = _median([r.wall for r in warm if r.ok])
    cold_first = _median(cold_ttft)
    warm_first = _median(warm_ttft)
    print()
    if cold_wall and warm_wall:
        print(f"speed-up, whole command : {cold_wall / warm_wall:.2f}x")
    if cold_first and warm_first:
        print(f"speed-up, first token   : {cold_first / warm_first:.2f}x")
    if first_cold_failure is not None and first_cold_failure.rate_limited and not cold_wall:
        out.warn(
            "\nEvery cold run hit Arena's rate limit. Lower the number of "
            "prompts (--runs), raise --pause, or wait a few minutes."
        )
    if not warm_wall:
        out.warn("No warm run succeeded, so there is nothing to compare.")
        return 3
    print(
        "\nReminder: the whole-command number includes the model's own "
        "generation time, so it understates the daemon's effect;\n"
        "'first token' is the overhead the daemon removes."
    )
    return 0


def _failures(runs: list[RunResult]) -> int:
    return sum(1 for run in runs if not run.ok)


if __name__ == "__main__":
    raise SystemExit(main())
