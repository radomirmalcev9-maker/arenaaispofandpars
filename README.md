0

# arena-api

[![tests](https://github.com/radomirmalcev9-maker/arenaaispofandpars/actions/workflows/tests.yml/badge.svg)](https://github.com/radomirmalcev9-maker/arenaaispofandpars/actions/workflows/tests.yml)

Неофициальная типизированная Python-библиотека для взаимодействия с **доступными вам** Arena frontend/API: Gradio queue/SSE и экспериментальным Direct Chat stream.

> Arena не предоставляет стабильный публичный API для всех этих маршрутов. Эндпоинты, очереди, порядок callback-аргументов и формат стрима могут измениться без предупреждения. Используйте клиент только в соответствии с правилами сервиса и имеющимися у вас правами доступа.
>
> Библиотека использует переданные вами cookie/заголовки и, для Direct Chat, явно переданную вами verification assertion. `auto_client()` и `session_source="firefox"` читают сессию из **вашего собственного** локального профиля браузера — ничего не отправляется наружу, кроме обычных запросов к выбранному `base_url`. При challenge (`ChallengeRequiredError`) клиент сообщает об этом — с браузерной сессией он дождётся проверки, перечитает cookie и повторит запрос.

## Установка

```bash
python -m pip install .
```

Для разработки (тесты + pyflakes + mypy):

```bash
python -m pip install -e '.[dev]'
pytest                 # 580 тестов, все офлайн: ни сети, ни браузера
python -m pyflakes arena_api tests examples example*.py
python -m mypy         # конфигурация — в pyproject.toml
```

Транспорт построен на `httpx` с HTTP/2. Пакет типизирован и помечен
`arena_api/py.typed` (PEP 561), поэтому mypy/pyright видят аннотации у тех, кто
ставит библиотеку. `import arena_api` не тянет `playwright`, `rookiepy`,
`recaptcha_toolkit`, `curl_cffi`, `torch` или `selenium` — все тяжёлые
зависимости импортируются лениво, только когда вы их действительно используете
(это проверяется в CI).

CI (`.github/workflows/tests.yml`) гоняет тесты на Python 3.10–3.13 плюс
pyflakes, mypy, smoke-тест CLI и проверку «лёгкого» импорта.

## Ноль настроек: от вас нужен только вход в аккаунт

Единственный ручной шаг — войти в свой аккаунт в своём браузере. Всё остальное
`arena_api/auto.py` делает сам: находит сессию, подставляет заголовки того же
браузера, проверяет её одним запросом, а при отказе — обновляет.

```bash
python -m pip install '.[auto]'           # всё сразу: профили браузеров + солвер
playwright install chromium               # для решения капчи и для --login
```

То же самое по частям (каждый шаг необязателен):

```bash
python -m pip install '.[firefox]'        # чтение профилей Chrome/Edge/Brave/...
python -m pip install '.[auth-captcha]'   # автоматическое решение reCAPTCHA
python -m pip install '.[e2e]'            # быстрая валидация сессии через curl_cffi
python -m pip install '.[impersonate]'    # отпечаток браузера на TLS/HTTP2 (см. ниже)
```

```python
from arena_api import auto_client

with auto_client() as arena:          # ни одного аргумента
    print(arena.get_models())
```

Если сессия не найдена, библиотека напишет, где именно она искала, и подождёт
Enter: откройте сайт в браузере, войдите, вернитесь в терминал — дальше всё само.

### Один раз залогиниться руками: `arena-api --login`

Если в вашем браузере сессии ещё нет (новый аккаунт, чистая машина, контейнер,
или сайт её сбросил), войдите один раз в обычном окне браузера — команда сама
откроет его, дождётся входа и сохранит сессию:

```bash
python -m arena_api --login              # то же самое: arena-api --login
python -m arena_api --login --models     # войти и сразу воспользоваться сессией
python -m arena_api --login --fresh      # войти под другим аккаунтом
python -m arena_api --login --storage my-state/cookies.json
python -m arena_api --login --channel chrome   # в настоящем Chrome (если аккаунт гугловый)
```

```python
from arena_api import interactive_login, auto_client

outcome = interactive_login()            # headed-браузер, ожидание входа, сохранение
print(outcome.login.describe())          # «14 cookie для arena.ai; saved (state/cookies.json)»
print(outcome.verification.describe())   # «ok: GET /config -> 200»
if outcome.ok:
    with auto_client() as arena:         # дальше аргументы не нужны вовсе
        print(len(arena.get_models()))
```

Что при этом происходит:

1. открывается **headed** Chromium с настройками обычного браузера
   (`identify_automation=False`, без детерминизма тестов) — вы видите страницу;
   при `--channel chrome`/`msedge` вместо него запускается установленный у вас
   браузер;
2. вы входите так, как привыкли: менеджер паролей, 2FA, «запомнить устройство».
   **Учётные данные через библиотеку не проходят** — она ничего не печатает и не
   сохраняет, кроме cookie;
3. вызов сравнивает cookie до и после: раунд засчитывается, как только для
   доменов сайта появились **новые** cookie (имя сессионного cookie знать не нужно);
4. сессия пишется в `state/cookies.json` с правами `0600` (каталог `state/` и
   `cookies.json` уже в `.gitignore`), а «пустое» состояние никогда не
   перезаписывает рабочую сессию;
5. сохранённый файл сразу проверяется запросом к `/config` — поэтому о том, что
   сервер сессию не принял, вы узнаете в тот же момент, а не в следующем запуске.

Коды возврата `--login`: `0` — сессия сохранена и принята (или проверка
отключена `--no-verify`), `1` — сохранить нечего, `2` — сервер сессию отверг.

### Если Google не даёт войти: «этот браузер или приложение может быть небезопасным»

Выбор «Войти через Google» упирается в защиту самого Google, а не сайта:
встроенный Chromium из `playwright install chromium` для Google — анонимная
сборка без фирменных компонентов, и вход из неё он отклоняет. Пароль от
аккаунта тут не поможет — это не про Arena. Три выхода, от лучшего к худшему:

1. **Войти в аккаунт своим обычным браузером — и вовсе не открывать окно
   `--login`.** Google доверяет браузеру, в котором вы уже вошли, а библиотеке
   от него нужны только cookie:
   - Firefox: `python example_firefox.py` или `auto_client()` — профиль Firefox
     в «лестнице» источников есть по умолчанию, ничего ставить не нужно;
   - Chrome/Chromium/Edge/Brave/Vivaldi/Opera: `pip install '.[firefox]'`
     (имя extra обманчиво — оно добавляет `rookiepy`, читающий профили всех этих
     браузеров), затем `auto_client()`.
   Дальше работает `arena-api --status` / `--models` / `--prompt` без всякого
   Playwright.
2. **Дать окну входа настоящий браузер и перестать представляться роботом:**
   ```bash
   python -m arena_api --login --channel chrome --hide-automation
   ```
   `--channel chrome` (или `msedge` на Windows) запускает установленный у вас
   Google Chrome вместо встроенной сборки, а `--hide-automation` убирает то, по
   чему Google и отличает управляемый браузер: флаг `--enable-automation`, строку
   «Chrome управляется программой автоматизации» и `navigator.webdriver`.
   Из Python то же самое:
   `interactive_login(channel="chrome", hide_automation=True)` или
   `login_and_save(channel="chrome", hide_automation=True)`.
   Если Chrome не установлен, команда скажет именно это, а не «поставьте
   chromium». Оба флага влияют **только на окно входа** — сессия, которую
   библиотека затем читает, от них не зависит; по умолчанию маскировка
   выключена, потому что библиотека не скрывает, что она автоматизация.
3. **Сначала войти в Google в этом же окне** (например, через другой вход на
   странице), а уже потом выбирать «Войти через Arena» — работает не всегда и
   потому стоит последним.

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
        wait_out_challenges=True,       # ждать Turnstile/hCaptcha/DataDome
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
| `impersonate` | `"auto"` | чей отпечаток TLS/HTTP2 нести: `auto` (следовать за источником сессии), `firefox`/`chrome`/`firefox147`, `off` |
| `channel` | `None` | в каком браузере открывать окно `--login`: `chrome`/`msedge` вместо встроенного Chromium (нужно, когда аккаунт входит через Google) |
| `hide_automation` | `False` | убрать в окне `--login` признаки автоматизации, которые читает Google (`--enable-automation`, `navigator.webdriver`); включать вместе с `channel="chrome"` |
| `solve_captchas`, `captcha_backend`, `captcha_solver` | `"auto"`, `"audio"`, `None` | решение reCAPTCHA (токен) |
| `wait_out_challenges`, `challenge_wait_timeout` | `True`, `30.0` | ждать ли челленджи без токена: Turnstile, managed Cloudflare, hCaptcha, DataDome |
| `probe_path`, `verify` | `/config`, `True` | каким запросом проверять сессию |
| `max_auth_refreshes` | `2` | сколько раз повторить запрос после обновления |

### То же самое из командной строки

```bash
python -m arena_api --login      # войти руками один раз и сохранить сессию
python -m arena_api --explain    # где искалась сессия, есть ли логин и солвер
python -m arena_api --status     # найденная сессия + один проверочный запрос
python -m arena_api --models     # список моделей
python -m arena_api --prompt "Привет"
```

Флаги ожидания: `--no-wait` (не ждать челленджи без токена, только детектировать),
`--wait-timeout 45` (сколько секунд ждать за один раунд), `--fresh`
(игнорировать сохранённую сессию при входе), `--headless` (спрятать окно входа —
почти никогда не нужно), `--login-rounds 5` (сколько нажатий Enter предложить),
`--channel chrome` и `--hide-automation` (окно входа как настоящий браузер — см.
«Если Google не даёт войти»), `--impersonate chrome|firefox|off` (каким отпечатком
идти к сервису — см. «Отпечаток вместо „нейтрального“ клиента»).

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

`auto_direct_client()` принимает ту же сессию, что и `auto_client()`, и так же
проверяет её при старте (`verify`, `probe_path`), поэтому отклонённый cookie
обновляется и запрос повторяется автоматически. `probe_path` должен указывать на
JSON/API-эндпоинт: HTML-страница со следами капчи читается как challenge
(исключение — страница Direct Chat, она и должна содержать v3-assertion).

## Отпечаток вместо «нейтрального» клиента (`impersonate`)

Сервис за Cloudflare выдаёт cookie `cf_clearance`, привязанную не только к IP и
User-Agent, но и к **отпечатку соединения** — порядку TLS ClientHello и настройкам
HTTP/2. `httpx` (и любой Python-клиент) говорит на своём диалекте, поэтому сессия,
безупречно перенесённая из браузера, всё равно может получить страницу проверки:
cookie верны, заголовки верны, а «рукопожатие» не то. Это ровно тот случай, когда
`arena-api --status` пишет `session: found`, а запрос падает на
`ChallengeRequiredError`.

```bash
python -m pip install 'arena-api[impersonate]'   # curl-cffi + httpx-curl-cffi
```

```python
from arena_api import ArenaClient, auto_client

# Ноль настроек: сессию нашли в Firefox -> поедет на отпечатке Firefox.
with auto_client() as arena:
    print(arena.get_models())
    print(arena.transport.impersonate)        # 'firefox'

# Явно и независимо от источника:
with ArenaClient(cookies=cookies, impersonate="chrome") as arena: ...
with ArenaClient(session_source="firefox", impersonate="firefox147") as arena: ...
with ArenaClient(session_source="firefox", impersonate=False) as arena: ...  # выключить
```

Что выбирается в режиме `auto` (по умолчанию):

| Источник сессии | Профиль | Почему |
| --- | --- | --- |
| Firefox (профиль или `session_source="firefox"`) | `firefox` | этим же менеджером строятся и заголовки |
| Chrome/Chromium/Brave/Vivaldi/Opera/Edge | `chrome` | библиотека представляет их Chromium-заголовками |
| `state/cookies.json`, `ARENA_COOKIE`, `cookies=` | `chrome` | заголовки в этом пути тоже Chromium-shaped |
| источник неизвестен | ничего | лучше честный нейтральный стек, чем Firefox-байты с Chrome-заголовками |
| extra `[impersonate]` не установлен | ничего | молчаливое поведение не имеет права ломать рабочую установку |

Явный `impersonate="…"` без установленного extra — ошибка с подсказкой, а не тихое
игнорирование. `impersonate=` и `http_transport=` взаимоисключающи (impersonation — это
и есть транспорт). В CLI: `python -m arena_api --impersonate chrome|firefox|off`;
состояние видно в `python -m arena_api --explain` (строка `fingerprint`).

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
- **Но не только заголовки** — строка User-Agent это половина отпечатка; вторая это то, *как* клиент говорит по TLS и HTTP/2. Поэтому при установленном extra `[impersonate]` (`pip install 'arena-api[impersonate]'`, он же входит в `[auto]`) клиент по умолчанию отправляет запрос через `curl_cffi` с профилем Firefox, а не через нейтральный стек httpx. Это и есть лечение от «куки верные, а сервис отдаёт страницу проверки»: `cf_clearance` и родственные cookie привязаны к отпечатку, и челлендж прилетает именно из-за его несовпадения. Отключить: `impersonate=False`; задать явно: `impersonate="firefox147"` или `impersonate="chrome"`.
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
| `captcha.py` | мост к вендоренному `recaptcha_toolkit`: решение reCAPTCHA v2/v3 на уже открытой странице (только по явному запросу) |
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

Виды (`ChallengeKind`): `NONE`, `TURNSTILE`, `CLOUDFLARE_MANAGED`, `RECAPTCHA`,
`HCAPTCHA`, `DATADOME`, `INTERSTITIAL`, `RATE_LIMIT`, `UNKNOWN` — приоритет такой
же: заголовок/managed важнее виджета, конкретный вендор важнее общего текста
интерстишла. Для страниц из синхронного API (`playwright.sync_api`) есть
`inspect_page_sync(page)` — те же сигналы, та же классификация, та же
диагностика на диске.

### Если синтетические проверки блокируются

1. Добавьте исключение в WAF/bot-management для egress-IP или ASN вашего CI — либо матчинг по заголовку `X-Synthetic-Monitor`.
2. В CI используйте официальные тестовые sitekey/secret Cloudflare Turnstile: они всегда возвращают проходящий токен, и пайплайн прогоняет реальный путь виджета.
3. На собственных staging-зонах отключите Bot Fight Mode и managed challenge.
4. Для machine-to-machine мониторинга используйте API-токен или сервисный аккаунт вместо чужой браузерной сессии.

При `identify_automation=True` страница получает `window.__arenaSynthetic`, а запросы — заголовок `X-Synthetic-Monitor`, по которому владелец сервиса может сделать исключение.

Поддерживаемые Playwright-опции нормализации окружения: `locale` (он же `navigator.languages`), `timezone_id`, `user_agent`, `viewport`, `device_scale_factor`, `color_scheme`, `reduced_motion`.

### Решение reCAPTCHA во время прогона (opt-in)

Всё вышесказанное — про **детектирование**: по умолчанию `arena_api.auth` находит верификационный экран, сохраняет диагностику и сообщает о нём. Если вам нужно именно *решить* reCAPTCHA (на своём стенде или там, где у вас есть на это право), подключите мост к вендоренному `recaptcha_toolkit`:

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

- **Второго браузера нет.** Боты тулкита используются как фасад солвера: `solve_recaptcha_v2/v3` принимают внешнюю `page` и не трогают собственный контекст бота, поэтому бот создаётся, но **не** запускается. Исключение — `vision_ai`: YOLO-солвер реплицирует виджет в своём Chromium, поэтому ему передаются `website_key`/`website_url` (определяются со страницы) и, по умолчанию, cookie вашей страницы.
- **v2 или v3 определяется по странице**: видимый чекбокс → аудио/картиночный челлендж, невидимый виджет → перехват токена с перезагрузкой страницы (слушатель должен встать до запроса `/recaptcha/api2/reload`). Можно зафиксировать вручную: `RecaptchaToolkitSolver(version="v3")`.
- **Токен умеет только reCAPTCHA.** Turnstile, hCaptcha, DataDome и managed-челлендж Cloudflare токена не дают — их закрывает другой механизм, ожидание/человек (`SOLVABLE_CHALLENGE_KINDS` vs `WAITABLE_CHALLENGE_KINDS`, см. следующий раздел). Смешанные случаи (reCAPTCHA внутри Cloudflare-интерстишла) по умолчанию тоже не решаются токеном: снятие виджета не снимет интерстишел. Флаг `allow_mixed_signals=True` снимает это ограничение.
- **Маскировка применяется при решении** (`apply_stealth=True` по умолчанию): решать капчу, одновременно объявляя `navigator.webdriver` и `X-Synthetic-Monitor`, бессмысленно. `BrowserEnvironmentOptions.with_captcha_solving()` поэтому снимает оба объявления; на собственном стенде, где монитор разрешён по заголовку, верните их: `with_captcha_solving(solver, identify_automation=True)`.
- **Циклы исключены.** Перезагрузка страницы снова будит слушатель, поэтому на страницу выделен бюджет `max_solve_attempts` (по умолчанию 1), параллельные решения по одной странице сериализуются, а неудача поднимает `CaptchaSolveError` (подкласс `ChallengeRequiredError`, так что существующие `except` продолжают работать) с перечислением всех попыток и их причин.
- Быстрый HTTP-путь (`validate()`, `inspect_response()`) страницы не имеет, поэтому всегда остаётся наблюдательным.

### Другие типы капчи: Turnstile, managed Cloudflare, hCaptcha, DataDome

Токен — не единственный способ пройти верификацию. Половина обнаруживаемых видов
вообще не имеет токена, который эта библиотека могла бы произвести, и это не
тупик:

| Вид | Что реально помогает | Кто это делает |
|---|---|---|
| `TURNSTILE`, `CLOUDFLARE_MANAGED` | пройти сам: в настоящем браузере экран обычно снимается за несколько секунд | `ChallengeWaitSolver` — пересканирует страницу, пока challenge не исчезнет |
| `HCAPTCHA`, `DATADOME`, `UNKNOWN`, `INTERSTITIAL` | решить человеку в видимом окне браузера | он же с `interactive=True` (headed): сообщает, что на экране, и продлевает ожидание по Enter |
| `RECAPTCHA` | произвести токен | `RecaptchaToolkitSolver` (раздел выше) |
| `RATE_LIMIT` | подождать и повторить | не решается: это политика повторов, а не капча |

```python
from arena_api.auth import (
    BrowserEnvironment, BrowserEnvironmentOptions,
    ChallengeWaitSolver, CompositeSolver, RecaptchaToolkitSolver,
)

solver = CompositeSolver(               # порядок важен: кто первый поддержит вид
    RecaptchaToolkitSolver(),           # reCAPTCHA -> токен
    ChallengeWaitSolver(                # всё остальное -> ожидание или человек
        timeout=45.0,                   # секунд на один раунд
        poll=1.0,                       # как часто пересматривать страницу
        interactive=True,               # headed: подсказать и дать ещё времени
        rounds=3,
    ),
)
options = BrowserEnvironmentOptions.with_captcha_solving(solver, headless=False)

async with BrowserEnvironment(options) as env:
    page = await env.new_page()
    await page.goto("https://your-staging.example/login")
    observation = await env.solve_challenge(page)
    print(observation.kind)             # none, если экран снят
```

Готовый сценарий — [`example_challenge_wait.py`](example_challenge_wait.py).

Важные детали:

- **Ожидание ничего не устанавливает.** `ChallengeWaitSolver` нужен только
  Playwright и уже открытая страница, поэтому `solve_captchas="auto"` больше не
  превращается в «ничего не делать» на машине без `playwright-recaptcha`:
  reCAPTCHA там по-прежнему не решается, а вот Turnstile/hCaptcha — да.
  `resolve_solver()` собирает композит сам, ничего настраивать не нужно.
- **Результат — `CaptchaAttempt(cleared=True, token=None)`.** Успех определяет
  сам `ChallengeHandler` своим пересканированием страницы, поэтому остальной код не
  знает и не должен знать, был ли токен.
- **Ожидание не блокирует event loop.** Enter в интерактивном режиме
  спрашивается в рабочем потоке (`asyncio.to_thread`), иначе Playwright
  остановился бы вместе с вводом — и страница не смогла бы пройти проверку.
- **Отключить**: `AutoOptions(wait_out_challenges=False)` или `--no-wait`;
  длительность — `challenge_wait_timeout` / `--wait-timeout`.
- **DataDome определяется отдельно**: фреймы `captcha-delivery.com` /
  `geo.captcha-delivery.com`, заголовки `x-datadome`/`x-datadome-cid`, селекторы
  `DATADOME_SELECTORS`. Как и прежде, одного статус-кода мало — нужны маркеры.

## `arena_api._vendor.recaptcha_toolkit` — решение reCAPTCHA и маскировка автоматизации

Вендоренный пакет `arena_api._vendor.recaptcha_toolkit` собирает под одним типизированным API четыре библиотеки:

| Библиотека | Роль |
| --- | --- |
| [`playwright-recaptcha`](https://github.com/Xewdy444/Playwright-reCAPTCHA) | v2 через аудио-челлендж (Google Speech API) или картинки (CapSolver), перехват токена для v3 |
| [`VisionAIRecaptchaSolver-0`](https://github.com/Altum-x/VisionAIRecaptchaSolver-0) | локальное решение v2/v3 на YOLO-моделях, без внешних API |
| [`selenium-recaptcha-solver`](https://pypi.org/project/selenium-recaptcha-solver/) | решение аудио-челленджа v2 для Selenium |
| [`playwright-stealth`](https://pypi.org/project/playwright-stealth/) | маскировка `navigator.webdriver`, языков, платформы и пр. |

> Тулкит лежит **внутри** пакета (`arena_api/_vendor/`), а не отдельным модулем верхнего уровня: так установка `arena-api` не может перезаписать одноимённый проект из PyPI (и наоборот). Он по-прежнему автономен: `import arena_api` его не тянет, единственная связь — opt-in мост `arena_api.auth.captcha` (см. «Решение reCAPTCHA во время прогона» выше), который импортируется лениво и ничего не делает, пока вы не передадите солвер в хендлер, окружение или менеджер сессии.

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
from arena_api._vendor.recaptcha_toolkit import PlaywrightBot, SolverBackend

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
from arena_api._vendor.recaptcha_toolkit import AsyncPlaywrightBot

async def main() -> None:
    async with AsyncPlaywrightBot() as bot:
        page = await bot.new_page()
        await page.goto("https://www.google.com/recaptcha/api2/demo")
        print(await bot.solve_recaptcha_v2(page))

asyncio.run(main())
```

### Selenium

```python
from arena_api._vendor.recaptcha_toolkit import SeleniumBot

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
from arena_api._vendor.recaptcha_toolkit import PlaywrightBot, SolverBackend, VisionAIOptions

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
from arena_api._vendor.recaptcha_toolkit import VisionAISolver

token = VisionAISolver().solve(
    website_key="6Le-wvkSVVABCPBMRTvw0Q4Muexq1bi0DJwx_mJ-",
    website_url="https://www.google.com/recaptcha/api2/demo",
)
```

Конкурентным инстансам нужны разные `server_port` и `download_dir`.

### Обработка ошибок

Всё наследуется от `CaptchaError`, частые случаи — отдельными классами:

```python
from arena_api._vendor.recaptcha_toolkit import CaptchaError, RecaptchaNotFoundError, RecaptchaTimeoutError

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
- `ProtocolError` — ответ не соответствует ожидаемому формату;
- `ClientClosedError` — запрос через закрытый клиент (после `close()`/выхода из
  `with`): постройте новый клиент.

## Примеры и тесты

- `example.py` — Gradio queue/SSE;
- `example_auto_login.py` — ноль настроек: `auto_client()` сам находит сессию, проверяет и обновляет её;
- `example_direct.py` — Direct Chat с assertion, переданной пользователем;
- `example_firefox.py` — сессия из локального профиля Firefox (и печатает, какой отпечаток ушёл на сервер);
- `example_e2e.py` — синтетический мониторинг на `arena_api.auth`: быстрая валидация через `curl_cffi` (`--check`, без браузера), обновление через реальный логин, детерминированный контекст, кинематический ввод, детектор challenge;
- `example_auth_captcha.py` — opt-in решение reCAPTCHA в E2E-прогоне: `with_captcha_solving`, challenge-only обновление сессии и явный `solve_challenge()`;
- `example_login.py` — вход руками один раз: `interactive_login()` открывает headed-браузер, дожидается входа, сохраняет `state/cookies.json` и проверяет его;
- `example_challenge_wait.py` — челленджи без токена: Turnstile/managed Cloudflare ожидаются, hCaptcha/DataDome решаются человеком в видимом окне (`CompositeSolver` + `ChallengeWaitSolver`);
- `tests/test_auth_integration.py` — интеграционные тесты: реальный локальный HTTP-сервер плюс настоящий `curl_cffi` (валидация, редиректы, 401/429, challenge) и сверка генерируемых kwargs с реальными сигнатурами `playwright.async_api`;
- `tests/test_auto.py` — офлайн-тесты `arena_api/auto.py`: порядок источников сессии, классификация проверочного запроса, автообновление отвергнутой сессии, «лестница» обновления, Direct Chat и CLI;
- `tests/test_auth_captcha.py` — офлайн-тесты моста к вендоренному тулкиту (выбор v2/v3, stealth, отсутствие второго браузера, бэкенды audio/image/vision_ai, бюджет попыток) и солверов ожидания: `ChallengeWaitSolver` (опрос, таймаут, интерактивное продление, неблокирующий ввод, sync-путь) и `CompositeSolver` (диспетчеризация по виду);
- `tests/test_login.py` — офлайн-тесты входа руками: эвристика «появились новые cookie», бюджет раундов, отмена, права `0600`, отказ перезаписывать рабочую сессию, посев из существующего файла;
- `python -m arena_api` — CLI того же bootstrap-слоя (`arena_api/__main__.py`);
- `tests/test_packaging.py` — гигиена пакета: `py.typed`, полнота `__all__`, extras из `pyproject.toml`, «лёгкий» импорт, вендоринг тулкита и лицензия;
- `.github/workflows/tests.yml` — CI: pytest (3.10–3.13), pyflakes, mypy, smoke-тест CLI;
- `CHANGELOG.md` — что изменилось в 0.4.0 и 0.3.0;
- остальные `tests/` — офлайн unit-тесты на парсинг cookie/SSE, mapping моделей, очереди, async API, ошибки, Firefox-сессию, retry при 403, кинематику курсора, init-скрипт детерминизма, детектор challenge (включая DataDome и `inspect_page_sync`) и жизненный цикл сессии.

### Быстрый прогон

```console
$ .venv/bin/python -m pytest -q
580 passed in 10.94s
```

Ни один тест не выходит в сеть и не запускает браузер: `curl_cffi`, `playwright`
и вендоренный тулкит подменяются офлайн-двойниками (два теста, требующие
`curl_cffi`, пропускаются, если его нет).

### Покрытие нового кода

Измерено `coverage.py` на этом же прогоне:

| Модуль | Покрытие | Что не покрыто |
| --- | --- | --- |
| `arena_api/auth/login.py` | 99 % | ветка Windows-ACL в `_restrict` (права там задаёт ОС) |
| `arena_api/auth/challenge.py` | 94 % | ветки записи артефактов при недоступной ФС |
| `arena_api/auto.py` | 94 % | сообщения об ошибках редких веток лестницы |
| `arena_api/auth/captcha.py` | 92 % | пути реального тулкита: ffmpeg/VisionAI и `_page_cookies_sync` — нужен настоящий браузер и солвер |

### Проверено

- `pytest -q` — **580 passed**; `mypy arena_api` — Success (27 файлов);
  `pyflakes arena_api tests examples example*.py` — 0 замечаний;
- «лёгкий» импорт `arena_api`: 69 имён, `arena_api.auth` — 67, тяжёлые модули
  (`playwright`, `curl_cffi`, `rookiepy`, `numpy`) не подгружаются;
- `python -m build`: колесо содержит 27 модулей, все семь
  `arena_api/_vendor/recaptcha_toolkit/*.py`, `arena_api/py.typed` и
  `dist-info/licenses/LICENSE`; top-level `recaptcha_toolkit` в колесе **нет**;
  в sdist есть `LICENSE` и `pyproject.toml`;
- `License-Expression: Apache-2.0` в метаданных, extras `auto`/`auth-captcha`
  объявлены;
- все 27 модулей пакета, тесты и примеры парсятся грамматикой Python 3.10
  (`ast.parse(..., feature_version=(3, 10))`) — нижняя граница `requires-python`
  соблюдена;
- CLI без браузера и без сети: `arena-api --explain` показывает лестницу солверов,
  `--status` печатает найденные источники, а `--login` превращает ошибку запуска
  браузера в одну строку с подсказкой `playwright install chromium`.

### Как собрать и поставить

```bash
python -m build                      # sdist + wheel в dist/
pip install dist/*.whl               # поставить колесо
playwright install chromium          # браузер для E2E-пути
# разработка:
pip install -e '.[test,e2e]' playwright-stealth pyflakes mypy
```

## Лицензия

Apache-2.0 (Apache License 2.0) — полный текст в [`LICENSE`](LICENSE); в `pyproject.toml` лицензия объявлена через SPDX-выражение `license = "Apache-2.0"` (PEP 639). Код можно свободно использовать,
изменять и распространять (в том числе коммерчески) при сохранении уведомления об авторских
правах и текста лицензии.

Вендоренный `arena_api/_vendor/recaptcha_toolkit` распространяется на тех же условиях. Его
необязательные зависимости (Playwright, `playwright-recaptcha`, `playwright-stealth`,
`selenium-recaptcha-solver`, VisionAIRecaptchaSolver) имеют собственные лицензии — ставьте их
только под тот бэкенд, которым действительно пользуетесь.
