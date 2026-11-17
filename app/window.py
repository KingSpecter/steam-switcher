"""Opens the UI in a chromeless application window.

Every Windows 10/11 machine already ships Edge with the Chromium engine, and
`--app=` gives a real borderless window with no tabs, address bar or browser
menus. That gives a desktop window without bundling a UI toolkit or installing
anything.

A private --user-data-dir keeps this out of the user's own browser profile, and
makes the launched process the window's owner so we can exit when it closes.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import webbrowser

from . import paths

WINDOW_WIDTH = 1240
WINDOW_HEIGHT = 840


def _candidates() -> list[str]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    relative = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
        r"Vivaldi\Application\vivaldi.exe",
    ]
    found = []
    for base in (program_files_x86, program_files, local):
        if not base:
            continue
        for tail in relative:
            found.append(os.path.join(base, tail))
    found += [p for p in (shutil.which("msedge"), shutil.which("chrome")) if p]
    return found


def find_browser() -> str | None:
    return next((path for path in _candidates() if os.path.isfile(path)), None)


def _centered_position() -> tuple[int, int]:
    """Centre on the primary monitor; fall back to a sane offset."""
    try:
        user32 = ctypes.WinDLL("user32.dll")
        user32.SetProcessDPIAware()
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        return max(0, (width - WINDOW_WIDTH) // 2), max(0, (height - WINDOW_HEIGHT) // 2)
    except (OSError, AttributeError):
        return 120, 60


def open_window(url: str) -> subprocess.Popen | None:
    """Launch the app window. Returns the process, or None if we fell back."""
    browser = find_browser()
    if browser is None:
        webbrowser.open(url)
        return None

    left, top = _centered_position()
    argv = [
        browser,
        f"--app={url}",
        f"--user-data-dir={paths.browser_profile_dir()}",
        f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
        f"--window-position={left},{top}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--disable-component-update",
        # keep the private profile small: no caches or extras for a loopback page
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--disable-gpu-shader-disk-cache",
        "--disable-breakpad",
        "--disable-features=Translate,OptimizationHints,MediaRouter,"
        "EdgeShoppingAssistant,EdgeCoupons,msShoppingTrigger,EdgeDiscoverPage,"
        "EdgeSidebar,CalculateNativeWinOcclusion",
    ]
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except OSError:
        webbrowser.open(url)
        return None
