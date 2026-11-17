"""Clipboard access with an auto-wipe timer.

Used for Steam Guard codes and, when the user opts into the vault, passwords.
A password left on the clipboard is readable by every process on the machine
and survives until something else overwrites it, so anything copied from the
vault is wiped after a short delay, and only if it is still ours, so something the
user copied in the meantime is left alone.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

__all__ = ["copy", "read", "copy_temporarily"]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

try:
    _user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    _user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _WIN32 = True
except (OSError, AttributeError):
    _WIN32 = False

_lock = threading.Lock()
_wipe_timer: threading.Timer | None = None


def _open(retries: int = 8) -> bool:
    """Other apps hold the clipboard briefly; retrying beats failing outright."""
    import time
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def copy(text: str) -> bool:
    """Put text on the clipboard. Returns False if Windows would not release it."""
    if not _WIN32:
        return False
    with _lock:
        if not _open():
            return False
        try:
            _user32.EmptyClipboard()
            data = text + "\0"
            size = len(data) * ctypes.sizeof(ctypes.c_wchar)
            handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                _kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(pointer, ctypes.create_unicode_buffer(data), size)
            _kernel32.GlobalUnlock(handle)
            if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                _kernel32.GlobalFree(handle)
                return False
            # on success the system owns the block; freeing it here would crash
            return True
        finally:
            _user32.CloseClipboard()


def read() -> str:
    if not _WIN32 or not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    with _lock:
        if not _open():
            return ""
        try:
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                _kernel32.GlobalUnlock(handle)
        except OSError:
            return ""
        finally:
            _user32.CloseClipboard()


def copy_temporarily(text: str, seconds: float = 45.0) -> bool:
    """Copy, then clear after `seconds` if the value is still ours."""
    global _wipe_timer
    if not copy(text):
        return False

    if _wipe_timer is not None:
        _wipe_timer.cancel()

    def wipe() -> None:
        if read() == text:
            copy("")

    _wipe_timer = threading.Timer(seconds, wipe)
    _wipe_timer.daemon = True
    _wipe_timer.start()
    return True
