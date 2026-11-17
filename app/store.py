"""Persistent app state: settings, per-account metadata, history, and the vault.

Everything here is small JSON in %LOCALAPPDATA%, written atomically so a crash
mid-write cannot leave a truncated file. The only sensitive values are in the
vault, and those are DPAPI-sealed before they ever reach disk.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import dpapi, paths

_lock = threading.RLock()

DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "dark",                  # dark | light | auto
    "accent": "signal",               # signal | steam | violet | emerald | amber | rose
    "steam_path_override": "",
    "confirm_before_switch": True,    # skipped automatically when nothing is at risk
    "close_after_switch": False,
    "launch_silent": False,           # start Steam minimised to tray
    "launch_big_picture": False,
    "show_game_stats": True,
    "sort_mode": "recent",            # recent | name | favorite | switches
    "compact_cards": False,
    "enable_vault": False,            # opt-in: store passwords, DPAPI-sealed
    "enable_guard": True,             # opt-in: Steam Guard code generation
    "auto_copy_password": True,       # copy to clipboard when an account needs one
    "shutdown_timeout": 25,
}


def _read_json(path: str, fallback: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def _write_json(path: str, data: Any) -> None:
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


## ---- Settings ----
def get_settings() -> dict[str, Any]:
    with _lock:
        stored = _read_json(paths.settings_file(), {})
        # Merge over defaults so a settings file from an older version still loads.
        return {**DEFAULT_SETTINGS, **(stored if isinstance(stored, dict) else {})}


def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        settings = get_settings()
        for key, value in changes.items():
            if key in DEFAULT_SETTINGS:
                settings[key] = value
        _write_json(paths.settings_file(), settings)
        return settings


## ---- Per-account metadata (labels, colours, notes, ordering, usage counters) ----
DEFAULT_META: dict[str, Any] = {
    "label": "", "color": "", "note": "", "favorite": False,
    "order": 9999, "switches": 0, "last_switch": 0, "hidden": False,
}


def get_all_meta() -> dict[str, dict[str, Any]]:
    with _lock:
        stored = _read_json(paths.accounts_file(), {})
        if not isinstance(stored, dict):
            return {}
        return {sid: {**DEFAULT_META, **meta}
                for sid, meta in stored.items() if isinstance(meta, dict)}


def get_meta(steam64: str) -> dict[str, Any]:
    return get_all_meta().get(str(steam64), dict(DEFAULT_META))


def set_meta(steam64: str, changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        everything = get_all_meta()
        meta = everything.get(str(steam64), dict(DEFAULT_META))
        for key, value in changes.items():
            if key in DEFAULT_META:
                meta[key] = value
        everything[str(steam64)] = meta
        _write_json(paths.accounts_file(), everything)
        return meta


def record_switch(steam64: str) -> None:
    """Bump the per-account counters that power the 'most used' sort."""
    with _lock:
        meta = get_meta(steam64)
        set_meta(steam64, {"switches": int(meta.get("switches", 0)) + 1,
                           "last_switch": time.time()})


def reorder(order: list[str]) -> None:
    with _lock:
        for index, steam64 in enumerate(order):
            set_meta(steam64, {"order": index})


## ---- Switch history ----
MAX_HISTORY = 200


def get_history() -> list[dict[str, Any]]:
    stored = _read_json(paths.history_file(), [])
    return stored if isinstance(stored, list) else []


def add_history(entry: dict[str, Any]) -> None:
    with _lock:
        history = get_history()
        history.insert(0, {"at": time.time(), **entry})
        _write_json(paths.history_file(), history[:MAX_HISTORY])


def clear_history() -> None:
    with _lock:
        _write_json(paths.history_file(), [])


def stats() -> dict[str, Any]:
    history = get_history()
    successful = [h for h in history if h.get("ok")]
    durations = [h["duration"] for h in successful if isinstance(h.get("duration"), (int, float))]
    counts: dict[str, int] = {}
    for entry in successful:
        key = entry.get("persona") or entry.get("steam64", "?")
        counts[key] = counts.get(key, 0) + 1
    return {
        "total_switches": len(successful),
        "failed_switches": len(history) - len(successful),
        "avg_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
        "fastest_seconds": round(min(durations), 1) if durations else 0,
        "most_used": max(counts, key=counts.get) if counts else "",
        "last_switch": successful[0]["at"] if successful else 0,
    }


## ---- Vault (DPAPI-sealed secrets) ----
# mixed into the DPAPI entropy so a secret only decrypts for its own account
_ENTROPY_PREFIX = b"SteamSwitcher-v2:"


def _entropy(steam64: str, kind: str) -> bytes:
    return _ENTROPY_PREFIX + f"{kind}:{steam64}".encode("utf-8")


def _read_vault() -> dict[str, dict[str, str]]:
    stored = _read_json(paths.vault_file(), {})
    return stored if isinstance(stored, dict) else {}


def _write_vault(data: dict) -> None:
    _write_json(paths.vault_file(), data)


def set_secret(steam64: str, kind: str, value: str) -> None:
    """Seal one secret. kind is 'password' or 'shared_secret'."""
    if kind not in ("password", "shared_secret"):
        raise ValueError("unknown secret kind")
    with _lock:
        vault = _read_vault()
        entry = vault.setdefault(str(steam64), {})
        if value:
            entry[kind] = dpapi.encrypt_str(value, _entropy(steam64, kind))
        else:
            entry.pop(kind, None)
        if not entry:
            vault.pop(str(steam64), None)
        _write_vault(vault)


def get_secret(steam64: str, kind: str) -> str:
    """Unseal one secret, or '' when absent or sealed by another user/machine."""
    blob = _read_vault().get(str(steam64), {}).get(kind)
    if not blob:
        return ""
    try:
        return dpapi.decrypt_str(blob, _entropy(steam64, kind))
    except (dpapi.DPAPIError, ValueError):
        return ""


def has_secret(steam64: str, kind: str) -> bool:
    """Presence check that never decrypts, so it is cheap to call for every account."""
    return bool(_read_vault().get(str(steam64), {}).get(kind))


def vault_summary() -> dict[str, dict[str, bool]]:
    return {sid: {"password": "password" in entry, "shared_secret": "shared_secret" in entry}
            for sid, entry in _read_vault().items()}


def forget_account(steam64: str) -> None:
    """Erase everything this app stores about one account."""
    with _lock:
        vault = _read_vault()
        vault.pop(str(steam64), None)
        _write_vault(vault)
        everything = get_all_meta()
        everything.pop(str(steam64), None)
        _write_json(paths.accounts_file(), everything)
