"""Async Playwright wrapper with reproducible, CI-ready browser contexts.

This is the browser half of the ``arena_api.auth`` package. It owns three
things and nothing else:

1. **Context normalisation** -- locale, timezone, viewport, colour scheme and
   reduced-motion are Playwright's own supported knobs, and they are the
   correct way to make a headless run match a reference run. Setting them here
   means every test in a suite shares one configuration instead of each test
   inventing its own.
2. **Determinism injection** -- the init script from
   :mod:`arena_api.auth.environment` is installed on every page.
3. **Artefact capture** -- traces, HARs and screenshots land in one directory
   so a CI failure is diagnosable after the runner is gone.

It does **not** install stealth/fingerprint patches. See
:mod:`arena_api.auth.environment` for the reasoning; the short version is that
a synthetic monitor should be identifiable, not camouflaged.

Playwright is imported lazily, so importing this module never requires the
package (or a downloaded browser binary) to be present.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path
from typing import Any, Literal

from .environment import DeterminismOptions, build_init_script
from .kinematics import KinematicMouse, TrajectoryPlanner

__all__ = [
    "BrowserEnvironment",
    "BrowserEnvironmentOptions",
    "SYNTHETIC_MONITOR_HEADER",
]

#: Header attached when :attr:`BrowserEnvironmentOptions.identify_automation`
#: is enabled, so the site under test can recognise and allowlist the monitor.
SYNTHETIC_MONITOR_HEADER = "X-Synthetic-Monitor"


def _library_version() -> str:
    try:
        return _distribution_version("arena-api")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass
class BrowserEnvironmentOptions:
    """Configuration for :class:`BrowserEnvironment`.

    Defaults are chosen for headless CI: a fixed 1440x900 viewport, UTC,
    reduced motion, deterministic animations, and artefacts on disk.
    """

    headless: bool = True

    #: Chromium channel, e.g. ``"chromium"`` or ``"chrome"``. ``None`` uses
    #: Playwright's bundled build.
    channel: str | None = None

    #: Sets ``navigator.language`` and ``navigator.languages``.
    locale: str = "en-US"

    #: Sets the ``Intl``/``Date`` timezone.
    timezone_id: str = "UTC"

    #: Viewport in CSS pixels; ``None`` uses the window size.
    viewport: dict[str, int] | None = field(
        default_factory=lambda: {"width": 1440, "height": 900}
    )

    device_scale_factor: float = 1.0

    #: ``None`` keeps Playwright's default UA for the browser build.
    user_agent: str | None = None

    reduced_motion: Literal["reduce", "no-preference", "null"] = "reduce"
    color_scheme: Literal["light", "dark", "no-preference", "null"] = "light"

    #: Emulates ``prefers-reduced-transparency`` / contrast where supported.
    forced_colors: Literal["active", "none", "null"] = "none"

    java_script_enabled: bool = True
    ignore_default_args: tuple[str, ...] = ()
    extra_http_headers: dict[str, str] = field(default_factory=dict)

    #: Maps to Playwright's ``chromium_sandbox``. Disabling it is required in
    #: some container images but weakens the browser sandbox, so it is opt-in.
    no_sandbox: bool = False

    #: Extra Chromium switches; ``--disable-dev-shm-usage`` is added
    #: automatically when ``container_safe`` is set.
    launch_args: tuple[str, ...] = ()
    container_safe: bool = True

    slow_mo: int = 0
    default_timeout: float = 30_000.0
    navigation_timeout: float = 30_000.0

    #: Determinism script options; ``None`` installs the defaults.
    determinism: DeterminismOptions | None = None

    #: Set to ``False`` to skip the init script entirely.
    install_determinism_script: bool = True

    #: Announce the run as synthetic (header + ``window.__arenaSynthetic``).
    identify_automation: bool = True

    #: Path to a Playwright ``storage_state`` JSON used to seed cookies.
    storage_state: str | Path | None = None

    #: Directory for traces, HARs and screenshots. ``None`` disables capture.
    artifacts_dir: str | Path | None = None

    record_har: bool = False
    record_trace: bool = False
    screenshot_on_close: bool = False

    #: Resource types dropped before they hit the network (speed, not stealth:
    #: skipping media and fonts removes a large source of timing flake).
    block_resource_types: tuple[str, ...] = ()

    #: Trajectory planner handed to :meth:`BrowserEnvironment.mouse`.
    planner: TrajectoryPlanner | None = None

    def context_kwargs(self) -> dict[str, Any]:
        """Render the Playwright ``new_context`` keyword arguments."""
        kwargs: dict[str, Any] = {
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "device_scale_factor": self.device_scale_factor,
            "reduced_motion": self.reduced_motion,
            "color_scheme": self.color_scheme,
            "java_script_enabled": self.java_script_enabled,
        }
        if self.viewport is not None:
            kwargs["viewport"] = dict(self.viewport)
            kwargs["no_viewport"] = False
        else:
            kwargs["no_viewport"] = True
        if self.user_agent:
            kwargs["user_agent"] = self.user_agent
        if self.forced_colors != "none":
            kwargs["forced_colors"] = self.forced_colors
        if self.storage_state:
            kwargs["storage_state"] = str(self.storage_state)
        headers = dict(self.extra_http_headers)
        if self.identify_automation:
            headers.setdefault(
                SYNTHETIC_MONITOR_HEADER, f"arena-api/{_library_version()}"
            )
        if headers:
            kwargs["extra_http_headers"] = headers
        return kwargs

    def launch_kwargs(self) -> dict[str, Any]:
        """Render the Playwright ``launch`` keyword arguments."""
        args: list[str] = list(self.launch_args)
        if self.container_safe:
            args.append("--disable-dev-shm-usage")
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "args": args,
            # Playwright's own switch, rather than a raw --no-sandbox flag.
            "chromium_sandbox": not self.no_sandbox,
        }
        if self.channel:
            kwargs["channel"] = self.channel
        if self.ignore_default_args:
            kwargs["ignore_default_args"] = list(self.ignore_default_args)
        return kwargs


class BrowserEnvironment:
    """Owns a Playwright browser and hands out normalised pages.

    Usable as an async context manager::

        async with BrowserEnvironment() as env:
            page = await env.new_page()
            await page.goto("https://example.com")

    Or manually with :meth:`start` / :meth:`close`.
    """

    def __init__(
        self,
        options: BrowserEnvironmentOptions | None = None,
        *,
        artifacts_dir: str | Path | None = None,
    ) -> None:
        self.options = options or BrowserEnvironmentOptions()
        if artifacts_dir is not None:
            self.options.artifacts_dir = artifacts_dir
        if self.options.artifacts_dir is not None:
            self.options.artifacts_dir = Path(self.options.artifacts_dir)
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: list[Any] = []
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has completed successfully."""
        return self._started

    @property
    def browser(self) -> Any:
        """The underlying Playwright ``Browser``."""
        if self._browser is None:
            raise RuntimeError("BrowserEnvironment is not started; call start() first")
        return self._browser

    @property
    def artifacts_dir(self) -> Path | None:
        """Directory that captured artefacts are written to."""
        directory = self.options.artifacts_dir
        return Path(directory) if directory is not None else None

    async def start(self) -> "BrowserEnvironment":
        """Launch Playwright and the browser."""
        if self._started:
            return self
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            **self.options.launch_kwargs()
        )
        self._started = True
        return self

    async def close(self) -> None:
        """Close every context, then the browser and Playwright."""
        if not self._started:
            return
        for context in list(self._contexts):
            await self._safe_close_context(context)
        self._contexts.clear()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._started = False

    async def __aenter__(self) -> "BrowserEnvironment":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def _safe_close_context(self, context: Any) -> None:
        try:
            if self.options.record_trace:
                await context.tracing.stop(path=str(self._artifact_path("trace.zip")))
        except Exception:
            pass
        try:
            if self.options.screenshot_on_close:
                for index, page in enumerate(context.pages):
                    await self.screenshot(page, name=f"close-page-{index}")
        except Exception:
            pass
        try:
            await context.close()
        except Exception:
            pass

    def _artifact_path(self, name: str) -> Path:
        directory = self.artifacts_dir
        if directory is None:
            raise RuntimeError(
                "artifacts_dir is not configured; set BrowserEnvironmentOptions."
                "artifacts_dir to capture traces, HARs or screenshots"
            )
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    # ------------------------------------------------------------------ #
    # Contexts and pages
    # ------------------------------------------------------------------ #

    async def new_context(self, **overrides: Any) -> Any:
        """Create a browser context with the configured normalisation."""
        if not self._started:
            await self.start()
        kwargs = self.options.context_kwargs()
        kwargs.update(overrides)
        if self.options.record_har:
            kwargs["record_har_path"] = str(self._artifact_path("network.har"))
            kwargs.setdefault("record_har_content", "omit")
        context = await self.browser.new_context(**kwargs)
        context.set_default_timeout(self.options.default_timeout)
        context.set_default_navigation_timeout(self.options.navigation_timeout)
        if self.options.record_trace:
            await context.tracing.start(
                screenshots=True, snapshots=True, sources=True
            )
        self._contexts.append(context)
        return context

    async def new_page(self, context: Any | None = None, **overrides: Any) -> Any:
        """Create a page with the determinism init script installed.

        A new context is created when ``context`` is not supplied.
        """
        created = context is None
        if created:
            context = await self.new_context(**overrides)
        page = await context.new_page()
        if self.options.install_determinism_script:
            script = build_init_script(
                self.options.determinism or DeterminismOptions()
            )
            await page.add_init_script(script=script)
        if self.options.block_resource_types:
            blocked = set(self.options.block_resource_types)

            async def _route_handler(route: Any, request: Any) -> None:
                if request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _route_handler)
        return page

    def mouse(self, page: Any, **kwargs: Any) -> KinematicMouse:
        """Return a :class:`KinematicMouse` bound to ``page``."""
        planner = self.options.planner or TrajectoryPlanner()
        return KinematicMouse(page, planner, **kwargs)

    # ------------------------------------------------------------------ #
    # Artefacts and state
    # ------------------------------------------------------------------ #

    async def screenshot(self, page: Any, name: str = "screenshot.png", **kwargs: Any) -> Path:
        """Save a screenshot into the artefacts directory.

        ``animations="disabled"`` and ``caret="hide"`` are Playwright's own
        supported stabilisers and are applied by default, on top of the
        init-script CSS (which also stabilises ``bounding_box()`` timings and
        non-screenshot assertions).
        """
        path = self._artifact_path(name)
        options: dict[str, Any] = {"animations": "disabled", "caret": "hide"}
        options.update(kwargs)
        options.setdefault("full_page", True)
        await page.screenshot(path=str(path), **options)
        return path

    async def save_storage_state(self, context: Any, name: str = "state.json") -> Path:
        """Persist cookies and ``localStorage`` to ``artifacts_dir/name``."""
        path = self._artifact_path(name)
        await context.storage_state(path=str(path))
        return path

    @staticmethod
    def stamp() -> str:
        """UTC timestamp usable as a unique artefact prefix."""
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def run_sync(coroutine: Any) -> Any:
    """Run ``coroutine`` from synchronous code.

    Uses ``asyncio.run`` on a thread with no loop, and a dedicated thread with
    a fresh loop when one is already running (so this is safe to call from
    inside an async client that needs a blocking session refresh).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()
