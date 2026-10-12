# История изменений

Формат: краткий список по версиям. Версии до 0.3.0 в этом файле не описаны —
смотрите историю git.

## 0.4.0

### Вход руками один раз: `arena-api --login`

- `arena_api/auth/login.py`: `login_and_save()` / `alogin_and_save()` открывают
  **headed** Chromium с настройками обычного браузера (`identify_automation=False`,
  без детерминизма тестов), ждут, пока вы войдёте, и пишут Playwright
  `storage_state` в `state/cookies.json` — тот самый путь, который
  `auto_client()` ищет в первую очередь. Учётные данные через библиотеку не
  проходят; при желании форму заполняет ваш `LoginFlow`.
- Успех определяется сравнением cookie до и после: раунд засчитывается, когда для
  доменов сайта появились новые cookie, поэтому знать имя сессионного cookie не
  нужно. Файл пишется с правами `0600`, `state/` добавлен в `.gitignore`, а
  «пустое» состояние никогда не перезаписывает рабочую сессию (тогда
  `saved=False`, файл не тронут).
- Существующий `cookies.json` по умолчанию используется как посев контекста
  (`reuse_existing`), чтобы не входить заново; `fresh=True` — войти под другим
  аккаунтом. Отмена (Ctrl-C / закрытый stdin) останавливает ожидание, но всё
  равно возвращает то, что браузер уже накопил.
- `arena_api.auto.interactive_login()` + `LoginOutcome`: тот же вход из кода, с
  последующей проверкой сохранённого файла запросом к `/config`. Проверка
  намеренно не лечит сессию сама (`interactive=False`, `verify=False`) —
  иначе «лекарством» от отказа оказался бы ещё один вход.
- CLI: `--login` (коды возврата 0/1/2), `--fresh`, `--headless`,
  `--login-rounds`, а `--storage` теперь описывает и куда сессия сохраняется.
  `--login --models` входит и сразу выполняет задачу с новой сессией.

### Другие типы капчи, а не только reCAPTCHA

- `ChallengeKind.DATADOME`: DataDome определяется по фреймам
  `captcha-delivery.com`/`geo.captcha-delivery.com`, заголовкам
  `x-datadome`/`x-datadome-cid` и селекторам `DATADOME_SELECTORS` — и в быстром
  HTTP-пути, и в браузере.
- `ChallengeWaitSolver` закрывает виды, у которых токена не существует:
  Turnstile и managed Cloudflare ожидаются (страница пересканивается, пока
  challenge не исчезнет), hCaptcha/DataDome/неизвестный экран передаются
  человеку в headed-окне (`interactive=True` — подсказка и продление ожидания по
  Enter). Результат — `CaptchaAttempt(cleared=True, token=None)`; успех, как и
  прежде, определяет `ChallengeHandler` своим пересканированием.
- Enter в интерактивном ожидании спрашивается в рабочем потоке
  (`asyncio.to_thread`): блокировка event loop остановила бы и Playwright, то
  есть как раз ту страницу, которая должна пройти проверку.
- `CompositeSolver` распределяет виды между солверами (первый поддержавший
  выигрывает), `solver_for()`/`describe_solver()` — публичные помощники.
- `DirectVerificationToken` (`verification_token="auto"`) больше не может тихо
  вернуть пустой assertion: композитный солвер разворачивается до токен-моста
  (`_token_solver`), солвер, который reCAPTCHA не поддерживает (ожидание на
  машине без `playwright-recaptcha`), отклоняется с подсказкой, а пустой токен
  после «успешного» снятия экрана превращается в `AuthenticationError`.
- `resolve_solver()` собирает композит автоматически: токен-мост (если
  зависимости на месте) + ожидание. Так как ожиданию нужен только Playwright,
  `solve_captchas="auto"` больше не «ничего» на машине без
  `playwright-recaptcha`. Новые поля `AutoOptions`: `wait_out_challenges=True`,
  `challenge_wait_timeout=30.0`; флаги `--no-wait`, `--wait-timeout`; строка
  `--explain` показывает, какой солвер действительно будет работать.
- `inspect_page_sync()` — синхронный двойник инспектора страницы (те же сигналы,
  классификация и диагностика на диске); общий код вынесен в `_frame_signals`,
  `_scan_signals`, `_capture_target`/`_finish_capture`.
- Исправлена классификация по селектору: `.h-captcha` (документированный класс
  виджета из `HCAPTCHA_SELECTORS`) не содержал подстроки `hcaptcha` и потому
  попадал в `UNKNOWN`. Сравнение теперь игнорирует пунктуацию.
- `REMEDIATION_TEMPLATE` подсказывает не только инфраструктурные решения, но и
  то, что экран можно подождать или решить руками там, где это разрешено.

### Упаковка и лицензия

- `recaptcha_toolkit` переехал внутрь пакета:
  `arena_api._vendor.recaptcha_toolkit`. Установка `arena-api` больше не может
  перезаписать одноимённый проект из PyPI (и наоборот), а glob `arena_api*`
  подхватывает подпакет сам. Импорты — относительные внутри тулкита, поэтому
  изменились только публичные пути: `from arena_api._vendor.recaptcha_toolkit
  import ...`. `pyproject.toml` больше не собирает отдельный top-level пакет.
