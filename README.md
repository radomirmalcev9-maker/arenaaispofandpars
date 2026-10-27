# arena-api

Типизированный синхронный и асинхронный Python-клиент и CLI для **Arena (`arena.ai`)** с автоматическим подхватом сессии и прокси из браузера, TLS-имперсонацией (`curl_cffi`), **невидимым фоновым браузером** для прохождения reCAPTCHA Enterprise v3 и встроенным аудио-авторешателем капчи **без внешнего `ffmpeg`**.

---

## Возможности

- **Zero-config подхват сессии и прокси**: автоматически читает куки из локального профиля Firefox (включая живую сессию `recovery.jsonlz4` Firefox 136+ и настройки прокси `prefs.js` / системного прокси Windows), профилей Chromium (`rookiepy`), `state/cookies.json` или `ARENA_COOKIE`.
- **Невидимый фоновый режим**: запросы Direct Chat выполняются через настоящий десктопный Chrome / Edge / Chromium со скрытым за пределами экрана окном (`WS_EX_TOOLWINDOW`, без иконки в панели задач, без `Alt+Tab` и без перехвата фокуса).
- **Встроенное авторешение капчи без FFmpeg**: при появлении Cloudflare Turnstile или Google reCAPTCHA v2 челлендж автоматически решается в фоне (декодирование аудио через WebAudio `OfflineAudioContext` + встроенный чистый Python FLAC-кодер).
- **Чистый UTF-8 поток**: корректно декодирует кириллицу и эмодзи из стриминговых ответов `arena.ai` и безопасно выводит текст в консоль Windows (`cp1251` / `cp866` / `utf-8`).
- **Лёгкое ядро**: базовый `import arena_api` зависит только от `httpx[http2]`; тяжёлые зависимости (`playwright`, `curl_cffi`, `rookiepy`, `arena_api._vendor.recaptcha_toolkit`) подгружаются лениво только тогда, когда реально нужны.

---

## Установка

```bash
# Полная установка со всеми возможностями (рекомендуется)
python -m pip install -e ".[auto]"

# Если на ПК нет установленного Google Chrome или Microsoft Edge:
python -m playwright install chromium
```

Дополнительные профили зависимостей в `pyproject.toml`:
- `.[impersonate]` — отправка HTTP/2 и TLS-отпечатка реального браузера (`curl-cffi`, `httpx-curl-cffi`);
- `.[firefox]` — чтение профилей браузеров через `rookiepy` (для Firefox также есть встроенный lock-safe читатель `cookies.sqlite` + `recovery.jsonlz4`);
- `.[e2e]` / `.[captcha]` / `.[auth-captcha]` / `.[vision]` — браузерное окружение и дополнительные бэкенды капчи;
- `.[dev]` — `pytest`, `mypy`, `pyflakes`.

---

## Быстрый старт (CLI)

Достаточно один раз быть авторизованным на `https://arena.ai` в своём браузере (например, Firefox) или выполнить `--login`:

```bash
# Отправить запрос и получить потоковый ответ (браузер работает полностью невидимо)
python -m arena_api --prompt "Привет! Чем хорош дебют Гроба?"

# Указать конкретную модель (по умолчанию используется авто-выбор самой свежей модели сайта)
python -m arena_api --model claude-opus-4-6 --prompt "Объясни разницу между TCP и UDP."

# Список доступных моделей
python -m arena_api --models

# Проверить найденную сессию и статус доступа
python -m arena_api --status
arena-api --status

# Показать все проверенные источники кук, прокси и TLS-профиль без сетевых запросов
python -m arena_api --explain

# Диагностика профилей Firefox
python -m arena_api --diagnose

# Разовый ручной вход в окне браузера с сохранением кук в ./state/cookies.json
python -m arena_api --login

# При необходимости показать окно браузера для визуальной отладки
python -m arena_api --show-browser --prompt "Тест с видимым окном"
```

---

## Тёплый демон: мгновенные повторные ответы

