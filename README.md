# arena-api

Неофициальная типизированная Python-библиотека для взаимодействия с **доступными вам** Arena frontend/API: Gradio queue/SSE и экспериментальным Direct Chat stream.

> Arena не предоставляет стабильный публичный API для всех этих маршрутов. Эндпоинты, очереди, порядок callback-аргументов и формат стрима могут измениться без предупреждения. Используйте клиент только в соответствии с правилами сервиса и имеющимися у вас правами доступа.
>
> Библиотека использует переданные вами cookie/заголовки и, для Direct Chat, явно переданную вами verification assertion. `auto_client()` и `session_source="firefox"` читают сессию из **вашего собственного** локального профиля браузера — ничего не отправляется наружу, кроме обычных запросов к выбранному `base_url`. При challenge (`ChallengeRequiredError`) клиент сообщает об этом — с браузерной сессией он дождётся проверки, перечитает cookie и повторит запрос.

## Установка

```bash
python -m pip install .
```

Для запуска тестов:

```bash
python -m pip install '.[test]'
pytest
```

Транспорт построен на `httpx` с HTTP/2.

## Ноль настроек: от вас нужен только вход в аккаунт

Единственный ручной шаг — войти в свой аккаунт в своём браузере. Всё остальное
`arena_api/auto.py` делает сам: находит сессию, подставляет заголовки того же
браузера, проверяет её одним запросом, а при отказе — обновляет.

```bash
python -m pip install '.[auto]'           # всё сразу: профили браузеров + солвер
playwright install chromium               # нужен только для решения капчи
```

То же самое по частям (каждый шаг необязателен):

```bash
python -m pip install '.[firefox]'        # чтение профилей Chrome/Edge/Brave/...
python -m pip install '.[auth-captcha]'   # автоматическое решение reCAPTCHA
python -m pip install '.[e2e]'            # быстрая валидация сессии через curl_cffi
```

```python
from arena_api import auto_client

with auto_client() as arena:          # ни одного аргумента
    print(arena.get_models())
```

Если сессия не найдена, библиотека напишет, где именно она искала, и подождёт
Enter: откройте сайт в браузере, войдите, вернитесь в терминал — дальше всё само.

### Где ищется сессия (по порядку, берётся первый найденный)

1. `cookies=` — если вы всё-таки передали экспорт вручную;
2. `ARENA_COOKIE` (строка `Cookie` из DevTools) или `ARENA_COOKIES_FILE` (путь к JSON);
3. `state/cookies.json` — если такой файл уже есть (его сохранили вы или прошлый
   запуск с `storage_path=`); сам bootstrap cookie на диск не пишет;
4. ваш профиль **Firefox** (`cookies.sqlite` читается даже залоченным, `rookiepy` не нужен);
5. остальные браузеры через `rookiepy`: Chrome, Chromium, Edge, Brave, Vivaldi, Opera.

Заголовки запросов соответствуют тому браузеру, откуда взялись cookie: сессия из
Firefox представляется Firefox, после обновления в нашем Chromium — Chromium.

### Что происходит, когда сессию отвергли

Порядок («лестница») — от самого дешёвого способа к самому тяжёлому:

1. **перечитать источник**: для браузерного профиля это «обновите страницу и
   нажмите Enter» — свой браузер не запускается;
2. **настоящий вход по форме**: задаётся переменными окружения
   `ARENA_LOGIN_URL`, `ARENA_USERNAME`, `ARENA_PASSWORD`, `ARENA_USER_SELECTOR`,
   `ARENA_PASS_SELECTOR`, `ARENA_SUBMIT_SELECTOR`, `ARENA_SUCCESS_URL`
   (неполный набор игнорируется — ничего не угадывается);
3. **снять капчу в браузере**: если установлены зависимости солвера,
   `solve_captchas="auto"` включает его сам; stealth применяется всегда, второй
   браузер не запускается — используется страница самого прогона.

Обычно до 2–3-й ступени дело не доходит: транспорт сам запрашивает свежие cookie
и повторяет запрос, так что `ChallengeRequiredError` до вас не доходит.

