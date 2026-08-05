"""Process inspection and silent process launching via Win32.

Shelling out to tasklist.exe costs ~100ms per poll and flashes a console
window; during a switch we poll for Steam's exit several times a second, so
this uses the Toolhelp32 snapshot API directly instead.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

__all__ = ["find_processes", "is_running", "wait_for_exit", "kill", "spawn_detached"]

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Hides the console window that CreateProcess would otherwise flash.
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),  # ULONG_PTR: pointer-sized
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


try:
    _k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _WIN32 = True
except (OSError, AttributeError):
    _WIN32 = False


def find_processes(*names: str) -> list[dict]:
    """Return [{pid, name}] for every running process matching any given name."""
    if not _WIN32:
        return []
    wanted = {name.lower() for name in names}
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    found = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in wanted:
                found.append({"pid": int(entry.th32ProcessID), "name": entry.szExeFile})
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)
    return found


def is_running(*names: str) -> bool:
    return bool(find_processes(*names))


def wait_for_exit(*names: str, timeout: float = 20.0, poll: float = 0.25) -> bool:
    """Block until no matching process remains. False means the timeout won."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(*names):
            return True
        time.sleep(poll)
    return not is_running(*names)


def kill(*names: str) -> int:
    """Force-terminate matching processes. The last resort when -shutdown hangs."""
    if not _WIN32:
        return 0
    killed = 0
    for proc in find_processes(*names):
        handle = _k32.OpenProcess(PROCESS_TERMINATE, False, proc["pid"])
        if handle:
            try:
                if _k32.TerminateProcess(handle, 1):
                    killed += 1
            finally:
                _k32.CloseHandle(handle)
    return killed


def spawn_detached(argv: list[str], cwd: str | None = None) -> int:
    """Start a program that outlives this app, with no console window.

    Steam must not be a child of the switcher: if it were, closing the switcher
    would take Steam's process tree down with it on some Windows configurations.
    """
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        argv,
        cwd=cwd if cwd and os.path.isdir(cwd) else None,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return proc.pid


def run_quiet(argv: list[str], timeout: float = 15.0) -> int:
    """Run a command to completion with no visible window; return its exit code."""
    try:
        completed = subprocess.run(
            argv,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return completed.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1
