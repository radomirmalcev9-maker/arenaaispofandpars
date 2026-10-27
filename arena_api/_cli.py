"""Console entry point, with a fast route for one-shot Direct Chat prompts.

``arena-api --prompt "..."`` is by far the most common invocation, and starting
a browser for it is the slow part.  When the command line is exactly that, the
:mod:`arena_api.warm` daemon answers it over loopback and this module never
imports the heavy bootstrap (:mod:`arena_api.auto` pulls in ``httpx``, the
session machinery and the Playwright bridge, which costs ~150 ms before any
work starts).

Everything else — ``--login``, ``--status``, ``--models``, ``--diagnose``,
``--help``, an explicit ``--verification-token``, ``--no-warm`` — falls through
to :func:`arena_api.auto.main` unchanged, so the CLI behaves exactly as before.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import latency

#: Anything that makes the command line something other than a single prompt.
_DISQUALIFYING = frozenset(
    {
        "-h",
        "--help",
        "--version",
        "--login",
        "--fresh",
        "--headless",
        "--diagnose",
        "--json",
        "--explain",
        "--status",
        "--models",
    }
)


def _wants_warm_prompt(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    if any(item in _DISQUALIFYING for item in argv):
        return False
    return any(item == "--prompt" or item.startswith("--prompt=") for item in argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI; ``argv`` defaults to ``sys.argv[1:]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    latency.mark("cli.entry")
    if any(
        flag in args or any(item.startswith(f"{flag}=") for item in args)
        for flag in ("--warm-stop", "--warm-status", "--warm-start", "--warm-profile")
    ):
        from .warm import profile_main, start_main, status_main, stop_main

        if "--warm-stop" in args:
            return stop_main(args)
        if "--warm-status" in args:
            return status_main(args)
        if "--warm-start" in args:
            return start_main(args)
        return profile_main(args)
    if _wants_warm_prompt(args):
        from .warm import fast_prompt_main

        code = fast_prompt_main(args)
        if code is not None:
            return code
    from .auto import main as auto_main

    return auto_main(args)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
