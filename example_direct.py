"""Current Arena Direct Chat example.

Set ARENA_VERIFICATION_TOKEN to a fresh verification assertion obtained through
your normal browser session.  Arena expires it frequently, so production code
should pass a callback that supplies a fresh user-provided value per request.
"""

from __future__ import annotations

import os
import sys

from arena_api import DirectArenaClient


def main() -> None:
    token = os.environ.get("ARENA_VERIFICATION_TOKEN")
    if not token:
        raise SystemExit("Set ARENA_VERIFICATION_TOKEN to a fresh browser-provided verification token.")

    with DirectArenaClient(
        cookies=os.environ.get("ARENA_COOKIE"),
        verification_token=token,
    ) as arena:
        models = arena.get_models()
        print("Available models:", len(models))
        model = next(item.display_name for item in models if item.display_name == "Max")

        print("Single response:", arena.complete("Reply with one greeting.", model=model))

        print("Streaming: ", end="", flush=True)
        for chunk in arena.stream("Count one, two, three.", model=model):
            sys.stdout.write(chunk.delta)
            sys.stdout.flush()
        print()

        chat = arena.chat(model=model)
        print("Turn 1:", chat.send_message("My name is Ada."))
        print("Turn 2:", chat.send_message("What name did I give you?"))
        print("Stored turns:", chat.turn_id)


if __name__ == "__main__":
    main()
