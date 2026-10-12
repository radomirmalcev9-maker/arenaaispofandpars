"""Синхронный пример: Playwright + stealth + решение reCAPTCHA v2/v3.

Запуск::

    python -m pip install ".[captcha]"
    python -m playwright install chromium

    python examples/playwright_recaptcha_demo.py                 # v2, аудио
    python examples/playwright_recaptcha_demo.py --version v3    # v3
    python examples/playwright_recaptcha_demo.py --backend image # v2 по картинкам
    python examples/playwright_recaptcha_demo.py --stealth-check # только проверка маскировки

Для варианта ``--backend image`` нужен ключ CapSolver::

    export CAPSOLVER_API_KEY=...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arena_api._vendor.recaptcha_toolkit import (
    BrowserOptions,
    CaptchaError,
    PlaywrightBot,
    PlaywrightSolverOptions,
    SolverBackend,
    StealthOptions,
)

# Официальный демо-стенд Google с настоящим виджетом reCAPTCHA v2.
DEMO_V2_URL = "https://www.google.com/recaptcha/api2/demo"
# Страница с reCAPTCHA v3 (из документации playwright-recaptcha).
DEMO_V3_URL = "https://antcpt.com/score_detector/"
# Главная Google: виджета нет, используется только для проверки маскировки.
GOOGLE_URL = "https://google.com"


def check_stealth(bot: PlaywrightBot) -> None:
    """Проверить, что маскировка применилась, и напечатать ключевые признаки.

    :param bot: запущенный бот.
    """
    page = bot.new_page()
    page.goto(GOOGLE_URL, wait_until="domcontentloaded")

    fingerprint = page.evaluate(
        """() => ({
            webdriver: navigator.webdriver,
            languages: navigator.languages,
            platform: navigator.platform,
            vendor: navigator.vendor,
            userAgent: navigator.userAgent,
            plugins: navigator.plugins.length,
        })"""
    )

    print("  navigator.webdriver :", fingerprint["webdriver"], "(должно быть undefined)")
    print("  navigator.languages :", fingerprint["languages"])
    print("  navigator.platform  :", fingerprint["platform"])
    print("  navigator.vendor    :", fingerprint["vendor"])
    print("  navigator.userAgent :", fingerprint["userAgent"][:80], "...")

    if fingerprint["webdriver"] is None or fingerprint["webdriver"] is False:
        print("  => маскировка работает: navigator.webdriver скрыт")
    else:
        print("  => ВНИМАНИЕ: navigator.webdriver всё ещё виден")

    page.close()


def solve_v2(bot: PlaywrightBot) -> None:
    """Решить reCAPTCHA v2 на демо-странице.

    :param bot: запущенный бот.
    """
    page = bot.new_page()
    page.goto(DEMO_V2_URL)

    print(f"  решаю reCAPTCHA v2 на {DEMO_V2_URL} ...")
    token = bot.solve_recaptcha_v2(page, wait=True, wait_timeout=30)
    print(f"  токен получен ({len(token)} симв.): {token[:60]}...")

    # Токен пригоден для отправки формы; сам Google его примет только
    # для этого демо-стенда.
    print("  капча решена:", page.locator("#recaptcha-demo-submit").is_enabled())
    page.close()


def solve_v3(bot: PlaywrightBot) -> None:
    """Перехватить токен reCAPTCHA v3.

    Слушатель надо подключать до запроса ``/recaptcha/.../reload``, поэтому
    страница перезагружается уже внутри солвера (``reload=True``).

    :param bot: запущенный бот.
    """
    page = bot.new_page()
    page.goto(DEMO_V3_URL, wait_until="domcontentloaded")

    print(f"  перехватываю токен reCAPTCHA v3 на {DEMO_V3_URL} ...")
    token = bot.solve_recaptcha_v3(page, timeout=30, reload=True)
    print(f"  токен получен ({len(token)} симв.): {token[:60]}...")
    page.close()


def main() -> int:
    """Точка входа примера.

    :returns: код возврата процесса.
    """
    parser = argparse.ArgumentParser(description="Playwright + reCAPTCHA (синхронно)")
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in SolverBackend],
        default=SolverBackend.AUDIO.value,
        help="audio — аудио-челлендж; image — картинки через CapSolver; vision_ai — YOLO",
    )
    parser.add_argument("--version", choices=["v2", "v3"], default="v2")
    parser.add_argument("--headless", action="store_true", help="без GUI")
    parser.add_argument("--stealth-check", action="store_true", help="только проверка маскировки")
    parser.add_argument("--no-stealth", action="store_true", help="отключить маскировку")
    parser.add_argument("--proxy", default=None, help="прокси protocol://[user:pass@]host:port")
    args = parser.parse_args()

    stealth = StealthOptions(
        enabled=not args.no_stealth,
        navigator_languages=("en-US", "en"),
        locale="en-US",
        timezone_id="America/New_York",
    )
    browser = BrowserOptions(headless=args.headless, proxy=args.proxy)
    solver = PlaywrightSolverOptions(attempts=5, wait=True, wait_timeout=30)

    print(f"бэкенд: {args.backend} | версия: {args.version} | stealth: {stealth.enabled}")

    try:
        with PlaywrightBot(
            backend=SolverBackend(args.backend),
            stealth=stealth,
            browser=browser,
            solver=solver,
        ) as bot:
            if args.stealth_check:
                check_stealth(bot)
            elif args.version == "v3":
                solve_v3(bot)
            else:
                solve_v2(bot)
    except CaptchaError as exc:
        print(f"капчу решить не удалось: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
