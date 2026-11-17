@echo off
REM ---- Steam Switcher launcher ----
REM Starts the app without a console window.
setlocal
cd /d "%~dp0"

REM pyw is the windowed Python launcher, so no console flashes up
where pyw >nul 2>&1 && (
    start "" pyw -3 -m app
    exit /b 0
)

where pythonw >nul 2>&1 && (
    start "" pythonw -m app
    exit /b 0
)

REM no windowed Python: run in a console so a startup error stays readable
where py >nul 2>&1 && (
    py -3 -m app
    exit /b 0
)
where python >nul 2>&1 && (
    python -m app
    exit /b 0
)

echo.
echo   Steam Switcher needs Python 3.9 or newer.
echo   Install it from https://www.python.org/downloads/  (tick "Add to PATH")
echo   or from the Microsoft Store, then run this file again.
echo.
pause
exit /b 1
