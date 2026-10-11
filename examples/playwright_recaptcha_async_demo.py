"""Асинхронный пример: Playwright + stealth + решение reCAPTCHA v2/v3.

Запуск::

    python -m pip install ".[captcha]"
    python -m playwright install chromium

    python examples/playwright_recaptcha_async_demo.py
    python examples/playwright_recaptcha_async_demo.py --version v3
    python examples/playwright_recaptcha_async_demo.py --stealth-check

Отличие от синхронного варианта только в ``async``/``await``: конфигурация и
последовательность вызовов полностью совпадают.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recaptcha_toolkit import (
    AsyncPlaywrightBot,
    BrowserOptions,
    CaptchaError,
    PlaywrightSolverOptions,
    SolverBackend,
    StealthOptions,
)

DEMO_V2_URL = "https://www.google.com/recaptcha/api2/demo"
DEMO_V3_URL = "https://antcpt.com/score_detector/"
GOOGLE_URL = "https://google.com"


async def check_stealth(bot: AsyncPlaywrightBot) -> None:
    """Проверить применение stealth-скриптов.

    :param bot: запущенный асинхронный бот.
    """
    page = await bot.new_page()
    await page.goto(GOOGLE_URL, wait_until="domcontentloaded")

    fingerprint = await page.evaluate(
        """() => ({
            webdriver: navigator.webdriver,
            languages: navigator.languages,
            platform: navigator.platform,
            vendor: navigator.vendor,
        })"""
    )
    print("  navigator.webdriver :", fingerprint["webdriver"], "(должно быть undefined)")
    print("  navigator.languages :", fingerprint["languages"])
    print("  navigator.platform  :", fingerprint["platform"])
    print("  navigator.vendor    :", fingerprint["vendor"])
    await page.close()


async def solve_v2(bot: AsyncPlaywrightBot) -> None:
    """Решить v2 на демо-странице.

    :param bot: запущенный асинхронный бот.
    """
    page = await bot.new_page()
    await page.goto(DEMO_V2_URL)

    print(f"  решаю reCAPTCHA v2 на {DEMO_V2_URL} ...")
    token = await bot.solve_recaptcha_v2(page, wait=True, wait_timeout=30)
    print(f"  токен получен ({len(token)} симв.): {token[:60]}...")
    await page.close()


async def solve_v3(bot: AsyncPlaywrightBot) -> None:
    """Перехватить токен v3.

    :param bot: запущенный асинхронный бот.
    """
    page = await bot.new_page()
    await page.goto(DEMO_V3_URL, wait_until="domcontentloaded")

    print(f"  перехватываю токен reCAPTCHA v3 на {DEMO_V3_URL} ...")
    token = await bot.solve_recaptcha_v3(page, timeout=30, reload=True)
    print(f"  токен получен ({len(token)} симв.): {token[:60]}...")
    await page.close()


async def run(args: argparse.Namespace) -> None:
    """Подготовить конфигурацию и выполнить выбранный сценарий.

    :param args: разобранные аргументы командной строки.
    """
    stealth = StealthOptions(enabled=not args.no_stealth)
    browser = BrowserOptions(headless=args.headless, proxy=args.proxy)
    solver = PlaywrightSolverOptions(attempts=5, wait=True, wait_timeout=30)

    async with AsyncPlaywrightBot(
        backend=SolverBackend(args.backend),
        stealth=stealth,
        browser=browser,
        solver=solver,
    ) as bot:
        if args.stealth_check:
            await check_stealth(bot)
        elif args.version == "v3":
            await solve_v3(bot)
        else:
            await solve_v2(bot)


def main() -> int:
    """Точка входа примера.

    :returns: код возврата процесса.
    """
    parser = argparse.ArgumentParser(description="Playwright + reCAPTCHA (асинхронно)")
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in SolverBackend],
        default=SolverBackend.AUDIO.value,
    )
    parser.add_argument("--version", choices=["v2", "v3"], default="v2")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--stealth-check", action="store_true")
    parser.add_argument("--no-stealth", action="store_true")
    parser.add_argument("--proxy", default=None)
    args = parser.parse_args()

    print(f"бэкенд: {args.backend} | версия: {args.version}")

    try:
        asyncio.run(run(args))
    except CaptchaError as exc:
        print(f"капчу решить не удалось: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
