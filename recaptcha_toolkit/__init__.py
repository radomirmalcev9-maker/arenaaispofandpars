"""Инструментарий для обхода детекта автоматизации и решения reCAPTCHA v2/v3.

Пакет объединяет четыре независимых библиотеки под одним типизированным API:

* `playwright-recaptcha <https://github.com/Xewdy444/Playwright-reCAPTCHA>`_
  — решение v2 через аудио-челлендж (распознавание речи Google) или через
  image-челлендж (внешний CapSolver API), перехват токена для v3;
* `VisionAIRecaptchaSolver-0 <https://github.com/Altum-x/VisionAIRecaptchaSolver-0>`_
  — локальное решение v2/v3 на базе YOLO-моделей (без внешних API);
* `selenium-recaptcha-solver <https://pypi.org/project/selenium-recaptcha-solver/>`_
  — решение аудио-челленджа v2 для Selenium;
* `playwright-stealth <https://pypi.org/project/playwright-stealth/>`_
  — маскировка признаков автоматизации в Playwright.

Быстрый старт (Playwright, синхронно)::

    from recaptcha_toolkit import PlaywrightBot

    with PlaywrightBot() as bot:
        page = bot.new_page()
        page.goto("https://www.google.com/recaptcha/api2/demo")
        token = bot.solve_recaptcha_v2(page)

Быстрый старт (Selenium)::

    from recaptcha_toolkit import SeleniumBot

    with SeleniumBot.launch_chrome() as bot:
        bot.driver.get("https://www.google.com/recaptcha/api2/demo")
        bot.solve_recaptcha_v2()
        print(bot.get_token())

.. warning::
   Назначение пакета — автоматизированное тестирование (E2E, QA, регрессы)
   веб-страниц, на которые у вас есть явное разрешение: собственные стенды,
   страницы ваших клиентов, публичные демо-стенды вендора. Применение к
   сервисам, чьи правила это запрещают, нарушает их условия использования и,
   в зависимости от юрисдикции, applicable law. Ответственность за выбор цели
   несёт вызывающая сторона.

Пакет намеренно **не** импортируется из :mod:`arena_api` и не подключается к
его клиентам: подключение — явное и осознанное со стороны вызывающего кода.
"""

from __future__ import annotations

from .config import (
    BotConfig,
    BrowserOptions,
    PlaywrightSolverOptions,
    RecaptchaVersion,
    SeleniumSolverOptions,
    SeleniumStealthOptions,
    SolverBackend,
    StealthOptions,
    VisionAIOptions,
)
from .errors import (
    CaptchaError,
    RecaptchaNotFoundError,
    RecaptchaRateLimitError,
    RecaptchaSolveError,
    RecaptchaTimeoutError,
    SiteKeyDetectionError,
    SolverDependencyError,
)
from .playwright_bot import AsyncPlaywrightBot, PlaywrightBot
from .selenium_bot import SeleniumBot
from .vision_ai import VisionAISolver

__all__ = [
    "AsyncPlaywrightBot",
    "BotConfig",
    "BrowserOptions",
    "CaptchaError",
    "PlaywrightBot",
    "PlaywrightSolverOptions",
    "RecaptchaNotFoundError",
    "RecaptchaRateLimitError",
    "RecaptchaSolveError",
    "RecaptchaTimeoutError",
    "RecaptchaVersion",
    "SeleniumBot",
    "SeleniumSolverOptions",
    "SeleniumStealthOptions",
    "SiteKeyDetectionError",
    "SolverBackend",
    "SolverDependencyError",
    "StealthOptions",
    "VisionAIOptions",
    "VisionAISolver",
]

__version__ = "0.1.0"
