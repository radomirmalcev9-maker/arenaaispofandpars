@echo off
rem Arena + Capri: a PRIVATE, authenticated HTTPS server over Tailscale.
rem See CAPRI-SETUP.md before first use. Do not use Tailscale Funnel.
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title Arena server for Capri
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "RUN_PY="

where py >nul 2>&1
if not errorlevel 1 set "RUN_PY=py -3"
if not defined RUN_PY where python >nul 2>&1
if not defined RUN_PY if not errorlevel 1 set "RUN_PY=python"
if not defined RUN_PY goto no_python

%RUN_PY% -c "import arena_api; import httpx" >nul 2>&1
if errorlevel 1 goto no_deps

echo [capri] Starting the private Arena server. Keep this window open.
echo [capri] The PC and phone must be connected to the same Tailscale tailnet.
echo.
%RUN_PY% -m arena_api.compat.capri_server %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo [capri] The server stopped with an error. See CAPRI-SETUP.md.
pause
endlocal & exit /b %EXIT_CODE%

:no_python
echo [capri] Python 3.10 or newer was not found. Install Python, then retry.
pause
endlocal & exit /b 1

:no_deps
echo [capri] Dependencies are missing. In this folder run:
echo         %RUN_PY% -m pip install -e ".[auto,capri]"
echo [capri] Then run this batch file again.
pause
endlocal & exit /b 1