### Полезные переключатели

Все поля `AutoOptions` необязательны; чаще всего хватает одного-двух:

```python
from arena_api import AutoOptions, auto_client

client = auto_client(
    AutoOptions(
        base_url="https://lmarena.ai",  # другой доступный вам frontend
        solve_captchas="auto",          # auto | True | False
        captcha_backend="audio",        # audio | image | vision_ai
        interactive=False,              # не ждать Enter (для cron/CI)
        verify=True,                    # один проверочный запрос при старте
    )
)
```

| Поле | По умолчанию | Зачем |
|---|---|---|
| `cookies` | `None` | явный экспорт — переопределяет всё остальное |
| `storage_path` | `state/cookies.json`, если существует | куда сохранять и откуда читать сессию |
| `use_browser_profiles` | `True` | читать профили браузеров |
| `profiles_root`, `profile_name`, `cookie_database` | `None` | нестандартное расположение Firefox |
| `interactive`, `prompt` | `True`, `input` | ждать ли входа и как именно спрашивать |
| `login_flow`, `login_from_env` | `None`, `True` | свой сценарий входа вместо переменных окружения |
| `solve_captchas`, `captcha_backend`, `captcha_solver` | `"auto"`, `"audio"`, `None` | решение reCAPTCHA |
| `probe_path`, `verify` | `/config`, `True` | каким запросом проверять сессию |
| `max_auth_refreshes` | `2` | сколько раз повторить запрос после обновления |

### То же самое из командной строки

```bash
python -m arena_api --explain    # где искалась сессия, есть ли логин и солвер
python -m arena_api --status     # найденная сессия + один проверочный запрос
python -m arena_api --models     # список моделей
python -m arena_api --prompt "Привет"
```

Коды возврата: `0` — всё работает, `1` — сессию найти не удалось, `2` — сессия
найдена, но сервер её отверг.

После `pip install .` те же флаги доступны как консольная команда `arena-api`
(`[project.scripts]` в `pyproject.toml`): `arena-api --status`.

### Direct Chat

```python
from arena_api import auto_direct_client

# assertion по-прежнему получаете вы сами — direct.py ничего не решает за вас
client = auto_direct_client(verification_token="03YOUR_ASSERTION")

# opt-in: перехватить свежую v3-assertion солвером на странице Direct Chat
client = auto_direct_client(verification_token="auto")
```

`auto_direct_client()` принимает ту же сессию, что и `auto_client()`, поэтому
отклонённый cookie обновляется и запрос повторяется автоматически.

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
- **403 / `ChallengeRequiredError`** — клиент печатает «Сессия устарела. Откройте arena.ai в Firefox, обновите страницу, затем нажмите Enter...», ждёт Enter, перечитывает cookie через `rookiepy` и прозрачно повторяет запрос (до `max_auth_refreshes=2` попыток по умолчанию, для sync- и async-методов, включая стримы).

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
| `challenge.py` | детектирование верификационных экранов и сбор диагностики; opt-in решение через солвер |
| `captcha.py` | мост к `recaptcha_toolkit`: решение reCAPTCHA v2/v3 на уже открытой странице (только по явному запросу) |
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

### Если синтетические проверки блокируются

1. Добавьте исключение в WAF/bot-management для egress-IP или ASN вашего CI — либо матчинг по заголовку `X-Synthetic-Monitor`.
2. В CI используйте официальные тестовые sitekey/secret Cloudflare Turnstile: они всегда возвращают проходящий токен, и пайплайн прогоняет реальный путь виджета.
3. На собственных staging-зонах отключите Bot Fight Mode и managed challenge.
4. Для machine-to-machine мониторинга используйте API-токен или сервисный аккаунт вместо чужой браузерной сессии.

При `identify_automation=True` страница получает `window.__arenaSynthetic`, а запросы — заголовок `X-Synthetic-Monitor`, по которому владелец сервиса может сделать исключение.

