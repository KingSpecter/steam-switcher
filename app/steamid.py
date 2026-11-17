"""SteamID conversions.

Steam uses more than one ID form in its own files: loginusers.vdf keys and
appmanifest LastOwner use SteamID64, while userdata/ folder names and the registry's
ActiveUser use the 32-bit account ID.
"""

from __future__ import annotations

# Base of the "individual account, public universe" SteamID64 range.
STEAM64_BASE = 76561197960265728


def to_steam64(account_id: int | str) -> int:
    """32-bit account ID to SteamID64."""
    return int(account_id) + STEAM64_BASE


def to_account_id(steam64: int | str) -> int:
    """SteamID64 to 32-bit account ID (the userdata/ folder name)."""
    return int(steam64) - STEAM64_BASE


def is_valid_steam64(steam64: int | str) -> bool:
    try:
        value = int(steam64)
    except (TypeError, ValueError):
        return False
    # Upper bound is the end of the 32-bit account ID space.
    return STEAM64_BASE < value < STEAM64_BASE + 0xFFFFFFFF


def to_steam2(steam64: int | str) -> str:
    """SteamID64 to the old STEAM_0:Y:Z form (still used by Source servers)."""
    account_id = to_account_id(steam64)
    return f"STEAM_0:{account_id & 1}:{account_id >> 1}"


def to_steam3(steam64: int | str) -> str:
    """SteamID64 to the [U:1:X] form."""
    return f"[U:1:{to_account_id(steam64)}]"


def profile_url(steam64: int | str) -> str:
    return f"https://steamcommunity.com/profiles/{steam64}"


def all_formats(steam64: int | str) -> dict[str, str]:
    """Every representation of one account, for the UI's detail panel."""
    value = int(steam64)
    return {
        "steam64": str(value),
        "account_id": str(to_account_id(value)),
        "steam2": to_steam2(value),
        "steam3": to_steam3(value),
        "profile_url": profile_url(value),
    }
