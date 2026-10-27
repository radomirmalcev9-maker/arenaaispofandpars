@echo off
rem ===========================================================================
rem  arena-claude.bat -- Claude Code на моделях Arena, одним файлом.
rem
rem  Что делает:
rem    1. поднимает Anthropic-совместимый шим  (arena-api --claude --port N)
rem       в скрытом окне, лог пишет в %TEMP%\arena-claude-<port>.log;
rem    2. ждёт, пока порт начнёт отвечать на /healthz (до 60 секунд);
rem    3. выставляет ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY и запускает claude;
rem    4. когда Claude Code закрывается -- гасит шим (если поднимал его сам).
rem
rem  Запуск:
rem    arena-claude.bat                 обычный запуск (порт 8082)
rem    arena-claude.bat --resume ...    все аргументы уходят в claude как есть
rem    arena-claude.bat doctor          диагностика: пакет, сессия, движок, SSE
rem
rem  Настройки (переменные окружения перед запуском):
rem    ARENA_CLAUDE_PORT=8082           порт шима
rem    ARENA_CLAUDE_MODEL=arena-auto    модель на стороне Arena
rem    ARENA_CLAUDE_SESSION=firefox     источник сессии: firefox, firefox:<профиль>,
rem                                     storage, chrome, auto
rem    ARENA_CLAUDE_ENGINE=warm         движок: warm (демон), http, echo
rem
rem  Требуется: установленный пакет arena-api и Claude Code (`claude` в PATH).
rem  Работает на Windows 10+ (использует curl или PowerShell для проверки порта).
rem ===========================================================================

setlocal EnableExtensions
title Arena + Claude Code

rem --- настройки по умолчанию ------------------------------------------------
set "SHIM_PORT=%ARENA_CLAUDE_PORT%"
if "%SHIM_PORT%"=="" set "SHIM_PORT=8082"
set "MODEL=%ARENA_CLAUDE_MODEL%"
if "%MODEL%"=="" set "MODEL=arena-auto"
set "SESSION=%ARENA_CLAUDE_SESSION%"
if "%SESSION%"=="" set "SESSION=firefox"
set "ENGINE=%ARENA_CLAUDE_ENGINE%"
if "%ENGINE%"=="" set "ENGINE=warm"

set "SHIMPID="
set "STARTED_SHIM="
set "LOG=%TEMP%\arena-claude-%SHIM_PORT%.log"

call :find_arena
if not defined LAUNCH_EXE goto :no_package

if /i "%~1"=="doctor" goto :doctor
if /i "%~1"=="--doctor" goto :doctor

rem --- уже слушает? тогда второй шим не нужен --------------------------------
call :probe
if "%PORT_OPEN%"=="1" goto :run_claude

echo [arena] запускаю шим на порту %SHIM_PORT% (движок %ENGINE%, сессия %SESSION%)...
if exist "%LOG%" del "%LOG%" >nul 2>&1
if exist "%LOG%.err" del "%LOG%.err" >nul 2>&1

for /f "usebackq delims=" %%p in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $p = Start-Process -FilePath '%LAUNCH_EXE%' -ArgumentList '%LAUNCH_ARGS%' -PassThru -WindowStyle Hidden -RedirectStandardOutput '%LOG%' -RedirectStandardError '%LOG%.err'; $p.Id" 2^>nul`) do set "SHIMPID=%%p"

if not defined SHIMPID goto :no_start
set "STARTED_SHIM=1"

set /a TRIES=0
:wait_loop
set /a TRIES+=1
call :probe
if "%PORT_OPEN%"=="1" goto :shim_ready
if %TRIES% GEQ 60 goto :no_response
timeout /t 1 /nobreak >nul
goto :wait_loop

:no_response
echo.
echo [arena] ОШИБКА: шим не ответил на http://127.0.0.1:%SHIM_PORT%/healthz за 60 секунд.
echo [arena] лог: %LOG%
if exist "%LOG%.err" (
    echo ------- последние строки лога -------
    powershell -NoProfile -Command "Get-Content -Tail 15 '%LOG%.err'"
    powershell -NoProfile -Command "Get-Content -Tail 15 '%LOG%'"
)
echo.
echo [arena] запускаю диагностику...
call :run_doctor
call :stop_shim
endlocal & exit /b 1

:no_start
echo.
echo [arena] ОШИБКА: не удалось запустить шим через PowerShell.
echo [arena] проверьте, что установлен пакет: pip install -U -e ".[auto]"
echo [arena] и что команда видна:  arena-api --version
endlocal & exit /b 1

:shim_ready
echo [arena] шим готов.

