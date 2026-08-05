"""Local HTTP API and static host for the UI.

Binds to 127.0.0.1 on an ephemeral port and gates every request on a random
per-run token. That matters more than usual here: the API can read vault
secrets and restart Steam, so it must not be drivable by any other process
that happens to guess the port.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import __version__, clipboard, dpapi, guard, legacy, paths, steam, steamid, store, winproc

COOKIE_NAME = "ss_session"

_routes: dict[tuple[str, str], Callable] = {}
_dynamic: list[tuple[str, str, Callable]] = []


def route(method: str, path: str):
    """Register an exact-match route."""
    def wrap(fn):
        _routes[(method, path)] = fn
        return fn
    return wrap


def prefix_route(method: str, path_prefix: str):
    """Register a route matching everything under a prefix (the tail is passed in)."""
    def wrap(fn):
        _dynamic.append((method, path_prefix, fn))
        return fn
    return wrap


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AppState:
    """Everything the request handlers share, so nothing lives in globals."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.jobs: dict[str, steam.SwitchJob] = {}
        self.started = time.time()
        self.should_quit = threading.Event()
        self.lock = threading.Lock()
        self._paths: dict[str, str] | None = None
        self._paths_error = ""

    def steam_paths(self, refresh: bool = False) -> dict[str, str]:
        with self.lock:
            if self._paths is None or refresh:
                override = store.get_settings().get("steam_path_override") or None
                try:
                    self._paths = steam.discover(override)
                    self._paths_error = ""
                except steam.SteamNotFound as exc:
                    self._paths = None
                    self._paths_error = str(exc)
            if self._paths is None:
                raise ApiError(self._paths_error or "Steam not found", 503)
            return self._paths


STATE = AppState()


# ==========================================================================
# API handlers
# ==========================================================================
@route("GET", "/api/state")
def api_state(_body: dict, _query: dict) -> dict:
    """One call that fills the whole dashboard - the UI polls only this."""
    settings = store.get_settings()
    payload: dict[str, Any] = {
        "version": __version__,
        "settings": settings,
        "dpapi": dpapi.available(),
        "steam_found": True,
        "steam_error": "",
    }
    try:
        paths_ = STATE.steam_paths()
    except ApiError as exc:
        return {**payload, "steam_found": False, "steam_error": str(exc),
                "accounts": [], "steam": {}, "preflight": []}

    accounts = steam.read_accounts(paths_)
    meta = store.get_all_meta()
    vault = store.vault_summary()

    for account in accounts:
        sid = account["steam64"]
        account["meta"] = meta.get(sid, dict(store.DEFAULT_META))
        account["vault"] = vault.get(sid, {"password": False, "shared_secret": False})

    payload.update({
        "steam": {**steam.registry_state(),
                  "root": paths_["root"], "exe": paths_["exe"],
                  "libraries": steam.library_folders(paths_)},
        "accounts": _sort_accounts(accounts, settings.get("sort_mode", "recent")),
        "preflight": steam.preflight(paths_),
        "active_job": next((j.snapshot() for j in STATE.jobs.values()
                            if j.state in ("pending", "running")), None),
    })
    return payload


def _sort_accounts(accounts: list[dict], mode: str) -> list[dict]:
    """Favourites always float to the top; `mode` orders the rest."""
    keys = {
        "recent": lambda a: -a["last_login"],
        "name": lambda a: a["persona_name"].lower(),
        "switches": lambda a: -int(a["meta"].get("switches", 0)),
        "favorite": lambda a: int(a["meta"].get("order", 9999)),
    }
    key = keys.get(mode, keys["recent"])
    return sorted(accounts, key=lambda a: (not a["meta"].get("favorite"), key(a)))


@prefix_route("GET", "/api/games/")
def api_games(tail: str, _body: dict, query: dict) -> dict:
    if not steamid.is_valid_steam64(tail):
        raise ApiError("invalid steam id")
    limit = int(query.get("limit", ["12"])[0])
    account_id = steamid.to_account_id(tail)
    return steam.account_games(account_id, STATE.steam_paths(), limit=limit)


