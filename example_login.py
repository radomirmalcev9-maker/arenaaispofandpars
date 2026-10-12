"""Run with:

    python -m playwright install chromium
    python example_login.py

The one manual step, scripted: open a real browser window, sign in to your
account by hand, and keep the session on disk so that every later run needs no
arguments at all.

The same thing from a terminal, with no Python:

    python -m arena_api --login                # or simply: arena-api --login
    python -m arena_api --login --models       # sign in, then use it right away
    python -m arena_api --login --fresh        # switch accounts
    python -m arena_api --login --storage my-state/cookies.json

What happens, and what does *not*:

1. a headed Chromium opens at the site (``identify_automation=False``, no
   determinism overrides -- it behaves like your own browser);
2. you sign in however you normally do: password manager, 2FA app, "remember
   this device". No credential ever passes through this library;
3. the call polls the context and accepts the round as soon as **new cookies
   appear** for the site's domains -- it does not need to know the session
   cookie's name;
4. ``./state/cookies.json`` is written with mode ``0600`` (git-ignored), and an
   anonymous state never overwrites a good one;
5. the file is probed against ``/config`` so you learn immediately whether the
   server actually accepts it.

Afterwards ``auto_client()`` finds that file on its own -- see
``example_auto_login.py``.
"""

from __future__ import annotations

import os
from importlib.util import find_spec
from pathlib import Path

from arena_api import AutoOptions, interactive_login
from arena_api.auth import DEFAULT_STATE_PATH

STATE_PATH = Path(os.environ.get("ARENA_STATE_PATH", str(DEFAULT_STATE_PATH)))


def main() -> int:
    if find_spec("playwright") is None:
        raise SystemExit(
            "Playwright is not installed. Run: python -m playwright install chromium"
        )

    outcome = interactive_login(
        AutoOptions(
            # Where the session is stored. The default (./state/cookies.json) is
            # the path every later auto_client() run looks for first.
            storage_path=STATE_PATH,
            # ``interactive=True`` (the default) is what allows the "press Enter"
            # prompts; a scheduled job should set it to False.
            interactive=True,
            # Waiting out a verification screen met on the way to the sign-in
            # form is on by default and needs no extra dependency.
            wait_out_challenges=True,
        ),
        # Ignore an existing state file to sign in as somebody else.
        fresh=os.environ.get("ARENA_FRESH") == "1",
        # How many Enter presses are offered before giving up.
        rounds=5,
        # A headed window is the whole point: you have to see the page.
        headless=False,
    )

    print(f"login           : {outcome.login.describe()}")
    if outcome.verification is not None:
        print(f"verification    : {outcome.verification.describe()}")
    if outcome.login.cookies:
        print(f"cookie names    : {', '.join(sorted(outcome.login.site_cookies()))}")

    if not outcome.ok:
        print(
            "no usable session; run again and finish the sign-in "
            "(or check whether the server rejected the cookies)",
        )
        return 1

    print(f"saved to        : {outcome.path}")

    # From here on, nothing needs configuring:
    #
    #     from arena_api import auto_client
    #     with auto_client() as arena:
    #         print(len(arena.get_models()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
