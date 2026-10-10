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
- `tests/` — офлайн unit-тесты на парсинг cookie/SSE, mapping моделей, очереди, async API, ошибки, Firefox-сессию и retry при 403.
