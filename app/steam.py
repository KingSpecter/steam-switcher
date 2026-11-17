"""Steam discovery, state inspection, and account switching.

How the switch actually works
-----------------------------
Modern Steam keeps a per-account refresh token on disk once you log in with
"stay signed in".  From then on it can sign that account back in with no
password and no Steam Guard prompt.  All a switcher has to do is tell Steam
which account is next:

    1. shut Steam down *gracefully* so it flushes its config and cloud saves
    2. set HKCU\\Software\\Valve\\Steam\\AutoLoginUser to the target account
    3. flip MostRecent/AutoLogin in config/loginusers.vdf to the target
    4. start Steam

No password is involved. Passing `-login <user> <pass>` instead would put the
password in the process table, where any program can read it, and current
Steam clients ignore that password argument anyway.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import threading
import time
from typing import Any, Callable

from . import appinfo, steamid, vdf, winproc

try:
    import winreg
except ImportError:  # non-Windows: the module still imports so tests can run
    winreg = None  # type: ignore[assignment]

__all__ = [
    "SteamNotFound", "discover", "registry_state", "read_accounts", "library_folders",
    "installed_apps", "account_playtime", "preflight", "health_report",
    "backup_now", "list_backups", "restore_backup", "shutdown_steam", "SwitchJob",
]

REG_STEAM = r"Software\Valve\Steam"
REG_ACTIVE = r"Software\Valve\Steam\ActiveProcess"

STEAM_PROCESSES = ("steam.exe", "steamwebhelper.exe", "steamservice.exe", "GameOverlayUI.exe")

# StateFlags bits from Valve's EAppState; only "mid-download" matters here
_STATE_UPDATE_REQUIRED = 2
_STATE_UPDATE_RUNNING = 256
_STATE_UPDATE_PAUSED = 512
_STATE_UPDATE_STARTED = 1024
_STATE_BUSY_MASK = _STATE_UPDATE_REQUIRED | _STATE_UPDATE_RUNNING | _STATE_UPDATE_STARTED

# localconfig.vdf's Playtime counter is kept by the client and can get corrupted
# (values of several hundred thousand hours have been seen). Anything past a year
# of play is shown but left out of totals.
_PLAYTIME_SANITY_MINUTES = 365 * 24 * 60


class SteamNotFound(RuntimeError):
    """Raised when Steam cannot be located on this machine."""


## ---- Parsed-file cache ----
# keyed on mtime: localconfig.vdf is ~80 KB per account and the UI polls every
# couple of seconds; edits by Steam change the mtime and are picked up at once
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached_vdf(path: str) -> dict:
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {}
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        data = vdf.load(path)
    except (OSError, vdf.VDFError):
        data = {}
    with _cache_lock:
        _cache[path] = (stamp, data)
    return data


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


## ---- Registry helpers ----
def _reg_read(subkey: str, name: str, hive=None) -> Any:
    if winreg is None:
        return None
    hive = hive or winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _reg_write(subkey: str, name: str, value: Any, kind: int) -> bool:
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, kind, value)
        return True
    except OSError:
        return False


## ---- Discovery ----
def discover(override: str | None = None) -> dict[str, str]:
    """Locate the Steam install. Registry first, then the usual install paths."""
    candidates: list[str] = []
    if override:
        candidates.append(override)

    for value in (_reg_read(REG_STEAM, "SteamPath"), _reg_read(REG_STEAM, "InstallPath")):
        if value:
            candidates.append(str(value))
    if winreg is not None:
        for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")):
            value = _reg_read(path, "InstallPath", hive)
            if value:
                candidates.append(str(value))

    program_files = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    candidates += [os.path.join(program_files, "Steam"),
                   os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
                   r"C:\Steam", r"D:\Steam"]

    for raw in candidates:
        # Steam writes its own path with forward slashes and lowercase drive letters.
        root = os.path.normpath(str(raw).replace("/", os.sep))
        exe = os.path.join(root, "steam.exe")
        if os.path.isfile(exe):
            return {
                "root": root,
                "exe": exe,
                "config_dir": os.path.join(root, "config"),
                "loginusers": os.path.join(root, "config", "loginusers.vdf"),
                "userdata": os.path.join(root, "userdata"),
                "avatars": os.path.join(root, "config", "avatarcache"),
            }
    raise SteamNotFound(
        "steam.exe was not found. Set the Steam folder manually in Settings."
    )


def registry_state() -> dict[str, Any]:
    """Live Steam state straight from the registry."""
    active_user = _reg_read(REG_ACTIVE, "ActiveUser") or 0
    pid = _reg_read(REG_ACTIVE, "pid") or 0
    running_app = _reg_read(REG_STEAM, "RunningAppID") or 0
    processes = winproc.find_processes("steam.exe")
    return {
        "running": bool(processes),
        "pid": int(pid) if processes else 0,
        "active_account_id": int(active_user),
        "active_steam64": steamid.to_steam64(active_user) if active_user else 0,
        "auto_login_user": str(_reg_read(REG_STEAM, "AutoLoginUser") or ""),
        "remember_password": int(_reg_read(REG_STEAM, "RememberPassword") or 0),
        "running_app_id": int(running_app),
        "last_game_name": str(_reg_read(REG_STEAM, "LastGameNameUsed") or ""),
        "wants_offline": bool(_reg_read(REG_STEAM, "WantsOfflineMode")),
    }


def _machine_authorized(root: str) -> bool:
    """ssfn* files in the Steam root mean this machine passed Steam Guard."""
    return bool(glob.glob(os.path.join(root, "ssfn*")))


## ---- Accounts ----
def read_accounts(paths: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every account Steam knows about, enriched with everything we can learn offline."""
    paths = paths or discover()
    state = registry_state()
    data = _cached_vdf(paths["loginusers"])

    # Steam has used both casings for the top-level key across versions.
    users = {}
    for key, value in data.items():
        if key.lower() == "users" and isinstance(value, dict):
            users = value
            break

    accounts = []
    for sid64, info in users.items():
        if not isinstance(info, dict) or not steamid.is_valid_steam64(sid64):
            continue
        account_id = steamid.to_account_id(sid64)
        account_name = str(info.get("AccountName", ""))
        avatar = os.path.join(paths["avatars"], f"{sid64}.png")
        # RememberPassword means Steam holds a refresh token: the switch needs no password
        has_token = str(info.get("RememberPassword", "0")) == "1"

        timestamp = 0
        for key in ("Timestamp", "timestamp"):
            if key in info:
                try:
                    timestamp = int(info[key])
                except (TypeError, ValueError):
                    timestamp = 0
                break

        accounts.append({
            "steam64": str(sid64),
            "account_id": account_id,
            "account_name": account_name,
            "persona_name": str(info.get("PersonaName", "")) or account_name,
            "has_token": has_token,
            "auto_login": str(info.get("AutoLogin", info.get("mostrecent", "0"))) == "1",
            "wants_offline": str(info.get("WantsOfflineMode", "0")) == "1",
            "last_login": timestamp,
            "has_avatar": os.path.isfile(avatar),
            "has_userdata": os.path.isdir(os.path.join(paths["userdata"], str(account_id))),
            "is_active": bool(state["active_account_id"]) and state["active_account_id"] == account_id,
            "ids": steamid.all_formats(sid64),
        })

    accounts.sort(key=lambda a: a["last_login"], reverse=True)
    return accounts