@route("POST", "/api/switch")
def api_switch(body: dict, _query: dict) -> dict:
    steam64 = str(body.get("steam64", ""))
    if not steamid.is_valid_steam64(steam64):
        raise ApiError("invalid steam id")

    with STATE.lock:
        if any(j.state in ("pending", "running") for j in STATE.jobs.values()):
            raise ApiError("a switch is already running", 409)

    settings = store.get_settings()
    started = time.time()

    def on_done(snapshot: dict) -> None:
        ok = snapshot["state"] == "done"
        account = snapshot.get("account") or {}
        store.add_history({
            "steam64": steam64,
            "persona": account.get("persona_name", ""),
            "account_name": account.get("account_name", ""),
            "ok": ok,
            "duration": round(time.time() - started, 1),
            "error": snapshot.get("error", ""),
            "app_id": body.get("app_id") or "",
        })
        if ok:
            store.record_switch(steam64)
            # The password is only touched when Steam is actually going to ask
            # for one, and it goes to the clipboard - never onto a command line.
            if not account.get("has_token") and settings.get("auto_copy_password"):
                password = store.get_secret(steam64, "password")
                if password:
                    clipboard.copy_temporarily(password, seconds=60)

    job = steam.SwitchJob(
        steam64,
        app_id=str(body["app_id"]) if body.get("app_id") else None,
        silent=bool(body.get("silent", settings.get("launch_silent"))),
        big_picture=bool(body.get("big_picture", settings.get("launch_big_picture"))),
        paths=STATE.steam_paths(),
        on_done=on_done,
    ).start()

    with STATE.lock:
        STATE.jobs[job.id] = job
        for stale in [k for k, v in STATE.jobs.items() if v.state in ("done", "error")][:-8]:
            STATE.jobs.pop(stale, None)
    return job.snapshot()


@prefix_route("GET", "/api/switch/")
def api_switch_status(tail: str, _body: dict, _query: dict) -> dict:
    job = STATE.jobs.get(tail)
    if job is None:
        raise ApiError("unknown job", 404)
    return job.snapshot()


@prefix_route("POST", "/api/meta/")
def api_meta(tail: str, body: dict, _query: dict) -> dict:
    if not steamid.is_valid_steam64(tail):
        raise ApiError("invalid steam id")
    return {"ok": True, "meta": store.set_meta(tail, body)}


@route("POST", "/api/reorder")
def api_reorder(body: dict, _query: dict) -> dict:
    order = [str(s) for s in body.get("order", []) if steamid.is_valid_steam64(s)]
    store.reorder(order)
    return {"ok": True}


@route("POST", "/api/settings")
def api_settings(body: dict, _query: dict) -> dict:
    settings = store.update_settings(body)
    if "steam_path_override" in body:
        try:
            STATE.steam_paths(refresh=True)
        except ApiError as exc:
            return {"ok": False, "settings": settings, "message": str(exc)}
    return {"ok": True, "settings": settings}


@route("GET", "/api/health")
def api_health(_body: dict, _query: dict) -> dict:
    try:
        return {"issues": steam.health_report(STATE.steam_paths())}
    except ApiError:
        return {"issues": steam.health_report(None)}


@route("POST", "/api/fix")
def api_fix(body: dict, _query: dict) -> dict:
    return steam.apply_fix(str(body.get("code", "")), STATE.steam_paths())


@route("GET", "/api/backups")
def api_backups(_body: dict, _query: dict) -> dict:
    return {"backups": steam.list_backups()}


@route("POST", "/api/backup")
def api_backup(_body: dict, _query: dict) -> dict:
    return steam.backup_now(STATE.steam_paths(), label="manual")


@route("POST", "/api/restore")
def api_restore(body: dict, _query: dict) -> dict:
    return steam.restore_backup(str(body.get("name", "")), STATE.steam_paths())


@route("GET", "/api/history")
def api_history(_body: dict, _query: dict) -> dict:
    return {"history": store.get_history(), "stats": store.stats()}


@route("POST", "/api/history/clear")
def api_history_clear(_body: dict, _query: dict) -> dict:
    store.clear_history()
    return {"ok": True}


@prefix_route("GET", "/api/guard/")
def api_guard(tail: str, _body: dict, _query: dict) -> dict:
    """Return the live Steam Guard code, if a shared secret is stored."""
    if not steamid.is_valid_steam64(tail):
        raise ApiError("invalid steam id")
    secret = store.get_secret(tail, "shared_secret")
    if not secret:
        return {"has_secret": False, "code": "", "remaining": 0}
    try:
        return {"has_secret": True, "code": guard.generate_code(secret),
                "remaining": guard.seconds_remaining(), "period": guard.STEP_SECONDS}
    except ValueError as exc:
        raise ApiError(f"stored secret is invalid: {exc}") from exc