Поддерживаемые Playwright-опции нормализации окружения: `locale` (он же `navigator.languages`), `timezone_id`, `user_agent`, `viewport`, `device_scale_factor`, `color_scheme`, `reduced_motion`.

### Решение reCAPTCHA во время прогона (opt-in)

Всё вышесказанное — про **детектирование**: по умолчанию `arena_api.auth` находит верификационный экран, сохраняет диагностику и сообщает о нём. Если вам нужно именно *решить* reCAPTCHA (на своём стенде или там, где у вас есть на это право), подключите мост к `recaptcha_toolkit`:

```bash
python -m pip install '.[auth-captcha]'      # audio/image + playwright-stealth
python -m pip install '.[e2e,vision]'        # либо локальный YOLO-бэкенд
python -m playwright install chromium
```

```python
from arena_api.auth import BrowserEnvironment, BrowserEnvironmentOptions, RecaptchaToolkitSolver

solver = RecaptchaToolkitSolver(backend="audio")   # "audio" | "image" | "vision_ai"
options = BrowserEnvironmentOptions.with_captcha_solving(solver, artifacts_dir="./artifacts")

async with BrowserEnvironment(options) as env:
    page = await env.new_page()          # хендлер решения ставится на каждую страницу
    await page.goto("https://your-staging.example/login")
    # капча решается сама по мере появления; явно — так:
    observation = await env.solve_challenge(page)
    print(observation.kind)              # none, если экран успешно снят
```

Три точки подключения, все opt-in:

| Куда передать солвер | Что происходит |
|---|---|
| `BrowserEnvironmentOptions(solve_challenges=True, captcha_solver=...)` | хендлер ставится на каждую новую страницу и решает капчу по мере появления |
| `ChallengeHandler(captcha_solver=..., solve_on_detection=True)` | решает в `check()` и в слушателе `install()`; подходит для точечного контроля |
| `BrowserSessionManager(captcha_solver=...)` | решает капчу во время `refresh_session()`; без `login_flow` это «challenge-only» обновление: открыть `captcha_url`, снять экран, сохранить cookie |

Как это устроено и где границы:

- **Второго браузера нет.** Боты `recaptcha_toolkit` используются как фасад солвера: `solve_recaptcha_v2/v3` принимают внешнюю `page` и не трогают собственный контекст бота, поэтому бот создаётся, но **не** запускается. Исключение — `vision_ai`: YOLO-солвер реплицирует виджет в своём Chromium, поэтому ему передаются `website_key`/`website_url` (определяются со страницы) и, по умолчанию, cookie вашей страницы.
- **v2 или v3 определяется по странице**: видимый чекбокс → аудио/картиночный челлендж, невидимый виджет → перехват токена с перезагрузкой страницы (слушатель должен встать до запроса `/recaptcha/api2/reload`). Можно зафиксировать вручную: `RecaptchaToolkitSolver(version="v3")`.
- **Решается только reCAPTCHA.** Turnstile, hCaptcha и managed-челлендж Cloudflare токена не дают — они остаются на пути «обнаружить и сообщить», даже если солвер подключён (`SOLVABLE_CHALLENGE_KINDS`). Смешанные случаи (reCAPTCHA внутри Cloudflare-интерстишла) по умолчанию тоже не решаются: снятие виджета не снимет интерстишел. Флаг `allow_mixed_signals=True` снимает это ограничение.
- **Маскировка применяется при решении** (`apply_stealth=True` по умолчанию): решать капчу, одновременно объявляя `navigator.webdriver` и `X-Synthetic-Monitor`, бессмысленно. `BrowserEnvironmentOptions.with_captcha_solving()` поэтому снимает оба объявления; на собственном стенде, где монитор разрешён по заголовку, верните их: `with_captcha_solving(solver, identify_automation=True)`.
- **Циклы исключены.** Перезагрузка страницы снова будит слушатель, поэтому на страницу выделен бюджет `max_solve_attempts` (по умолчанию 1), параллельные решения по одной странице сериализуются, а неудача поднимает `CaptchaSolveError` (подкласс `ChallengeRequiredError`, так что существующие `except` продолжают работать) с перечислением всех попыток и их причин.
- Быстрый HTTP-путь (`validate()`, `inspect_response()`) страницы не имеет, поэтому всегда остаётся наблюдательным.

