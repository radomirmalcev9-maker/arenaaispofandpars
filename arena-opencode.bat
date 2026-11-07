@echo off
setlocal EnableExtensions
rem Desktop mode starts the local API; use --cli to launch the CLI instead.
if /I "%CD%"=="%SystemRoot%\System32" cd /d "%~dp0"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Use a working interpreter, not merely the first executable on PATH.
if not exist "%~dp0.venv\Scripts\python.exe" goto try_py
set "DIAG_EXE=%~dp0.venv\Scripts\python.exe"
set "DIAG_ARGS="
"%~dp0.venv\Scripts\python.exe" -c "import arena_api.compat.opencode_server" >nul 2>&1
if errorlevel 1 goto try_py
set "PY_EXE=%~dp0.venv\Scripts\python.exe"
set "PY_ARGS="
goto run

:try_py
where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 goto try_python
set "DIAG_EXE=py"
set "DIAG_ARGS=-3"
py -3 -c "import arena_api.compat.opencode_server" >nul 2>&1
if errorlevel 1 goto try_python
set "PY_EXE=py"
set "PY_ARGS=-3"
goto run

:try_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
set "DIAG_EXE=python"
set "DIAG_ARGS="
python -c "import arena_api.compat.opencode_server" >nul 2>&1
if errorlevel 1 goto missing_dependencies
set "PY_EXE=python"
set "PY_ARGS="
goto run

:missing_python
echo [arena] No working Python with arena-api dependencies was found.
if defined DIAG_EXE echo [arena] Import error from the last Python found:
if defined DIAG_EXE "%DIAG_EXE%" %DIAG_ARGS% -c "import arena_api.compat.opencode_server"
echo [arena] Install Python 3.10 or newer and the project dependencies.
echo [arena] In a terminal, from the repository directory:
echo     py -3 -m pip install -e ".[auto]"
echo     py -3 -m playwright install chromium
set "RESULT=1"
goto finish

:missing_dependencies
echo [arena] Python was found, but arena-api could not be imported.
echo [arena] Import error:
python -c "import arena_api.compat.opencode_server"
echo.
echo [arena] From the repository directory, install dependencies with:
echo     python -m pip install -e ".[auto]"
echo     python -m playwright install chromium
set "RESULT=1"
goto finish

:run
echo [arena] Starting the local Arena API using %PY_EXE% %PY_ARGS%...
"%PY_EXE%" %PY_ARGS% -m arena_api.compat.opencode_server --desktop %*
set "RESULT=%ERRORLEVEL%"

:finish
echo.
echo [arena] Launcher exited with code %RESULT%.
if not "%RESULT%"=="0" echo [arena] Read the error above before closing this window.
if not "%ARENA_OPENCODE_NO_PAUSE%"=="1" pause
exit /b %RESULT%
