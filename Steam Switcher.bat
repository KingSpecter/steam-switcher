@echo off
REM ===========================================================================
REM  Steam Switcher - launcher
REM  Starts the app with no console window. Double-click this file, or pin it
REM  to the taskbar / Start menu.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM pyw.exe is the Python launcher's windowed variant: no console flash.
where pyw >nul 2>&1 && (
    start "" pyw -3 -m app
    exit /b 0
)

where pythonw >nul 2>&1 && (
    start "" pythonw -m app
    exit /b 0
)

REM Fall back to a console-visible run so any startup error is readable.
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
