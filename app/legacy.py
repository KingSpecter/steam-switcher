"""Importer for the old Steam.bat account list.

Reads the `SET acc_username[N]=` / `SET acc_password[N]=` pairs out of the
legacy batch file so the switch list carries over. Passwords are reported but
never stored unless the user explicitly opts in, and every imported password is
flagged: a password kept in plain text should be rotated rather than reused.
"""

from __future__ import annotations

import os
import re
from typing import Any

_USERNAME = re.compile(r"^\s*set\s+acc_username\[(\d+)\]\s*=\s*(.*?)\s*$", re.IGNORECASE)
_PASSWORD = re.compile(r"^\s*set\s+acc_password\[(\d+)\]\s*=\s*(.*?)\s*$", re.IGNORECASE)

# The placeholder rows shipped with the original script.
_PLACEHOLDERS = {"changeme", "username", "password", "", "null"}


def find_legacy_files(folder: str) -> list[str]:
    """Locate candidate legacy scripts next to the app."""
    found = []
    for root, _dirs, files in os.walk(folder):
        # Never descend into our own data or virtualenv folders.
        if any(part in root.lower() for part in (".git", "__pycache__", "node_modules")):
            continue
        for name in files:
            if name.lower().endswith((".bat", ".cmd")):
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        head = handle.read(8192)
                    if "acc_username" in head.lower():
                        found.append(path)
                except OSError:
                    continue
    return found


def parse(path: str) -> dict[str, Any]:
    """Extract accounts from a legacy batch file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return {"ok": False, "error": str(exc), "accounts": []}

    usernames: dict[int, str] = {}
    passwords: dict[int, str] = {}
    for line in lines:
        match = _USERNAME.match(line)
        if match:
            # Later duplicate slots overwrite earlier ones, matching how batch
            # itself evaluates repeated SET statements.
            usernames[int(match.group(1))] = match.group(2)
            continue
        match = _PASSWORD.match(line)
        if match:
            passwords[int(match.group(1))] = match.group(2)

    accounts = []
    for slot in sorted(usernames):
        username = usernames[slot].strip()
        if username.lower() in _PLACEHOLDERS:
            continue
        password = passwords.get(slot, "").strip()
        accounts.append({
            "slot": slot,
            "username": username,
            "password": password,
            "has_password": bool(password) and password.lower() not in _PLACEHOLDERS,
            # `& | > < ^ %` end or redirect a SET in cmd.exe, so any password
            # containing them was never actually stored correctly by the old script.
            "password_was_broken": bool(re.search(r"[&|<>^%]", password)),
        })

    return {"ok": True, "path": path, "accounts": accounts,
            "count": len(accounts), "error": ""}


def match_to_steam(legacy_accounts: list[dict], steam_accounts: list[dict]) -> list[dict[str, Any]]:
    """Pair legacy usernames with the accounts Steam already knows about."""
    by_name = {a["account_name"].lower(): a for a in steam_accounts}
    merged = []
    for entry in legacy_accounts:
        match = by_name.get(entry["username"].lower())
        merged.append({
            **entry,
            "matched": match is not None,
            "steam64": match["steam64"] if match else "",
            "persona_name": match["persona_name"] if match else "",
            "has_token": match["has_token"] if match else False,
        })
    return merged