Почти всё время между `arena-api --prompt ...` и первым символом ответа уходит
не на генерацию, а на одинаковую для каждого запроса подготовку: чтение
сессии, запуск браузера, загрузку Direct Chat, прогрев композера и подпись
reCAPTCHA v3. Поэтому одиночный запрос с нуля стоит секунды, даже когда модель
начинает отвечать сразу.

Первый `--prompt` теперь поднимает фоновый демон (`python -m arena_api.warm`),
который держит браузер и вкладку Direct Chat уже загруженными. Все следующие
вызовы CLI подключаются к нему по loopback-сокету и платят только за отправку
промпта:

```bash
arena-api --prompt "Привет!"      # первый вызов: запускает демон (как раньше)
arena-api --prompt "А теперь про TCP?"   # ~десятки миллисекунд до отправки
arena-api --warm-start            # поднять демон заранее (например, из скрипта при входе)
arena-api --warm-status           # где демон, тёплый ли браузер, сколько простаивает
arena-api --warm-stop             # остановить демон и закрыть браузер
```

- Демон слушает только `127.0.0.1`, доступ по случайному токену из файла
  `state/warm-daemon.json` с правами `0600`; в сеть ничего не отдаётся.
- Простаивая `--warm-idle` секунд (по умолчанию 900), он завершается сам и
  закрывает браузер.
- Любая проблема (нет браузера, занятый профиль, устаревший state-файл, сессия
  отклонена) означает прозрачный откат на прежний путь в том же процессе:
  вывод и код возврата остаются прежними.
- Отключить: `--no-warm` для одного запуска или `ARENA_WARM=0` целиком.
  Управление таймаутами: `--warm-idle`, `--warm-timeout`, `ARENA_WARM_IDLE`,
  `ARENA_WARM_TIMEOUT`; свой путь к state-файлу — `--warm-state` /
  `ARENA_WARM_STATE`.

Демон использует тот же `--prompt`/`--model` и те же флаги, что и обычный
запуск: они передаются ему целиком, поэтому семантика опций (прокси, профиль,
канал браузера, решатель капчи) не дублируется.

### Замер: `ARENA_PROFILE=1`

Чтобы увидеть, где на самом деле уходит время, включите профилирование:

```bash
ARENA_PROFILE=1 arena-api --prompt "Привет!"
```

В конце работы в stderr печатается таблица фаз (поиск сессии, запуск браузера,
загрузка страницы, отправка, первый байт). `ARENA_PROFILE=json` выдаёт ту же
информацию одной JSON-строкой — удобно собирать в цикле или в CI. Отсчёт
начинается с момента загрузки профилировщика (несколько миллисекунд после
старта интерпретатора), поэтому «total» — это время работы кода пакета, а не
полное время команды.

Чтобы измерить выигрыш на своей машине (без «на глазок»), есть готовый
бенчмарк: он запускает один и тот же промпт холодно (`--no-warm`, демон
остановлен) и через демон, и печатает медианы и коэффициенты.

```bash
python tools/bench_cli.py --prompt "Ответь одним словом: ок"
```

Он показывает **две** метрики:

- `whole command` — всё время команды, вместе с генерацией ответа (это то, что
  вы чувствуете, но длинный ответ размывает сравнение);
- `first token` — время до первого символа из отчёта `ARENA_PROFILE=json`,
  то есть ровно те накладные расходы, которые убирает демон.

Arena ограничивает частоту запросов, поэтому промпты по умолчанию разделены
паузой 20 с (`--pause`), а упёршийся в лимит прогон повторяется после паузы,
которую назвал сервер (`--retries`). Сбой одного прогона не обрывает замер:
скрипт продолжает, печатает сколько успел измерить, и отдельно сообщает, если
холодный путь вообще не работает (тогда сначала `arena-api --status`).

---

## Использование из Python

### Direct Chat (`arena.ai`, синхронно и потоково)

