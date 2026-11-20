@echo off
REM otherwake -- one-click launcher.
REM
REM Double-click this. It will:
REM   1. Find a Python 3.10+ on your machine.
REM   2. Auto-detect your ComfyUI install and write config.json.
REM   3. Start ComfyUI if it isn't already running.
REM   4. Start the viewer (web UI) on port 8787.
REM   5. Open the viewer in your default browser.
REM
REM Close this window (or Ctrl+C) to stop the viewer. ComfyUI runs in its
REM own window and keeps going so segments mid-generation don't get killed.

setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM ---- find a Python 3.10+ ----
set "PY="

where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto found_py
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto found_py
)

if exist "config.json" (
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content -Raw -Encoding UTF8 config.json | ConvertFrom-Json).comfy_root" 2^>nul') do set "COMFY_ROOT=%%i"
    if exist "%COMFY_ROOT%\python_embeded\python.exe" (
        set "PY=%COMFY_ROOT%\python_embeded\python.exe"
        goto found_py
    )
)

for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
) do (
    if exist %%p (
        set "PY=%%~p"
        goto found_py
    )
)

echo.
echo === Python not found ===
echo.
echo otherwake needs Python 3.10 or newer.
echo.
echo Install from: https://www.python.org/downloads/
echo IMPORTANT: during install, check "Add Python to PATH".
echo Then double-click this file again.
echo.
pause
exit /b 1

:found_py
set PYTHONIOENCODING=utf-8
echo Using Python: %PY%
%PY% "driver\_launch.py" %*
set RC=%errorlevel%

if not "%RC%"=="0" (
    echo.
    echo (launcher ended with code %RC% -- press any key to close^)
    pause >nul
)
exit /b %RC%