## `recaptcha_toolkit` — решение reCAPTCHA и маскировка автоматизации

Отдельный пакет `recaptcha_toolkit` собирает под одним типизированным API четыре библиотеки:

| Библиотека | Роль |
| --- | --- |
| [`playwright-recaptcha`](https://github.com/Xewdy444/Playwright-reCAPTCHA) | v2 через аудио-челлендж (Google Speech API) или картинки (CapSolver), перехват токена для v3 |
| [`VisionAIRecaptchaSolver-0`](https://github.com/Altum-x/VisionAIRecaptchaSolver-0) | локальное решение v2/v3 на YOLO-моделях, без внешних API |
| [`selenium-recaptcha-solver`](https://pypi.org/project/selenium-recaptcha-solver/) | решение аудио-челленджа v2 для Selenium |
| [`playwright-stealth`](https://pypi.org/project/playwright-stealth/) | маскировка `navigator.webdriver`, языков, платформы и пр. |

> Пакет автономен: по умолчанию он не импортируется из `arena_api` и не участвует ни в одном сценарии этого клиента. Единственная связь — opt-in мост `arena_api.auth.captcha` (см. «Решение reCAPTCHA во время прогона» выше): он импортируется лениво и ничего не делает, пока вы не передадите солвер в хендлер, окружение или менеджер сессии.

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
- `CaptchaSolveError` (подкласс `ChallengeRequiredError`) — подключённый солвер не снял капчу; в сообщении — все попытки и их причины;
- `RateLimitError` — лимит запросов (при наличии `retry_after` доступен в секундах);
- `QueueTimeoutError` — очередь/стрим не завершился в отведённое время;
- `ModelUnavailableError` — модель или callback недоступны;
- `ProtocolError` — ответ не соответствует ожидаемому формату.

## Примеры и тесты

- `example.py` — Gradio queue/SSE;
- `example_auto_login.py` — ноль настроек: `auto_client()` сам находит сессию, проверяет и обновляет её;
- `example_direct.py` — Direct Chat с assertion, переданной пользователем;
- `example_firefox.py` — сессия из локального профиля Firefox;
- `example_e2e.py` — синтетический мониторинг на `arena_api.auth`: быстрая валидация через `curl_cffi` (`--check`, без браузера), обновление через реальный логин, детерминированный контекст, кинематический ввод, детектор challenge;
- `example_auth_captcha.py` — opt-in решение reCAPTCHA в E2E-прогоне: `with_captcha_solving`, challenge-only обновление сессии и явный `solve_challenge()`;
- `tests/test_auth_integration.py` — интеграционные тесты: реальный локальный HTTP-сервер плюс настоящий `curl_cffi` (валидация, редиректы, 401/429, challenge) и сверка генерируемых kwargs с реальными сигнатурами `playwright.async_api`;
- `tests/test_auto.py` — офлайн-тесты `arena_api/auto.py`: порядок источников сессии, классификация проверочного запроса, автообновление отвергнутой сессии, «лестница» обновления, Direct Chat и CLI;
- `tests/test_auth_captcha.py` — офлайн-тесты моста к `recaptcha_toolkit`: выбор v2/v3, stealth, отсутствие второго браузера, бэкенды audio/image/vision_ai, бюджет попыток и интеграция с `ChallengeHandler`/`BrowserEnvironment`/`BrowserSessionManager`;
- `python -m arena_api` — CLI того же bootstrap-слоя (`arena_api/__main__.py`);
- остальные `tests/` — офлайн unit-тесты на парсинг cookie/SSE, mapping моделей, очереди, async API, ошибки, Firefox-сессию, retry при 403, кинематику курсора, init-скрипт детерминизма, детектор challenge и жизненный цикл сессии.