## ---- Games ----
def library_folders(paths: dict[str, str] | None = None) -> list[str]:
    """Every Steam library on this machine, including secondary drives."""
    paths = paths or discover()
    found = [paths["root"]]
    # the registry gives the root in lower case and libraryfolders.vdf does not,
    # so compare case-insensitively or the same folder shows up twice
    seen = {paths["root"].lower()}
    data = _cached_vdf(os.path.join(paths["config_dir"], "libraryfolders.vdf"))
    for value in (data.get("libraryfolders") or data.get("LibraryFolders") or {}).values():
        path = value.get("path") if isinstance(value, dict) else value
        if not path or not os.path.isdir(str(path)):
            continue
        path = os.path.normpath(str(path))
        if path.lower() not in seen:
            seen.add(path.lower())
            found.append(path)
    return found


def installed_apps(paths: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Installed games across all libraries, keyed by appid."""
    paths = paths or discover()
    apps: dict[str, dict[str, Any]] = {}
    for library in library_folders(paths):
        for manifest in glob.glob(os.path.join(library, "steamapps", "appmanifest_*.acf")):
            state = (_cached_vdf(manifest).get("AppState") or {})
            appid = str(state.get("appid", ""))
            if not appid:
                continue

            def as_int(key: str) -> int:
                try:
                    return int(state.get(key, 0) or 0)
                except (TypeError, ValueError):
                    return 0

            flags = as_int("StateFlags")
            to_download, downloaded = as_int("BytesToDownload"), as_int("BytesDownloaded")
            apps[appid] = {
                "appid": appid,
                "name": str(state.get("name", f"App {appid}")),
                "size_bytes": as_int("SizeOnDisk"),
                "last_played": as_int("LastPlayed"),
                "last_owner": str(state.get("LastOwner", "")),
                "library": library,
                "state_flags": flags,
                # Two independent signals; either alone produces false negatives.
                "updating": bool(flags & _STATE_BUSY_MASK) or downloaded < to_download,
                "paused": bool(flags & _STATE_UPDATE_PAUSED),
                "bytes_to_download": to_download,
                "bytes_downloaded": downloaded,
            }
    return apps


def account_playtime(account_id: int | str, paths: dict[str, str] | None = None) -> dict[str, dict[str, int]]:
    """Per-account playtime from userdata/<id>/config/localconfig.vdf."""
    paths = paths or discover()
    config = os.path.join(paths["userdata"], str(account_id), "config", "localconfig.vdf")
    data = _cached_vdf(config)

    # Walk down case-insensitively; Steam's casing here varies by client version.
    node: Any = data
    for step in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        if not isinstance(node, dict):
            return {}
        for key, value in node.items():
            if key.lower() == step.lower():
                node = value
                break
        else:
            return {}

    result = {}
    if isinstance(node, dict):
        for appid, info in node.items():
            if not isinstance(info, dict):
                continue
            try:
                minutes = int(info.get("Playtime", 0) or 0)
                last = int(info.get("LastPlayed", 0) or 0)
            except (TypeError, ValueError):
                continue
            if minutes or last:
                result[str(appid)] = {"minutes": minutes, "last_played": last}
    return result


def account_games(account_id: int | str, paths: dict[str, str] | None = None,
                  limit: int = 12) -> dict[str, Any]:
    """Top games for one account, merging playtime with install state."""
    paths = paths or discover()
    playtime = account_playtime(account_id, paths)
    installed = installed_apps(paths)
    # names for owned games that are not installed (no appmanifest for those)
    names = appinfo.load_names(paths["root"])
    sid64 = str(steamid.to_steam64(account_id))

    def name_of(appid: str) -> str:
        app = installed.get(appid)
        if app and app["name"] and not app["name"].startswith("App "):
            return app["name"]
        return names.get(appid) or f"App {appid}"

    games = []
    for appid, stats in playtime.items():
        minutes = stats["minutes"]
        games.append({
            "appid": appid,
            "name": name_of(appid),
            "minutes": minutes,
            "suspect": minutes > _PLAYTIME_SANITY_MINUTES,
            "last_played": stats["last_played"],
            "installed": appid in installed,
        })
    # Games installed by this account but never launched still belong in the list.
    for appid, app in installed.items():
        if appid not in playtime and app["last_owner"] == sid64:
            games.append({"appid": appid, "name": name_of(appid), "minutes": 0,
                          "suspect": False, "last_played": app["last_played"],
                          "installed": True})

    games.sort(key=lambda g: (g["minutes"], g["last_played"]), reverse=True)
    trusted = [g for g in games if not g["suspect"]]
    return {
        "total": len(games),
        "total_minutes": sum(g["minutes"] for g in trusted),
        "suspect_count": len(games) - len(trusted),
        "installed_count": sum(1 for g in games if g["installed"]),
        "named_count": sum(1 for g in games if not g["name"].startswith("App ")),
        "games": games[:limit],
    }


## ---- Pre-flight safety ----
def preflight(paths: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Warn about anything a restart would interrupt (a running game, a download)."""
    paths = paths or discover()
    state = registry_state()
    checks: list[dict[str, str]] = []

    if not state["running"]:
        return checks

    apps = installed_apps(paths)

    if state["running_app_id"]:
        appid = str(state["running_app_id"])
        app = apps.get(appid)
        # not LastGameNameUsed: Steam sometimes leaves the persona name in it
        name = (app["name"] if app else "") or appinfo.resolve(appid, paths["root"])
        checks.append({
            "level": "danger",
            "code": "game_running",
            "title": f"{name} is running",
            "detail": "Switching closes Steam and will end the game. Unsaved progress "
                      "and Steam Cloud sync for this session may be lost.",
        })

    downloading = [a for a in apps.values() if a["updating"] and not a["paused"]]
    if downloading:
        remaining = sum(max(0, a["bytes_to_download"] - a["bytes_downloaded"]) for a in downloading)
        names = ", ".join(a["name"] for a in downloading[:3])
        more = f" +{len(downloading) - 3} more" if len(downloading) > 3 else ""
        checks.append({
            "level": "warn",
            "code": "downloading",
            "title": f"Download in progress: {names}{more}",
            "detail": f"{_human_bytes(remaining)} still to download. Steam resumes after "
                      "the switch, but the current chunk restarts.",
        })

    if state["wants_offline"]:
        checks.append({
            "level": "warn",
            "code": "offline_mode",
            "title": "Steam is in Offline Mode",
            "detail": "Auto-login cannot complete offline. The Doctor can clear this flag.",
        })
    return checks


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def health_report(paths: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Diagnostics with machine-readable codes the Doctor panel can act on."""
    issues: list[dict[str, Any]] = []
    try:
        paths = paths or discover()
    except SteamNotFound as exc:
        return [{"level": "danger", "code": "no_steam", "title": "Steam not found",
                 "detail": str(exc), "fixable": False}]

    state = registry_state()

    if not os.path.isfile(paths["loginusers"]):
        issues.append({"level": "danger", "code": "no_loginusers",
                       "title": "loginusers.vdf is missing",
                       "detail": "Sign in to Steam once to create it.", "fixable": False})
    else:
        try:
            vdf.load(paths["loginusers"])
        except (OSError, vdf.VDFError) as exc:
            issues.append({"level": "danger", "code": "bad_loginusers",
                           "title": "loginusers.vdf is corrupt",
                           "detail": f"{exc}. Restore a backup from the History panel.",
                           "fixable": True})

    accounts = read_accounts(paths)
    tokenless = [a for a in accounts if not a["has_token"]]
    if tokenless:
        names = ", ".join(a["persona_name"] for a in tokenless[:4])
        issues.append({
            "level": "warn", "code": "no_token",
            "title": (f"{len(tokenless)} account{'s' if len(tokenless) != 1 else ''} "
                      f"need{'s' if len(tokenless) == 1 else ''} one manual sign-in: {names}"),
            "detail": "Switch to each one and sign in with \"Remember me\" ticked. After "
                      "that they switch instantly with no password and no Guard code.",
            "fixable": False,
        })

    if not _machine_authorized(paths["root"]):
        issues.append({
            "level": "info", "code": "no_ssfn",
            "title": "This machine is not Steam Guard authorized",
            "detail": "No ssfn file in the Steam folder. The first sign-in will ask for "
                      "an email or mobile code.", "fixable": False,
        })

    if state["wants_offline"]:
        issues.append({"level": "warn", "code": "offline_mode",
                       "title": "Steam is stuck in Offline Mode",
                       "detail": "Auto-login will not complete until this is cleared.",
                       "fixable": True})

    if state["running"] and not state["active_account_id"]:
        issues.append({"level": "info", "code": "logged_out",
                       "title": "Steam is running but signed out",
                       "detail": "Sitting on the login screen.", "fixable": False})

    if not accounts:
        issues.append({"level": "warn", "code": "no_accounts",
                       "title": "No accounts found",
                       "detail": "Sign in to Steam once, or import from the legacy Steam.bat.",
                       "fixable": False})
    return issues


def apply_fix(code: str, paths: dict[str, str] | None = None) -> dict[str, Any]:
    """One-click remediation for the fixable health codes."""
    paths = paths or discover()
    if code == "offline_mode":
        _reg_write(REG_STEAM, "WantsOfflineMode", 0, winreg.REG_DWORD)
        _reg_write(REG_STEAM, "AlreadyRetriedOfflineMode", 0, winreg.REG_DWORD)
        data = _cached_vdf(paths["loginusers"])
        users = next((v for k, v in data.items() if k.lower() == "users"), {})
        for info in users.values():
            if isinstance(info, dict):
                info["WantsOfflineMode"] = "0"
        if users:
            _atomic_write_vdf(paths["loginusers"], data)
        clear_cache()
        return {"ok": True, "message": "Offline Mode cleared."}
    if code == "bad_loginusers":
        backups = list_backups()
        if not backups:
            return {"ok": False, "message": "No backup available to restore."}
        return restore_backup(backups[0]["name"], paths)
    return {"ok": False, "message": f"No automatic fix for '{code}'."}


## ---- Backups ----
def _backup_dir() -> str:
    from .paths import backups_dir
    return backups_dir()


def backup_now(paths: dict[str, str] | None = None, label: str = "manual") -> dict[str, Any]:
    """Snapshot loginusers.vdf plus the registry keys we are about to change."""
    paths = paths or discover()
    folder = _backup_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}"
    target = os.path.join(folder, name)
    os.makedirs(target, exist_ok=True)

    saved = []
    if os.path.isfile(paths["loginusers"]):
        shutil.copy2(paths["loginusers"], os.path.join(target, "loginusers.vdf"))
        saved.append("loginusers.vdf")

    state = registry_state()
    with open(os.path.join(target, "registry.json"), "w", encoding="utf-8") as handle:
        json.dump({"AutoLoginUser": state["auto_login_user"],
                   "RememberPassword": state["remember_password"],
                   "created": time.time(), "label": label}, handle, indent=2)
    saved.append("registry.json")

    _prune_backups(folder)
    return {"ok": True, "name": name, "files": saved}


def _prune_backups(folder: str, keep: int = 25) -> None:
    entries = sorted((e for e in os.listdir(folder)
                      if os.path.isdir(os.path.join(folder, e))), reverse=True)
    for stale in entries[keep:]:
        shutil.rmtree(os.path.join(folder, stale), ignore_errors=True)


def list_backups() -> list[dict[str, Any]]:
    folder = _backup_dir()
    out = []
    for name in sorted(os.listdir(folder), reverse=True):
        path = os.path.join(folder, name)
        if not os.path.isdir(path):
            continue
        meta = {}
        try:
            with open(os.path.join(path, "registry.json"), encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            pass
        out.append({"name": name, "created": meta.get("created", 0),
                    "label": meta.get("label", ""),
                    "auto_login_user": meta.get("AutoLoginUser", "")})
    return out


def restore_backup(name: str, paths: dict[str, str] | None = None) -> dict[str, Any]:
    """Roll back to a snapshot. Refuses to run while Steam would overwrite it."""
    paths = paths or discover()
    source = os.path.join(_backup_dir(), os.path.basename(name))
    if not os.path.isdir(source):
        return {"ok": False, "message": "That backup no longer exists."}
    if winproc.is_running("steam.exe"):
        return {"ok": False, "message": "Close Steam first. It rewrites these files when it exits."}

    vdf_backup = os.path.join(source, "loginusers.vdf")
    if os.path.isfile(vdf_backup):
        shutil.copy2(vdf_backup, paths["loginusers"])
    try:
        with open(os.path.join(source, "registry.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        if meta.get("AutoLoginUser"):
            _reg_write(REG_STEAM, "AutoLoginUser", str(meta["AutoLoginUser"]), winreg.REG_SZ)
        _reg_write(REG_STEAM, "RememberPassword", int(meta.get("RememberPassword", 1)),
                   winreg.REG_DWORD)
    except (OSError, ValueError):
        pass
    clear_cache()
    return {"ok": True, "message": f"Restored {name}."}


## ---- Writing Steam's state ----
def _atomic_write_vdf(path: str, data: dict) -> None:
    """Write via a temp file in the same directory, then rename.

    A half-written loginusers.vdf makes Steam forget every account, so the
    file is never truncated in place.
    """
    folder = os.path.dirname(path)
    temp = os.path.join(folder, f".{os.path.basename(path)}.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(vdf.dumps(data))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _set_active_in_vdf(paths: dict[str, str], target_sid64: str) -> bool:
    """Point loginusers.vdf at one account and clear the flag on the rest."""
    if not os.path.isfile(paths["loginusers"]):
        return False
    try:
        data = vdf.load(paths["loginusers"])
    except (OSError, vdf.VDFError):
        return False

    users_key = next((k for k in data if k.lower() == "users"), None)
    if users_key is None:
        return False

    found = False
    for sid64, info in data[users_key].items():
        if not isinstance(info, dict):
            continue
        is_target = str(sid64) == str(target_sid64)
        found = found or is_target
        # different client versions read different keys, so both are written
        info["MostRecent"] = "1" if is_target else "0"
        info["AutoLogin"] = "1" if is_target else "0"
        if is_target:
            # ask Steam to keep a token so the next switch needs no password
            info["RememberPassword"] = "1"
            info["WantsOfflineMode"] = "0"
    if not found:
        return False

    _atomic_write_vdf(paths["loginusers"], data)
    clear_cache()
    return True


def shutdown_steam(timeout: float = 25.0, on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Ask Steam to exit with `-shutdown` (config saved, cloud synced); force it only if it refuses."""
    def step(message: str) -> None:
        if on_step:
            on_step(message)

    if not winproc.is_running("steam.exe"):
        step("Steam is not running")
        return {"ok": True, "forced": False, "was_running": False}

    try:
        exe = discover()["exe"]
    except SteamNotFound:
        exe = None

    if exe:
        step("Asking Steam to close (saving cloud data)")
        winproc.run_quiet([exe, "-shutdown"], timeout=10)

    if winproc.wait_for_exit("steam.exe", timeout=timeout):
        step("Steam closed cleanly")
        # steamwebhelper can outlive steam.exe briefly and hold config files open.
        winproc.wait_for_exit("steamwebhelper.exe", timeout=6)
        return {"ok": True, "forced": False, "was_running": True}

    step("Steam did not respond, forcing it to close")
    winproc.kill(*STEAM_PROCESSES)
    forced_ok = winproc.wait_for_exit("steam.exe", timeout=8)
    return {"ok": forced_ok, "forced": True, "was_running": True}


## ---- The switch, run on a worker thread with live progress ----
class SwitchJob:
    """Runs a switch on a worker thread and publishes progress for the UI to poll."""

    def __init__(self, steam64: str, *, app_id: str | None = None, silent: bool = False,
                 big_picture: bool = False, paths: dict[str, str] | None = None,
                 on_done: Callable[[dict], None] | None = None):
        self.steam64 = str(steam64)
        self.app_id = app_id
        self.silent = silent
        self.big_picture = big_picture
        self.paths = paths or discover()
        self.on_done = on_done

        self.id = f"sw{int(time.time() * 1000)}"
        self.started = time.time()
        self.state = "pending"          # pending | running | done | error
        self.steps: list[dict[str, Any]] = []
        self.error = ""
        self.account: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _step(self, message: str, status: str = "ok") -> None:
        with self._lock:
            self.steps.append({"at": time.time(), "message": message, "status": status})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id, "state": self.state, "steps": list(self.steps),
                "error": self.error, "steam64": self.steam64,
                "account": self.account, "elapsed": round(time.time() - self.started, 1),
            }

    def start(self) -> "SwitchJob":
        self._thread = threading.Thread(target=self._run, name="switch", daemon=True)
        self._thread.start()
        return self

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        self.state = "running"
        try:
            accounts = read_accounts(self.paths)
            account = next((a for a in accounts if a["steam64"] == self.steam64), None)
            if account is None:
                raise RuntimeError("That account is no longer in loginusers.vdf.")
            self.account = account

            self._step(f"Preparing switch to {account['persona_name']}")

            backup = backup_now(self.paths, label=f"switch-{account['account_name']}")
            self._step(f"Backed up current state ({backup['name']})")

            result = shutdown_steam(on_step=self._step)
            if not result["ok"]:
                raise RuntimeError("Steam would not close. Close it manually and retry.")
            if result["forced"]:
                self._step("Steam was force-closed; cloud sync may be incomplete", "warn")

            # both writes must land while Steam is stopped: it rewrites these on exit
            account_name = account["account_name"]
            if not _reg_write(REG_STEAM, "AutoLoginUser", account_name.lower(), winreg.REG_SZ):
                raise RuntimeError("Could not write AutoLoginUser to the registry.")
            _reg_write(REG_STEAM, "RememberPassword", 1, winreg.REG_DWORD)
            self._step(f"Set auto-login user to {account_name}")

            if _set_active_in_vdf(self.paths, self.steam64):
                self._step("Updated loginusers.vdf")
            else:
                self._step("Could not update loginusers.vdf; using registry only", "warn")

            argv = [self.paths["exe"]]
            if self.silent:
                argv.append("-silent")
            if self.big_picture:
                argv.append("-bigpicture")
            if not account["has_token"]:
                # username only; a password here would be readable in the process table
                argv += ["-login", account_name]
            if self.app_id:
                argv += ["-applaunch", str(self.app_id)]

            winproc.spawn_detached(argv, cwd=self.paths["root"])
            self._step("Starting Steam" + (f" and launching app {self.app_id}" if self.app_id else ""))

            if account["has_token"]:
                self._step(f"Signing in as {account['persona_name']}, no password needed")
            else:
                self._step("Steam will ask for this account's password once. Tick "
                           "\"Remember me\" and future switches are instant.", "warn")

            self.state = "done"
        except Exception as exc:  # surfaced verbatim in the UI
            self.error = str(exc)
            self.state = "error"
            self._step(str(exc), "error")
        finally:
            clear_cache()
            if self.on_done:
                try:
                    self.on_done(self.snapshot())
                except Exception:
                    pass
