# arena-api

Неофициальная типизированная Python-библиотека для взаимодействия с **доступными вам** Arena frontend/API: Gradio queue/SSE и экспериментальным Direct Chat stream.

> Arena не предоставляет стабильный публичный API для всех этих маршрутов. Эндпоинты, очереди, порядок callback-аргументов и формат стрима могут измениться без предупреждения. Используйте клиент только в соответствии с правилами сервиса и имеющимися у вас правами доступа.
>
> Библиотека использует переданные вами cookie/заголовки и, для Direct Chat, явно переданную вами verification assertion; по явному запросу (`session_source="firefox"`) она может читать сессию из вашего локального профиля Firefox (см. ниже). Она **не** решает CAPTCHA и не обходит Cloudflare, Turnstile или другие проверки: при challenge (`ChallengeRequiredError`) проверку проходите вы в обычном браузере — с Firefox-сессией клиент дождётся этого, перечитает cookie и повторит запрос.

## Установка

```bash
python -m pip install .
```

Для запуска тестов:

```bash
python -m pip install '.[test]'
pytest
```

Транспорт построен на `httpx` с HTTP/2. TLS fingerprint spoofing, ротации User-Agent и автоматического решения браузерных проверок нет.

## Cookie-сессия

Передавайте cookie только из собственной/разрешённой сессии. Поддерживаются словарь, строка `Cookie` из DevTools, путь к JSON-файлу, стандартный список cookie-объектов и browser-export с полем `cookie`:

```python
from arena_api import ArenaClient

with ArenaClient(cookies="session=value; cf_clearance=value") as arena:
    print(arena.get_models())
```

```python
with ArenaClient(cookies={"session": "value"}) as arena:
    ...

with ArenaClient(cookies="./cookies.json") as arena:
    ...
```

Пример допустимого `cookies.json`:

```json
[
  {"name": "session", "value": "...", "domain": "arena.ai"},
  {"name": "cf_clearance", "value": "...", "domain": ".arena.ai"}
]
```

Поддерживается также объект `{"cookies": [...]}` или экспорт `{"cookie": "a=b; c=d"}`. Атрибуты domain/path из JSON не переносятся в cookie jar `httpx`; клиент передаёт нормализованные имена и значения в запросы к заданному `base_url`. Не помещайте реальные cookie или токены в репозиторий, логи и issue.

По умолчанию `base_url="https://arena.ai"`; можно указать другой разрешённый frontend/backend, например `https://lmarena.ai`. Статические дополнительные заголовки передаются через `headers={...}`. User-Agent по умолчанию идентифицирует библиотеку как `arena-api`.

## Сессия из локального Firefox (`session_source="firefox"`)

Вместо ручного экспорта cookie можно переиспользовать сессию вашего Firefox: клиент читает авторизационные cookie из локального профиля и сам формирует согласованные с браузером заголовки.

```python
from arena_api import ArenaClient

with ArenaClient(session_source="firefox") as arena:
    print(arena.get_models())
```

Как это работает:

- **Извлечение cookie** — через `rookiepy` (`python -m pip install '.[firefox]'`): `rookiepy.firefox(domains=["arena.ai", "lmarena.ai"])` и нормализация записей в плоский `dict[str, str]`, готовый для прямой передачи в `curl_cffi.requests.Session` или `httpx.Client`. Если `rookiepy` не установлен, cookie-база заблокирована запущенным Firefox (SQLite lock) или вернула пустой результат, включается встроенный fallback без внешних зависимостей: снимок `cookies.sqlite` вместе с `-wal`/`-shm` во временную папку и чтение через стандартный `sqlite3` — без борьбы за блокировки с браузером. Профиль по умолчанию определяется по `profiles.ini` (секция `[Install...]` → `Default`, затем профиль с `Default=1`), с fallback'ом на каталог `*.default-release` и, в крайнем случае, на профиль с самым свежим `cookies.sqlite`.
- **Заголовки** — `FirefoxSessionManager.firefox_headers()` строит User-Agent под текущую ОС и версию Firefox из `compatibility.ini` профиля (с замороженным `rv:109.0`, как в реальном Firefox), плюс `Accept-Language` (из профиля/системы или параметр `language`), `Sec-GPC: 1` и `Sec-Fetch-*` (вариант `fetch`/XHR по умолчанию, `navigation=True` для документных запросов) — отпечаток заголовков совпадает с реальным браузером. `Accept-Encoding` не подставляется: его согласует сам HTTP-бэкенд (httpx/curl_cffi) в соответствии с доступными декодерами.
- **403 / `ChallengeRequiredError`** — клиент печатает «Сессия устарела. Откройте arena.ai в Firefox, обновите страницу, затем нажмите Enter...», ждёт Enter, перечитывает cookie через `rookiepy` и прозрачно повторяет запрос (до `max_auth_refreshes=2` попыток по умолчанию, для sync- и async-методов, включая стримы). CAPTCHA и защиты не решаются автоматически — проверку проходите вы в обычном браузере.

