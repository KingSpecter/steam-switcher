"""Where this app keeps its own data.

%LOCALAPPDATA%, never the app folder: the app folder may sit in a synced folder
such as OneDrive, LOCALAPPDATA is per machine and never synced. That also matches
DPAPI, whose sealed data only opens on the machine that sealed it.
"""

from __future__ import annotations

import os

APP_DIR_NAME = "SteamSwitcher"


def data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _sub(name: str) -> str:
    path = os.path.join(data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


def backups_dir() -> str:
    return _sub("backups")


def settings_file() -> str:
    return os.path.join(data_dir(), "settings.json")


def accounts_file() -> str:
    return os.path.join(data_dir(), "accounts.json")


def history_file() -> str:
    return os.path.join(data_dir(), "history.json")


def vault_file() -> str:
    return os.path.join(data_dir(), "vault.dat")


def ui_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def browser_profile_dir() -> str:
    return _sub("window")