```python
import sys
from arena_api import AutoOptions, auto_direct_client

with auto_direct_client(
    AutoOptions(), verification_token="auto", prewarm=True
) as client:
    for chunk in client.stream("Привет! Напиши 3 коротких совета по Python."):
        if chunk.delta:
            sys.stdout.write(chunk.delta)
            sys.stdout.flush()
```

`prewarm=True` заранее запускает браузер, устанавливает cookie и загружает
Direct Chat-страницу параллельно с проверкой сессии. В быстром CLI-пути этот
прогрев включён автоматически. Если в `--model` / `model=` передан готовый UUID
модели, клиент отправляет его сразу, без дополнительного GET-запроса за
каталогом моделей; разрешение человекочитаемого имени по-прежнему использует
каталог.

### Асинхронный Direct Chat

```python
import asyncio
from arena_api import AutoOptions, auto_async_direct_client

async def main() -> None:
    async with await auto_async_direct_client(
        AutoOptions(), verification_token="auto", prewarm=True
    ) as client:
        answer = await client.acomplete("Коротко сравни SQLite и PostgreSQL.")
        print(answer)

asyncio.run(main())
```

### Классический Gradio-клиент

Для развёртываний со стандартной очередью Gradio (`/queue/join`) доступен `auto_client()` (и асинхронный `auto_async_client()`):

```python
from arena_api import auto_client

with auto_client() as client:
    print(client.complete("Hello!"))
```

---

## Примеры (`examples/`)

Все готовые скрипты собраны в каталоге [`examples/`](examples/):

- [`examples/quickstart.py`](examples/quickstart.py) — минимальный запуск Direct Chat с потоковым выводом ответа в консоль;
- [`examples/firefox_session.py`](examples/firefox_session.py) — прямое чтение сессии и прокси из профиля Firefox (`FirefoxSessionManager`);
- [`examples/login_save_cookies.py`](examples/login_save_cookies.py) — разовый вход через браузер с сохранением `./state/cookies.json`;
- [`examples/captcha_autosolve.py`](examples/captcha_autosolve.py) — автономная демонстрация встроенного решателя reCAPTCHA без `ffmpeg`.

---

## Структура пакета

| Модуль | Описание |
| --- | --- |
| `arena_api/auto.py` | Zero-config обнаружение сессий, `AutoOptions`, `DirectVerificationToken`, `auto_direct_client`, `auto_client` и CLI. |
| `arena_api/_window_cloak.py` | Скрытие окна браузера на уровне ОС (Win32 `ctypes` ToolWindow, CDP `setWindowBounds`, Preferences) и восстановление UTF-8 из CDP. |
| `arena_api/_direct_page.py` | Нативная работа с UI-композером `arena.ai`, инъекция кук доверия и детекция челленджей после отправки. |
| `arena_api/warm.py` | Тёплый демон для повторных `--prompt` вызовов: state-файл, loopback-протокол, `--warm-status` / `--warm-stop`. |
| `arena_api/latency.py` | Опциональный профилировщик фаз запроса (`ARENA_PROFILE=1` / `json`). |
| `arena_api/_cli.py` | Лёгкая точка входа CLI: одноразовый `--prompt` уходит в демон без импорта `arena_api.auto`. |
| `arena_api/direct.py` | Клиенты `DirectArenaClient` / `AsyncDirectArenaClient` и парсер стрима Vercel AI Data Stream / SSE. |
| `arena_api/transport.py` | HTTP/2 транспорт, нормализация кук и прокси, TLS-имперсонация (`curl_cffi`). |
| `arena_api/auth/` | Менеджеры сессий (`FirefoxSessionManager`, `BrowserSessionManager`), `BrowserEnvironment`, кинематика мыши и встроенный `autosolve.py`. |
| `arena_api/_vendor/recaptcha_toolkit/` | Встроенный подпакет `arena_api._vendor.recaptcha_toolkit` для интеграции дополнительных солверов без конфликтов имён в PyPI. |

---

## Проверка и тесты

```bash
python -m pyflakes arena_api tests examples
python -m mypy
python -m pytest -q
```

Лицензия: [Apache-2.0](LICENSE).