@route("POST", "/api/vault")
def api_vault_set(body: dict, _query: dict) -> dict:
    steam64 = str(body.get("steam64", ""))
    kind = str(body.get("kind", ""))
    if not steamid.is_valid_steam64(steam64):
        raise ApiError("invalid steam id")
    if kind == "shared_secret" and body.get("value") and not guard.validate_secret(str(body["value"])):
        raise ApiError("that is not a valid Steam Guard shared secret")
    if not dpapi.available():
        raise ApiError("this system cannot seal secrets", 503)
    store.set_secret(steam64, kind, str(body.get("value", "")))
    return {"ok": True, "vault": store.vault_summary().get(steam64, {})}


@route("POST", "/api/vault/copy")
def api_vault_copy(body: dict, _query: dict) -> dict:
    """Copy a stored secret to the clipboard; it self-wipes shortly after."""
    steam64 = str(body.get("steam64", ""))
    kind = str(body.get("kind", "password"))
    if not steamid.is_valid_steam64(steam64):
        raise ApiError("invalid steam id")
    value = store.get_secret(steam64, kind)
    if not value:
        raise ApiError("nothing stored for that account", 404)
    seconds = 45 if kind == "password" else 30
    if not clipboard.copy_temporarily(value, seconds=seconds):
        raise ApiError("the clipboard is locked by another program", 503)
    return {"ok": True, "wipes_in": seconds}


@route("POST", "/api/clipboard")
def api_clipboard(body: dict, _query: dict) -> dict:
    ok = clipboard.copy(str(body.get("text", "")))
    return {"ok": ok}


@route("POST", "/api/forget")
def api_forget(body: dict, _query: dict) -> dict:
    steam64 = str(body.get("steam64", ""))
    if not steamid.is_valid_steam64(steam64):
        raise ApiError("invalid steam id")
    store.forget_account(steam64)
    return {"ok": True}


@route("GET", "/api/legacy/scan")
def api_legacy_scan(_body: dict, _query: dict) -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = legacy.find_legacy_files(root)
    if not files:
        return {"found": False, "files": [], "accounts": []}
    parsed = legacy.parse(files[0])
    try:
        steam_accounts = steam.read_accounts(STATE.steam_paths())
    except ApiError:
        steam_accounts = []
    return {
        "found": True,
        "files": files,
        "path": parsed["path"],
        "accounts": legacy.match_to_steam(parsed["accounts"], steam_accounts),
    }


@route("POST", "/api/legacy/import")
def api_legacy_import(body: dict, _query: dict) -> dict:
    """Import labels and, only if asked, seal the legacy passwords."""
    entries = body.get("accounts") or []
    save_passwords = bool(body.get("save_passwords")) and dpapi.available()
    imported = notes = secrets_saved = 0

    for entry in entries:
        steam64 = str(entry.get("steam64", ""))
        if not steamid.is_valid_steam64(steam64):
            continue
        imported += 1
        if entry.get("label"):
            store.set_meta(steam64, {"label": str(entry["label"])})
            notes += 1
        if save_passwords and entry.get("password"):
            store.set_secret(steam64, "password", str(entry["password"]))
            secrets_saved += 1

    return {"ok": True, "imported": imported, "labels": notes,
            "passwords_sealed": secrets_saved}


@route("POST", "/api/steam/shutdown")
def api_steam_shutdown(_body: dict, _query: dict) -> dict:
    return steam.shutdown_steam(timeout=float(store.get_settings()["shutdown_timeout"]))


@route("POST", "/api/steam/launch")
def api_steam_launch(body: dict, _query: dict) -> dict:
    paths_ = STATE.steam_paths()
    if winproc.is_running("steam.exe"):
        return {"ok": True, "message": "Steam is already running."}
    argv = [paths_["exe"]]
    if body.get("app_id"):
        argv += ["-applaunch", str(body["app_id"])]
    winproc.spawn_detached(argv, cwd=paths_["root"])
    return {"ok": True, "message": "Steam is starting."}


