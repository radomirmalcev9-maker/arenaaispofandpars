"""Run once with your own current Arena session and assertion.

ARENA_VERIFICATION_TOKEN must be supplied by the caller through its normal
authorized browser flow. The library does not retrieve or solve it.
"""

from __future__ import annotations

import os
import sys

from arena_api import DirectArenaClient


def main() -> None:
    token = os.environ.get("ARENA_VERIFICATION_TOKEN")
    if not token:
        raise SystemExit("Set ARENA_VERIFICATION_TOKEN to a caller-provided assertion.")

    with DirectArenaClient(
        cookies=os.environ.get("ARENA_COOKIE"),
        verification_token=token,
    ) as arena:
        models = arena.get_models()
        if not models:
            raise SystemExit("No Direct Chat models were found in the current page.")
        model = models[0].display_name or models[0].id
        print("Available models:", len(models), "— selected:", model)

        for chunk in arena.stream("Reply with one short greeting.", model=model):
            sys.stdout.write(chunk.delta)
            sys.stdout.flush()
        print()


if __name__ == "__main__":
    main()