:run_claude
echo.
echo   ANTHROPIC_BASE_URL = http://127.0.0.1:%SHIM_PORT%
echo   модель             = %MODEL%
echo   сессия             = %SESSION%
echo.
if not defined STARTED_SHIM echo [arena] порт %SHIM_PORT% уже слушал -- использую его и не буду гасить.

where claude >nul 2>&1
if errorlevel 1 goto :no_claude

set "ANTHROPIC_BASE_URL=http://127.0.0.1:%SHIM_PORT%"
set "ANTHROPIC_API_KEY=arena"
set "ANTHROPIC_MODEL=%MODEL%"
set "ANTHROPIC_SMALL_FAST_MODEL=%MODEL%"
rem Телеметрия и проверки обновлений ходят в api.anthropic.com мимо шима --
rem выключаем их, чтобы Claude Code не тормозил на них и не падал с ошибкой.
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"

claude %*
set "CLAUDE_EXIT=%ERRORLEVEL%"

echo.
echo [arena] Claude Code завершился (код %CLAUDE_EXIT%).
call :stop_shim
if exist "%LOG%" del "%LOG%" >nul 2>&1
if exist "%LOG%.err" del "%LOG%.err" >nul 2>&1
endlocal & exit /b %CLAUDE_EXIT%

:no_claude
echo [arena] ОШИБКА: команда claude не найдена в PATH.
echo [arena] установите Claude Code:  npm install -g @anthropic-ai/claude-code
echo.
echo [arena] шим при этом работает: http://127.0.0.1:%SHIM_PORT%
echo [arena] можно запустить вручную в другом окне:
echo           set ANTHROPIC_BASE_URL=http://127.0.0.1:%SHIM_PORT%
echo           set ANTHROPIC_API_KEY=arena
echo           claude
pause
call :stop_shim
endlocal & exit /b 1

rem ===========================================================================
rem  Вспомогательные метки
rem ===========================================================================

:doctor
echo [arena] диагностика: пакет, сессия, движок ответа, протокол на loopback
echo.
call :run_doctor
endlocal & exit /b %ERRORLEVEL%

:run_doctor
"%LAUNCH_EXE%" %DOCTOR_ARGS% --claude --doctor --engine %ENGINE% --session %SESSION% --model %MODEL%
goto :eof

rem --- поиск arena-api (или python -m arena_api) ------------------------------
:find_arena
set "LAUNCH_EXE="
set "LAUNCH_ARGS="
set "DOCTOR_ARGS="
for /f "delims=" %%e in ('where arena-api 2^>nul') do (
    set "LAUNCH_EXE=%%e"
    goto :arena_found
)
for /f "delims=" %%e in ('where py 2^>nul') do (
    set "LAUNCH_EXE=%%e"
    set "LAUNCH_ARGS=-m arena_api"
    set "DOCTOR_ARGS=-m arena_api"
    goto :arena_found
)
for /f "delims=" %%e in ('where python 2^>nul') do (
    set "LAUNCH_EXE=%%e"
    set "LAUNCH_ARGS=-m arena_api"
    set "DOCTOR_ARGS=-m arena_api"
    goto :arena_found
)
goto :eof

:arena_found
set "LAUNCH_ARGS=%LAUNCH_ARGS% --claude --port %SHIM_PORT% --engine %ENGINE% --session %SESSION% --model %MODEL%"
goto :eof

rem --- отвечает ли порт (curl, иначе PowerShell) ------------------------------
:probe
set "PORT_OPEN=0"
where curl >nul 2>&1
if errorlevel 1 goto :probe_ps
curl -s -f -m 2 -o NUL "http://127.0.0.1:%SHIM_PORT%/healthz" >nul 2>&1
if not errorlevel 1 set "PORT_OPEN=1"
goto :eof

:probe_ps
for /f "usebackq delims=" %%r in (`powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%SHIM_PORT%/healthz').StatusCode } catch { '' }" 2^>nul`) do (
    if not "%%r"=="" set "PORT_OPEN=1"
)
goto :eof

rem --- остановка поднятого шима (только своего) --------------------------------
:stop_shim
if not defined STARTED_SHIM goto :eof
if not defined SHIMPID goto :eof
taskkill /F /T /PID %SHIMPID% >nul 2>&1
set "STARTED_SHIM="
goto :eof

:no_package
echo.
echo [arena] ОШИБКА: не найден ни arena-api, ни python.
echo [arena] установите пакет из клона репозитория:
echo           pip install -U -e ".[auto]"
echo [arena] и проверьте:  arena-api --version
endlocal & exit /b 1
