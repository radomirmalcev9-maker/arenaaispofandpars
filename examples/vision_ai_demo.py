"""Пример: локальный YOLO-солвер VisionAIRecaptchaSolver-0.

В отличае от ``playwright-recaptcha``, этот солвер не работает с уже открытой
страницей: он сам поднимает Chromium, реплицирует виджет и решает челлендж
картинками. Поэтому на вход ему нужны ``website_key`` и ``website_url``.

Запуск::

    python -m pip install ".[vision]"
    python examples/vision_ai_demo.py
    python examples/vision_ai_demo.py --key <site_key> --url <page_url>
    python examples/vision_ai_demo.py --invisible --action login   # невидимая v3

При первом запуске скачиваются модели (классификация с Hugging Face и
``yolo12x.pt`` через ultralytics) — это несколько сотен мегабайт.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recaptcha_toolkit import (
    BrowserOptions,
    CaptchaError,
    PlaywrightBot,
    SolverBackend,
    StealthOptions,
    VisionAIOptions,
    VisionAISolver,
)
from recaptcha_toolkit.vision_ai import VisionAIResult

# Публичный тестовый site key демо-стенда Google.
DEMO_SITE_KEY = "6Le-wvkSVVABCPBMRTvw0Q4Muexq1bi0DJwx_mJ-"
DEMO_SITE_URL = "https://www.google.com/recaptcha/api2/demo"


def solve_direct(args: argparse.Namespace) -> None:
    """Решить капчу напрямую через :class:`VisionAISolver`.

    Самый короткий путь: солверу не нужен Playwright вообще.

    :param args: аргументы командной строки.
    """
    options = VisionAIOptions(
        timeout=args.timeout,
        headless=args.headless,
        server_port=args.port,
        is_invisible=args.invisible,
        action=args.action,
        log_level=args.log_level,
    )
    solver = VisionAISolver(options)

    print(f"  решаю: key={args.key[:12]}... url={args.url}")
    result: VisionAIResult = solver.solve_detailed(
        website_key=args.key,
        website_url=args.url,
    )
    _report(result)


def solve_via_bot(args: argparse.Namespace) -> None:
    """Решить капчу через :class:`PlaywrightBot` с бэкендом ``VISION_AI``.

    Здесь ``website_key`` и ``website_url`` определяются автоматически:
    бот открывает страницу, вытаскивает site key из разметки или ``src``
    iframe и подставляет URL текущей страницы.

    :param args: аргументы командной строки.
    """
    with PlaywrightBot(
        backend=SolverBackend.VISION_AI,
        stealth=StealthOptions(),
        browser=BrowserOptions(headless=True),
        vision_ai=VisionAIOptions(
            timeout=args.timeout,
            headless=args.headless,
            server_port=args.port,
            log_level=args.log_level,
        ),
    ) as bot:
        page = bot.new_page()
        page.goto(DEMO_SITE_URL)

        # Ключ и URL можно не передавать — бот найдёт их сам.
        token = bot.solve_recaptcha_v2(page)
        print(f"  токен получен ({len(token)} симв.): {token[:60]}...")


async def solve_async(args: argparse.Namespace) -> None:
    """Асинхронный вариант решения.

    Конкурентным вызовам нужны разные ``server_port`` — иначе репликаторы
    домена конфликтуют за порт.

    :param args: аргументы командной строки.
    """
    solver = VisionAISolver(
        VisionAIOptions(
            timeout=args.timeout,
            headless=args.headless,
            server_port=args.port,
            log_level=args.log_level,
        )
    )
    result = await solver.solve_async(website_key=args.key, website_url=args.url)
    _report(result)


def _report(result: VisionAIResult) -> None:
    """Напечатать результат решения.

    :param result: результат YOLO-солвера.
    """
    print("  токен:", result.token[:60], "...")
    print("  тип челленджа:", result.captcha_type)
    print("  затрачено, с:", result.elapsed)
    print("  попыток:", result.attempts)


def main() -> int:
    """Точка входа примера.

    :returns: код возврата процесса.
    """
    parser = argparse.ArgumentParser(description="VisionAIRecaptchaSolver-0 (YOLO)")
    parser.add_argument("--key", default=DEMO_SITE_KEY, help="site key виджета")
    parser.add_argument("--url", default=DEMO_SITE_URL, help="страница с виджетом")
    parser.add_argument("--mode", choices=["direct", "bot", "async"], default="direct")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--invisible", action="store_true", help="невидимая reCAPTCHA (v3)")
    parser.add_argument("--action", default=None, help="action для невидимой reCAPTCHA")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    try:
        if args.mode == "bot":
            solve_via_bot(args)
        elif args.mode == "async":
            asyncio.run(solve_async(args))
        else:
            solve_direct(args)
    except CaptchaError as exc:
        print(f"капчу решить не удалось: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
