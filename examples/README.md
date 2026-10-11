# Примеры использования recaptcha_toolkit

Запуск — из корня репозитория:

```bash
python -m pip install ".[captcha]"      # Playwright + playwright-recaptcha + playwright-stealth
python -m pip install ".[captcha-selenium]"  # Selenium + selenium-recaptcha-solver
python -m pip install ".[vision]"       # VisionAIRecaptchaSolver-0 (YOLO, тянет ultralytics/torch)
python -m playwright install chromium
```

## Про демонстрационный сайт

В запросе фигурировал `https://google.com`, но **на главной странице Google нет
виджета reCAPTCHA** — там просто нечего решать, и любой солвер упадёт с
`RecaptchaNotFoundError`.

Поэтому в примерах используется официальный демо-стенд Google с настоящим
виджетом:

| URL | Что там |
| --- | --- |
| `https://www.google.com/recaptcha/api2/demo` | референсный виджет reCAPTCHA **v2** (чекбокс + челлендж) |
| `https://www.google.com/recaptcha/api2/demo` + site key `6Le-wvkS...` | тот же ключ подходит для YOLO-солвера |
| `https://antcpt.com/score_detector/` | страница с reCAPTCHA **v3** (используется в README `playwright-recaptcha`) |

`https://google.com` в примерах оставлен только для проверки маскировки:
страница загружается и печатается `navigator.webdriver` — это самый быстрый
способ убедиться, что stealth-скрипты применились.

## Файлы

| Файл | Описание |
| --- | --- |
| `playwright_recaptcha_demo.py` | синхронный Playwright: v2 аудио, v2 картинки (CapSolver), v3, проверка stealth |
| `playwright_recaptcha_async_demo.py` | то же самое на `asyncio` |
| `selenium_recaptcha_demo.py` | Selenium: поиск iframe по XPath и `click_recaptcha_v2` |
| `vision_ai_demo.py` | YOLO-солвер (VisionAIRecaptchaSolver-0) по `website_key` / `website_url` |

## Требования

- **FFmpeg** обязателен для обоих аудио-солверов (`playwright-recaptcha` и
  `selenium-recaptcha-solver`): `apt-get install ffmpeg` / `brew install ffmpeg`.
- Для `SolverBackend.IMAGE` нужен ключ CapSolver: `export CAPSOLVER_API_KEY=...`.
- YOLO-солвер при первом запуске скачивает модели с Hugging Face и сам
  поднимает Chromium — ему нужен отдельный `server_port` на каждый инстанс.
