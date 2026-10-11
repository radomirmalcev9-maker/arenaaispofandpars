"""Пример: Selenium + маскировка автоматизации + решение reCAPTCHA v2.

Запуск::

    python -m pip install ".[captcha-selenium]"
    python examples/selenium_recaptcha_demo.py
    python examples/selenium_recaptcha_demo.py --headless
    python examples/selenium_recaptcha_demo.py --proxy host:port

Ключевые моменты, ради которых писан этот пример:

1. User-Agent обязательно «не headless» — иначе детект почти гарантирован.
2. Солверу нужно передать именно WebElement iframe, а не локатор.
3. ``click_recaptcha_v2`` не возвращает токен: его читают из формы отдельно.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recaptcha_toolkit import (
    CaptchaError,
    SeleniumBot,
    SeleniumSolverOptions,
    SeleniumStealthOptions,
)

DEMO_V2_URL = "https://www.google.com/recaptcha/api2/demo"
# Главная Google: виджета нет, только проверка маскировки.
GOOGLE_URL = "https://google.com"


def check_stealth(bot: SeleniumBot) -> None:
    """Проверить, что CDP-хук скрыл признаки автоматизации.

    :param bot: бот с драйвером.
    """
    bot.driver.get(GOOGLE_URL)
    webdriver = bot.driver.execute_script("return navigator.webdriver;")
    languages = bot.driver.execute_script("return navigator.languages;")
    platform = bot.driver.execute_script("return navigator.platform;")
    ua = bot.driver.execute_script("return navigator.userAgent;")

    print("  navigator.webdriver :", webdriver, "(должно быть undefined/None)")
    print("  navigator.languages :", languages)
    print("  navigator.platform  :", platform)
    print("  navigator.userAgent :", str(ua)[:80], "...")
    print("  'Headless' в UA     :", "Headless" in str(ua), "(должно быть False)")


def solve_demo(bot: SeleniumBot) -> None:
    """Найти iframe reCAPTCHA и решить челлендж.

    :param bot: бот с драйвером.
    """
    bot.driver.get(DEMO_V2_URL)

    # Шаг 1: убеждаемся, что виджет вообще есть.
    if not bot.recaptcha_is_present():
        print("  виджет reCAPTCHA не найден на странице", file=sys.stderr)
        return

    # Шаг 2: находим iframe стандартным XPath.
    # Эквивалентно ручному driver.find_element(By.XPATH, '//iframe[@title="reCAPTCHA"]')
    iframe = bot.find_recaptcha_iframe(timeout=10)
    print(f"  iframe найден: {iframe.get_attribute('title')!r}")

    # Шаг 3: кликаем «Я не робот» и решаем аудио-челлендж.
    bot.solve_recaptcha_v2(iframe=iframe)
    print("  челлендж пройден")

    # Шаг 4: забираем токен, который солвер положил в форму.
    token = bot.wait_for_token(timeout=20)
    print(f"  токен получен ({len(token)} симв.): {token[:60]}...")


def main() -> int:
    """Точка входа примера.

    :returns: код возврата процесса.
    """
    parser = argparse.ArgumentParser(description="Selenium + reCAPTCHA")
    parser.add_argument("--headless", action="store_true", help="без GUI (риск детекта)")
    parser.add_argument("--proxy", default=None, help="прокси host:port")
    parser.add_argument("--language", default="en-US", help="язык интерфейса и распознавания")
    parser.add_argument("--stealth-check", action="store_true", help="только проверка маскировки")
    parser.add_argument(
        "--xpath",
        default='//iframe[@title="reCAPTCHA"]',
        help="XPath для поиска iframe виджета",
    )
    args = parser.parse_args()

    # Маскировка: не-headless UA + CDP-хук на navigator.*.
    stealth = SeleniumStealthOptions(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        languages=(args.language, args.language.split("-")[0]),
    )
    solver = SeleniumSolverOptions(
        iframe_xpath=args.xpath,
        iframe_timeout=10,
        service_language=args.language,
        humanize_delays=True,
    )

    try:
        with SeleniumBot.launch_chrome(
            headless=args.headless,
            proxy=args.proxy,
            language=args.language,
            stealth=stealth,
            solver=solver,
        ) as bot:
            if args.stealth_check:
                check_stealth(bot)
            else:
                solve_demo(bot)
    except CaptchaError as exc:
        print(f"капчу решить не удалось: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
