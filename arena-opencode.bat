@echo off
setlocal EnableExtensions
rem Run from the project directory, e.g. C:\path\to\arena-opencode.bat
rem Arena session must already be signed in. OpenCode must be in PATH.
if /I "%CD%"=="%SystemRoot%\System32" cd /d "%~dp0"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -m arena_api.compat.opencode_server %*
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python 3 was not found in PATH. Install Python and arena-api first.
        exit /b 1
    )
    python -m arena_api.compat.opencode_server %*
)
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo Arena OpenCode launcher exited with code %RESULT%.
exit /b %RESULT%