- Добавлен `LICENSE` — Apache-2.0 (канонический текст, сверенный с независимой
  копией), в `pyproject.toml` лицензия объявлена SPDX-выражением
  `license = "Apache-2.0"` + `license-files` (PEP 639, `setuptools>=77`);
  классификатор `License ::` убран — с PEP 639 он несовместим. Добавлены
  остальные классификаторы и секция «Лицензия» в README.
- Тесты: `tests/test_login.py`, блоки про ожидание/композит в
  `tests/test_auth_captcha.py`, DataDome и `inspect_page_sync` в
  `tests/test_auth_challenge.py`, `interactive_login` и `--login` в
  `tests/test_auto.py`, лицензия и вендоринг в `tests/test_packaging.py`.
  Всего 580 офлайн-тестов.
- Прогоны герметичны: `isolated_options()` в `tests/test_auto.py` теперь
  фиксирует `profiles_root` несуществующим каталогом внутри `tmp_path`. Без
  этого ступень Firefox разрешала профиль **разработчика**
  (`~/.mozilla/firefox`, на Windows `%APPDATA%\Mozilla\Firefox\Profiles`) и
  отвечала раньше проверяемой ступени: на машине, где в Firefox есть cookie
  arena.ai, `test_other_browsers_are_read_through_rookiepy` падал с
  `'firefox' == 'browser:chrome'`. Добавлен сторожевой тест
  `test_isolated_options_never_point_at_a_host_browser_profile`.
- Новые примеры: `example_login.py` (вход руками) и
  `example_challenge_wait.py` (челленджи без токена).

## 0.3.0

### Ноль настроек: `arena_api.auto`

- `auto_client()`, `auto_async_client()`, `auto_direct_client()`,
  `auto_async_direct_client()` — единственный ручной шаг теперь вход в аккаунт в
  своём браузере. Библиотека сама находит сессию (`cookies=` →
  `ARENA_COOKIE`/`ARENA_COOKIES_FILE` → `state/cookies.json` → профиль Firefox →
  Chrome/Chromium/Edge/Brave/Vivaldi/Opera через `rookiepy`), подставляет
  заголовки того же браузера, проверяет сессию одним запросом и обновляет её.
- «Лестница» обновления `AutoSessionManager`: перечитать источник → вход по
  форме из полного набора `ARENA_*` → решить reCAPTCHA в браузере.
- Проверка сессии замечает капчу-интерстициал, отданный с кодом 200 вместо JSON
  (страница Direct Chat при этом не считается капчей — она и должна содержать
  v3-assertion).
- CLI: `python -m arena_api` (`--explain`, `--status`, `--models`, `--prompt`,
  `--solve auto|on|off`, `--non-interactive`, …) и консольная команда
  `arena-api`. Коды возврата: 0 — работает, 1 — сессия не найдена, 2 — сессия
  отвергнута.
- `DirectArenaClient`/`AsyncDirectArenaClient` принимают `session_source` и
  `max_auth_refreshes`: Direct Chat теперь обновляет сессию и повторяет запрос
  так же, как основной клиент. Assertion по-прежнему добывает вызывающий;
  `verification_token="auto"` — отдельный явный opt-in.
- Транспорт получил публичный `apply_session_cookies()` — для обновления,
  выполненного вне запроса.
- Обновление сессии сериализовано: параллельные запросы, получившие 403, не
  запускают по браузеру и не спрашивают Enter по разу каждый — первый обновляет,
  остальные берут его результат.

### Решение reCAPTCHA во время auth-прогона

- `arena_api/auth/captcha.py`: мост к `recaptcha_toolkit` — определение v2/v3,
  бэкенды AUDIO/IMAGE/перехват v3/VisionAI (YOLO), stealth всегда, без Selenium
  и без второго браузера (используется страница самого прогона).
- Подключено в `ChallengeHandler.check()`/`install()`,
  `BrowserEnvironment.with_captcha_solving()` и обновление
  `BrowserSessionManager`; всё opt-in.
- Новая ошибка `CaptchaSolveError` (подкласс `ChallengeRequiredError`) с полным
  отчётом о попытках.
- Extras: `auth-captcha` (мост), `auto` (всё для bootstrap сразу).

### Исправления и гигиена

- `ClientClosedError`: запрос через закрытый клиент больше не выглядит как
  `RuntimeError` из `httpx` и ловится через `except ArenaError`.
- Сообщения об ошибке `session_source` называют источник: профиль Firefox и
  свой менеджер сессии больше не путаются.
- `arena_api/py.typed` + `[tool.setuptools.package-data]`: аннотации видны
  mypy/pyright у тех, кто ставит пакет (PEP 561).
- `__all__` пакета полный (62 имени): `from arena_api import *` и статический
  анализ больше не расходятся с фактическим API.
- pyflakes чистый по всему репозиторию; mypy чистый при конфигурации из
  `pyproject.toml` (`check_untyped_defs`, `warn_unused_ignores`).
- CI: `.github/workflows/tests.yml` — pytest на Python 3.10–3.13 (Ubuntu) и
  дополнительно macOS/Windows, pyflakes, mypy, проверка «лёгкого» импорта,
  smoke-тест CLI, наличие `py.typed`.
- Тесты платформенных путей Firefox (`default_profiles_root`, User-Agent-токен)
  для Windows/macOS/Linux — раньше эта логика не была покрыта.
- `arena_api.egg-info/` убран из-под версионного контроля (артефакт сборки;
  `.gitignore` его уже покрывал).
- Конфигурация pytest (`testpaths`) и extra `dev` (pytest, pyflakes, mypy).