Cookie из Firefox перекрывают одноимённые значения из `cookies=`, а явные `headers=` перекрывают заголовки Firefox. Для headless-окружений создайте менеджер с `interactive=False` (без ожидания Enter, cookie перечитываются сразу) и передайте сам объект:

```python
from arena_api import ArenaClient
from arena_api.auth import FirefoxSessionManager

manager = FirefoxSessionManager(interactive=False, language="ru-RU")
with ArenaClient(session_source=manager) as arena:
    ...
```

Менеджер удобно использовать и отдельно, например с `curl_cffi`:

```python
from arena_api.auth import FirefoxSessionManager

manager = FirefoxSessionManager(domains=["arena.ai", "lmarena.ai"])
cookies = manager.load_cookies()      # {"session": "...", "cf_clearance": "..."}
headers = manager.firefox_headers()   # User-Agent, Sec-GPC, Sec-Fetch-*, ...

from curl_cffi import requests

session = requests.Session(impersonate="firefox", cookies=cookies, headers=headers)
# либо домено-зависимый экспорт:
for domain, jar in manager.to_curl_cffi().items():
    for name, value in jar.items():
        session.cookies.set(name, value, domain=domain)
```

Тонкости: имена cookie от более ранних доменов в `domains` имеют приоритет при коллизиях; сессия из Firefox не «чинит» просроченный на сервере логин — перезайдите в браузере; полный листинг cookie и записей с доменами доступен через `manager.cookie_records()` / `manager.to_curl_cffi()`.

## E2E-инфраструктура `arena_api.auth` (Playwright)

Модуль для синтетического мониторинга и интеграционных тестов **собственных или явно разрешённых** деплоев. Дополнительные зависимости:

```bash
python -m pip install '.[e2e]'
playwright install chromium
```

| Модуль | Назначение |
|---|---|
| `environment.py` | init-скрипт детерминизма: заморозка анимаций, seeded `Math.random`, привязка `Date`, тестовые хуки |
| `browser.py` | `BrowserEnvironment`: воспроизводимые контексты Playwright, трассировки, HAR, скриншоты, артефакты |
| `kinematics.py` | кинематика курсора: кубические кривые Безье, время по Фиттсу, ease-in-out, микро-тремор, overshoot |
| `challenge.py` | детектирование верификационных экранов и сбор диагностики |
| `session.py` | `BrowserSessionManager`: валидация через `curl_cffi`, экспорт `cookies.json`, обновление через реальный логин |

### Валидация и обновление сессии

```python
from arena_api.auth import BrowserSessionManager, FormLoginFlow

manager = BrowserSessionManager(
    base_url="https://example.com",
    storage_path="./state/cookies.json",
    validate_paths=("/api/me",),
    login_flow=FormLoginFlow(
        login_url="https://example.com/login",
        username_selector="input[name=email]",
        password_selector="input[name=password]",
        submit_selector="button[type=submit]",
        success_url_contains="/app",
    ),
)

result = manager.validate()
print(result.describe())   # ok [200] https://example.com/api/me: session accepted

if not result.ok:
    manager.refresh_session()      # реальный логин в Playwright, без обхода проверок

session = manager.to_curl_cffi_session()
cookies = manager.load_cookies()   # {"session": "...", ...}
```

`storage_path` понимает все три формата файла cookie: `storage_state` от Playwright, массив записей `{"name", "value", "domain"}` (тот, что выше в разделе «Cookie-сессия») и браузерный экспорт `{"cookie": "a=b; c=d"}`. У записей без `domain` он подставляется из `base_url`; порт из хоста отбрасывается, потому что домен cookie его не содержит.