@route("POST", "/api/open")
def api_open(body: dict, _query: dict) -> dict:
    """Open a Steam profile or a local folder in the shell."""
    target = str(body.get("target", ""))
    kind = str(body.get("kind", "url"))
    if kind == "folder":
        if not os.path.isdir(target):
            raise ApiError("no such folder", 404)
        os.startfile(target)  # noqa: S606 - a validated local directory
    elif target.startswith(("https://steamcommunity.com/", "https://store.steampowered.com/",
                            "steam://")):
        os.startfile(target)  # noqa: S606 - restricted to Steam URLs
    else:
        raise ApiError("refusing to open that target")
    return {"ok": True}


@route("POST", "/api/quit")
def api_quit(_body: dict, _query: dict) -> dict:
    STATE.should_quit.set()
    return {"ok": True}


# ==========================================================================
# HTTP plumbing
# ==========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "SteamSwitcher"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep the console clean
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The UI is same-origin and fully inlined; this blocks anything else.
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _authorized(self, query: dict) -> bool:
        """Accept the token from a header, the query string, or the session cookie.

        The cookie exists because the browser fetches /style.css, /app.js and
        avatar images by itself - those requests carry no header we control, so
        without it every subresource would 401 and the page would render bare.
        """
        supplied = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        if not supplied:
            cookies = self.headers.get("Cookie") or ""
            for part in cookies.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME:
                    supplied = value
                    break
        return bool(supplied) and hmac.compare_digest(supplied, STATE.token)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > 1_000_000:
            raise ApiError("request too large", 413)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(f"invalid JSON body: {exc}") from exc

    # -- dispatch ---------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        # Reject cross-origin callers outright: a page on another site must not
        # be able to drive this API even if it somehow learned the token.
        origin = self.headers.get("Origin")
        if origin and not origin.startswith("http://127.0.0.1:"):
            self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"})
            return

        if not self._authorized(query):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad or missing token"})
            return

        try:
            if path.startswith("/avatar/"):
                self._serve_avatar(path[len("/avatar/"):])
                return
            if path.startswith("/api/"):
                self._serve_api(method, path, query)
                return
            self._serve_static(path)
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except steam.SteamNotFound as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except Exception as exc:  # never leave the UI hanging on a 500
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": f"{type(exc).__name__}: {exc}"})

    def _serve_api(self, method: str, path: str, query: dict) -> None:
        body = self._read_body() if method == "POST" else {}

        handler = _routes.get((method, path))
        if handler is not None:
            self._json(HTTPStatus.OK, handler(body, query))
            return

        for route_method, route_prefix, fn in _dynamic:
            if method == route_method and path.startswith(route_prefix):
                self._json(HTTPStatus.OK, fn(path[len(route_prefix):], body, query))
                return

        self._json(HTTPStatus.NOT_FOUND, {"error": f"no route for {method} {path}"})

    def _serve_avatar(self, name: str) -> None:
        sid = name.removesuffix(".png")
        if not steamid.is_valid_steam64(sid):
            raise ApiError("invalid avatar id", 404)
        try:
            avatar = os.path.join(STATE.steam_paths()["avatars"], f"{sid}.png")
            with open(avatar, "rb") as handle:
                self._send(HTTPStatus.OK, handle.read(), "image/png")
        except (OSError, ApiError):
            # The UI draws its own initials avatar when this 404s.
            self._json(HTTPStatus.NOT_FOUND, {"error": "no cached avatar"})

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        base = paths.ui_dir()
        target = os.path.normpath(os.path.join(base, name))
        # Containment check: reject anything that escapes the ui/ directory.
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        with open(target, "rb") as handle:
            body = handle.read()

        extra: dict[str, str] = {}
        if name == "index.html":
            body = body.replace(b"__SESSION_TOKEN__", STATE.token.encode("ascii"))
            # Hands the token to the browser so it can fetch the stylesheet,
            # script and avatars. SameSite=Strict keeps any other origin from
            # riding along, and the server only listens on loopback.
            extra["Set-Cookie"] = (f"{COOKIE_NAME}={STATE.token}; Path=/; "
                                   "SameSite=Strict; HttpOnly")

        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if content_type.startswith(("text/", "application/javascript")):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, body, content_type, extra)


def serve() -> tuple[ThreadingHTTPServer, str]:
    """Start the server on a free loopback port; return it and the UI URL."""
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}/?t={STATE.token}"
