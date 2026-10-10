"""Run with: ARENA_COOKIE='name=value; other=value' python example.py"""

from __future__ import annotations

import os
import sys

from arena_api import ArenaClient, GenerationConfig


def main() -> None:
    cookie = os.environ.get("ARENA_COOKIE")
    cookie_file = os.environ.get("ARENA_COOKIES_FILE")
    base_url = os.environ.get("ARENA_BASE_URL", "https://arena.ai")
    if not cookie and not cookie_file:
        raise SystemExit(
            "Set ARENA_COOKIE or ARENA_COOKIES_FILE to your own cookie export."
        )

    # If the frontend has changed, inspect arena.config() and pass the callback
    # index and a chat_payload_factory matching that authorized callback schema.
    with ArenaClient(base_url=base_url, cookies=cookie_file or cookie) as arena:
        models = arena.get_models()
        print("Known model choices:", ", ".join(model.id for model in models[:10]))
        print("Chat queue endpoint:", arena.get_endpoint("chat").fn_index)

        response = arena.complete(
            "Reply with exactly one short greeting.",
            config=GenerationConfig(queue_timeout=120),
        )
        print("Single response:", response)

        print("Streaming: ", end="", flush=True)
        for chunk in arena.stream("Count from one to three, separated by commas."):
            sys.stdout.write(chunk.delta)
            sys.stdout.flush()
        print()

        chat = arena.chat(config=GenerationConfig(temperature=0.2))
        print("Turn 1:", chat.send_message("My name is Ada."))
        print("Turn 2:", chat.send_message("What name did I give you?"))
        print("Stored turns:", chat.turn_id)


if __name__ == "__main__":
    main()