Быстрый путь (`validate()`) — это один `curl_cffi`-запрос: поднимать Chromium, чтобы узнать «мы ещё залогинены?», не нужно. Медленный путь (`refresh_session()`) запускает браузер и выполняет **обычный вход с вашими учётными данными**; результат сохраняется в `cookies.json` (формат `storage_state`) и экспортируется в `curl_cffi.requests.Session`.

### Воспроизводимый браузер

```python
import asyncio
from arena_api.auth import (
    BrowserEnvironment, BrowserEnvironmentOptions, DeterminismOptions,
)

async def main():
    options = BrowserEnvironmentOptions(
        locale="en-US",
        timezone_id="UTC",
        viewport={"width": 1440, "height": 900},
        reduced_motion="reduce",
        artifacts_dir="./artifacts",
        record_trace=True,
        determinism=DeterminismOptions(random_seed=7),
        block_resource_types=("media", "font"),
    )
    async with BrowserEnvironment(options) as env:
        page = await env.new_page()
        await page.goto("https://example.com")
        await page.evaluate("() => window.__arenaTestHooks.drain(3)")

        mouse = env.mouse(page, seed=7)
        await mouse.hover("nav a[href='/pricing']")
        await mouse.click("a.cta")

        await env.screenshot(page, "landing.png")
        await env.save_storage_state(page.context, "state.json")

asyncio.run(main())
```

### Кинематика курсора

```python
from arena_api.auth import TrajectoryPlanner

planner = TrajectoryPlanner(seed=42)
trajectory = planner.plan((0, 0), (640, 480))
len(trajectory), trajectory.duration, trajectory.straightness
```

Многие UI-компоненты зависят от **движения**, а не только от финальной точки: hover-меню с гистерезисом, тултипы с debounce на `pointerenter`, drag-handles, слайдеры, канвас. Один `mouse.move(x, y)` пропускает все промежуточные события и даёт и ложные срабатывания, и пропущенные баги. Траектория строится по кубической кривой Безье, длительность — по закону Фиттса, скорость — ease-in-out с микро-тремором, изредка — небольшой overshoot с возвратом. При заданном `seed` траектория воспроизводится бит-в-бит, поэтому упавший hover-тест можно отладить, а не «поймать заново».

### Детектирование challenge

```python
from arena_api import ChallengeRequiredError
from arena_api.auth import ChallengeHandler, inspect_response

handler = ChallengeHandler(capture_dir="./artifacts/challenges", raise_on_detection=True)
try:
    await handler.check(page)
except ChallengeRequiredError as exc:
    print(exc)      # что обнаружено, где, и как это исправить
```

`inspect_response(status, headers, body)` — тот же детектор для быстрого HTTP-пути, без браузера. Статус-код сам по себе не считается challenge: `403` — это ещё и обычная ошибка авторизации, поэтому требуются подтверждающие маркеры (`cf-mitigated`, текст интерстишла, ссылка на `challenges.cloudflare.com`, challenge-фрейм). При обнаружении сохраняются скриншот, HTML и JSON с сигналами.

### Что этот модуль сознательно не делает

- **Не решает и не обходит** CAPTCHA, Turnstile и Managed Challenge: нет поиска и клика по чекбоксу, нет обхода через аудиоканал и STT-распознавания токена, нет автоматического получения или обновления `cf_clearance`.
- **Не маскирует автоматизацию**: нет правок `navigator.webdriver`, `navigator.plugins`, WebGL-вендора/рендерера, Canvas-шума, `navigator.permissions.query` и объекта `window.chrome`.

Причины две. Первая — это обход защиты, которую владелец сервиса включил сознательно; позиция библиотеки та же, что в `ChallengeRequiredError`. Вторая — практическая: тихо «пройденный» challenge превращает громкую и понятную ошибку сборки в тихую и плавающую, после чего мониторинг перестаёт что-либо измерять. Синтетический мониторинг полезен ровно тогда, когда он **виден**: при `identify_automation=True` страница получает `window.__arenaSynthetic`, а запросы — заголовок `X-Synthetic-Monitor`, по которому владелец сервиса может сделать исключение.

Что делать вместо обхода:

1. Добавьте исключение в WAF/bot-management для egress-IP или ASN вашего CI — либо матчинг по заголовку `X-Synthetic-Monitor`.
2. В CI используйте официальные тестовые sitekey/secret Cloudflare Turnstile: они всегда возвращают проходящий токен, и пайплайн прогоняет реальный путь виджета.
3. На собственных staging-зонах отключите Bot Fight Mode и managed challenge.
4. Для machine-to-machine мониторинга используйте API-токен или сервисный аккаунт вместо чужой браузерной сессии.

Поддерживаемые Playwright-опции покрывают легитимную часть «нормализации окружения» без патчей: `locale` (он же `navigator.languages`), `timezone_id`, `user_agent`, `viewport`, `device_scale_factor`, `color_scheme`, `reduced_motion`.

## `recaptcha_toolkit` — решение reCAPTCHA и маскировка автоматизации

Отдельный пакет `recaptcha_toolkit` собирает под одним типизированным API четыре библиотеки:

| Библиотека | Роль |
| --- | --- |
| [`playwright-recaptcha`](https://github.com/Xewdy444/Playwright-reCAPTCHA) | v2 через аудио-челлендж (Google Speech API) или картинки (CapSolver), перехват токена для v3 |
| [`VisionAIRecaptchaSolver-0`](https://github.com/Altum-x/VisionAIRecaptchaSolver-0) | локальное решение v2/v3 на YOLO-моделях, без внешних API |
| [`selenium-recaptcha-solver`](https://pypi.org/project/selenium-recaptcha-solver/) | решение аудио-челленджа v2 для Selenium |
| [`playwright-stealth`](https://pypi.org/project/playwright-stealth/) | маскировка `navigator.webdriver`, языков, платформы и пр. |

> **Область применения.** Пакет предназначен для автоматизированного тестирования страниц, на которые у вас есть явное разрешение: собственные стенды, staging-зоны, страницы клиентов, публичные демо-стенды вендора. Он **не** подключён к `arena_api` и не участвует ни в одном сценарии этого клиента — раздел «Что этот модуль сознательно не делает» выше остаётся в силе: позиция проекта по обходу защит не изменилась, подключение `recaptcha_toolkit` к чему-либо — полностью осознанное решение вызывающего кода. Использование против сервисов, чьи правила это запрещают, нарушает их условия.

### Установка

```bash
python -m pip install ".[captcha]"            # Playwright + playwright-recaptcha + playwright-stealth
python -m pip install ".[captcha-selenium]"   # Selenium + selenium-recaptcha-solver
python -m pip install ".[vision]"             # YOLO-солвер (тянет ultralytics/torch)
python -m playwright install chromium
```

Оба аудио-солвера требуют системный **FFmpeg** (`apt-get install ffmpeg` / `brew install ffmpeg`) — `pydub` конвертирует MP3 в WAV перед распознаванием.

### Playwright, синхронно

```python
from recaptcha_toolkit import PlaywrightBot, SolverBackend

with PlaywrightBot(backend=SolverBackend.AUDIO) as bot:
    page = bot.new_page()
    page.goto("https://www.google.com/recaptcha/api2/demo")

    token = bot.solve_recaptcha_v2(page, wait=True, wait_timeout=30)
    print(token)
```

reCAPTCHA v3 — браузер решает её сам, библиотека только перехватывает токен. Слушатель надо подключить **до** запроса `/recaptcha/api2/reload`, поэтому страница перезагружается внутри солвера:

```python
token = bot.solve_recaptcha_v3(page, timeout=30, reload=True)
```

### Playwright, асинхронно

API зеркальный, конфигурация та же:

```python
import asyncio
from recaptcha_toolkit import AsyncPlaywrightBot

async def main() -> None:
    async with AsyncPlaywrightBot() as bot:
        page = await bot.new_page()
        await page.goto("https://www.google.com/recaptcha/api2/demo")
        print(await bot.solve_recaptcha_v2(page))

asyncio.run(main())
```

### Selenium

```python
from recaptcha_toolkit import SeleniumBot

with SeleniumBot.launch_chrome() as bot:
    bot.driver.get("https://www.google.com/recaptcha/api2/demo")

    iframe = bot.find_recaptcha_iframe(timeout=10)   # XPath //iframe[@title="reCAPTCHA"]
    bot.solve_recaptcha_v2(iframe=iframe)            # внутри: solver.click_recaptcha_v2(iframe=...)

    print(bot.get_token())
```

User-Agent по умолчанию — «не headless»: любой UA с `Headless` резко повышает риск детекта (это прямо указано в документации `selenium-recaptcha-solver`). Маскировка накатывается через CDP-хук `Page.addScriptToEvaluateOnNewDocument` до первой навигации.

Чуть короче — сразу токен:

```python
token = bot.solve_and_get_token(token_timeout=20)
```

### Переключение на YOLO (`VisionAIRecaptchaSolver-0`)

Флагом, без изменений в коде бота:

```python
from recaptcha_toolkit import PlaywrightBot, SolverBackend, VisionAIOptions

with PlaywrightBot(
    backend=SolverBackend.VISION_AI,
    vision_ai=VisionAIOptions(timeout=180, server_port=8443),
) as bot:
    page = bot.new_page()
    page.goto("https://www.google.com/recaptcha/api2/demo")
    # website_key и website_url определяются сами; можно передать явно:
    token = bot.solve_recaptcha_v2(page, website_key="6Le-...", website_url=page.url)
```

YOLO-солвер работает **не** в вашей странице: он поднимает свой Chromium и реплицирует виджет, поэтому ему нужны `website_key` / `website_url`. Прямой вызов, без Playwright вообще:

```python
from recaptcha_toolkit import VisionAISolver

token = VisionAISolver().solve(
    website_key="6Le-wvkSVVABCPBMRTvw0Q4Muexq1bi0DJwx_mJ-",
    website_url="https://www.google.com/recaptcha/api2/demo",
)
```

Конкурентным инстансам нужны разные `server_port` и `download_dir`.

### Обработка ошибок

Всё наследуется от `CaptchaError`, частые случаи — отдельными классами:

```python
from recaptcha_toolkit import CaptchaError, RecaptchaNotFoundError, RecaptchaTimeoutError

try:
    token = bot.solve_recaptcha_v2(page)
except RecaptchaNotFoundError:
    ...   # iframe/виджет не найден на странице
except RecaptchaTimeoutError:
    ...   # не уложились в wait_timeout / timeout
except CaptchaError as exc:
    print("капчу решить не удалось:", exc)
```

Полный набор: `RecaptchaNotFoundError`, `RecaptchaTimeoutError`, `RecaptchaRateLimitError` («Try again later» — лимит на IP), `RecaptchaSolveError`, `SiteKeyDetectionError`, `SolverDependencyError` (не установлен пакет).

### Известные грабли

- **Конфликт пинов `SpeechRecognition`.** `playwright-recaptcha` требует `SpeechRecognition==3.10.4`, а `selenium-recaptcha-solver` — `~=3.8.1`. Оба используют один и тот же API (`Recognizer`, `AudioFile`, `recognize_google`, `UnknownValueError`), поэтому на практике работают с любой из двух версий, но `pip` будет ругаться. Либо ставьте ветки в разные venv, либо зафиксируйте одну версию и примите предупреждение резолвера.
- **`https://google.com` как демо не подходит** — на главной странице Google нет виджета, солвер закономерно упадёт с `RecaptchaNotFoundError`. Используйте `https://www.google.com/recaptcha/api2/demo` (v2) и `https://antcpt.com/score_detector/` (v3).
- **FFmpeg** отсутствующий в `PATH` даёт `RuntimeWarning` от `pydub` и молча неработающий аудиоканал.

Готовые сценарии — в [`examples/`](examples/) (`--help` у каждого файла), тесты — `tests/test_recaptcha_toolkit.py`.

## Gradio очередь и SSE

```python
from arena_api import ArenaClient, GenerationConfig

with ArenaClient(cookies="./cookies.json") as arena:
    models = arena.get_models()  # model dropdown из /config; может быть пустым
    print(arena.get_endpoint("chat"))

    answer = arena.complete(
        "Привет!",
        model=models[0].id if models else None,
        config=GenerationConfig(temperature=0.2, queue_timeout=120),
    )
    print(answer)

    for chunk in arena.stream("Расскажи кратко о SSE."):
        print(chunk.delta, end="", flush=True)
    print()

    chat = arena.chat(model=models[0].id if models else None)
    print(chat.send_message("Меня зовут Ada."))
    print(chat.send_message("Как меня зовут?"))
    print(chat.turn_id, chat.parent_message_id, chat.history)
```

`StreamChunk.text` — накопленный текст ответа, `delta` — новый фрагмент, `finished=True` отмечает терминальное событие. Встроенный анализатор читает `/config`, ищет callbacks и варианты моделей, но Gradio-схема зависит от конкретного развёртывания.

Если порядок входов callback отличается от стандартного, передайте `chat_fn_index` и `chat_payload_factory`:

```python
from arena_api import ArenaClient, GenerationConfig, Message


def payload(prompt: str, model: str | None, config: GenerationConfig,
            history: tuple[Message, ...]) -> list[object]:
    # Составьте список в порядке входных компонентов конкретного callback.
    return [prompt, [{"role": m.role, "content": m.content} for m in history], model]

with ArenaClient(chat_fn_index=12, chat_payload_factory=payload) as arena:
    ...
```

Можно изучить конфигурацию через `arena.config()` и получить сырые queue events через `arena.stream_queue(data, fn_index=...)`. Индексы и формы данных не являются гарантированным контрактом.

Для Arena side-by-side callback доступны `stream_battle()` и `battle()`; у async-клиента — `astream_battle()` и `abattle()`. Они требуют обнаруживаемый `battle` callback или явный `battle_fn_index` и возвращают оба ответа.

## Async API

Все async-методы имеют явный префикс `a` там, где это предотвращает путаницу со sync API:

```python
import asyncio
from arena_api import AsyncArenaClient


async def main() -> None:
    async with AsyncArenaClient(cookies="./cookies.json") as arena:
        print(await arena.get_models())
        async for chunk in arena.astream("Напиши одно приветствие."):
            print(chunk.delta, end="", flush=True)
        print()
        chat = arena.chat()
        print(await chat.send_message("Привет"))


asyncio.run(main())
```

## Direct Chat

Direct Chat использует другой, недокументированный Next.js stream-протокол. Его можно выбрать отдельно:

```python
from arena_api import DirectArenaClient

# Получите свежую assertion штатным способом в своей сессии.
verification_token = "<переданный вызывающим кодом токен>"

with DirectArenaClient(
    cookies="./cookies.json",
    verification_token=verification_token,
) as arena:
    models = arena.get_models()
    model = models[0].display_name
    for chunk in arena.stream("Привет!", model=model):
        print(chunk.delta, end="", flush=True)
```

Можно передать `verification_token` непосредственно в `stream()`/`complete()` или callback `verification_token=lambda: ...`, если ваше приложение само получает новое значение легальным способом. Клиент его не извлекает и не обновляет самостоятельно. `DirectChatSession` хранит локальную историю и отдельный conversation UUID для каждого объекта чата. Есть async-вариант `AsyncDirectArenaClient` / `AsyncDirectChatSession`.

## Ошибки

Все публичные ошибки наследуются от `ArenaError`:

- `AuthenticationError` — cookie/session отклонена;
- `ChallengeRequiredError` — нужна интерактивная браузерная проверка;
- `RateLimitError` — лимит запросов (при наличии `retry_after` доступен в секундах);
- `QueueTimeoutError` — очередь/стрим не завершился в отведённое время;
- `ModelUnavailableError` — модель или callback недоступны;
- `ProtocolError` — ответ не соответствует ожидаемому формату.

## Примеры и тесты

- `example.py` — Gradio queue/SSE;
- `example_direct.py` — Direct Chat с assertion, переданной пользователем;
- `example_firefox.py` — сессия из локального профиля Firefox;
- `example_e2e.py` — синтетический мониторинг на `arena_api.auth`: быстрая валидация через `curl_cffi` (`--check`, без браузера), обновление через реальный логин, детерминированный контекст, кинематический ввод, детектор challenge;
- `tests/test_auth_integration.py` — интеграционные тесты: реальный локальный HTTP-сервер плюс настоящий `curl_cffi` (валидация, редиректы, 401/429, challenge) и сверка генерируемых kwargs с реальными сигнатурами `playwright.async_api`;
- остальные `tests/` — офлайн unit-тесты на парсинг cookie/SSE, mapping моделей, очереди, async API, ошибки, Firefox-сессию, retry при 403, кинематику курсора, init-скрипт детерминизма, детектор challenge и жизненный цикл сессии.
