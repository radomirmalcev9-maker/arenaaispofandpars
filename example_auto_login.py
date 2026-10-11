"""Run with: python example_auto_login.py

Zero-configuration bootstrap: the only manual step is being signed in to your
own account in your own browser. Everything else -- finding the session,
matching its fingerprint, verifying it, refreshing it when the service rejects
it, and (optionally) clearing a reCAPTCHA -- is done for you.

    # read a Chrome/Edge/Brave/... profile too
    python -m pip install '.[firefox]'
    # solve reCAPTCHA challenges automatically while refreshing
    python -m pip install '.[auth-captcha]' && playwright install chromium

Diagnostics without touching the network:

    python -m arena_api --explain          # every source considered, and why
    python -m arena_api --status           # found session + one probe request
"""

from __future__ import annotations

from arena_api import AutoOptions, GenerationConfig, auto_client


def main() -> None:
    # Every field of AutoOptions already defaults to the zero-configuration
    # behaviour, so this is the same call as plain ``auto_client()``:
    #
    #     base_url="https://arena.ai"  frontend to talk to
    #     solve_captchas="auto"        on as soon as the solver deps exist
    #     captcha_backend="audio"      audio | image | vision_ai
    #     use_browser_profiles=True    read Firefox (and Chrome/Edge via
    #                                  rookiepy) instead of asking for cookies
    #     interactive=True             wait for Enter rather than fail
    #     verify=True                  one probe request at startup
    options = AutoOptions()
    client = auto_client(options)

    with client as arena:
        manager = arena.session_manager
        print("session cookies:", ", ".join(sorted(manager.load_cookies())))
        print("user-agent     :", manager.firefox_headers()["User-Agent"])

        models = arena.get_models()
        print("models         :", ", ".join(model.id for model in models[:10]))

        print("answer         : ", end="", flush=True)
        for chunk in arena.stream(
            "Объясни одним предложением, зачем нужен session cookie.",
            model=models[0].id if models else None,
            config=GenerationConfig(temperature=0.2, max_tokens=64, queue_timeout=120),
        ):
            print(chunk.delta, end="", flush=True)
        print()

        # A rejected session never reaches you as an exception: the transport
        # asks the manager for fresh cookies (re-read your browser profile, run
        # a form login, or clear a challenge) and replays the request.
        print("still signed in:", bool(manager.load_cookies()))


if __name__ == "__main__":
    main()
