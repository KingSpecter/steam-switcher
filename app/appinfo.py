"""Offline game-name resolution from Steam's binary appinfo.vdf cache.

Without this only installed games have names, since an appmanifest exists only
for those, and the rest of a library shows as "App 271590".

appcache/appinfo.vdf is Steam's own PICS metadata cache and holds the name of
every app the client knows about. It is a binary KeyValues stream, not the text
VDF format, so it needs its own reader. Parsing it keeps the app fully offline:
no API key, no network call, no Steam login.

Layout
------
    u32   magic          0x07564427 (v27) / ...28 / ...29
    u32   universe
    i64   string-table offset          (v29 only)
    per app, until appid == 0:
        u32 appid
        u32 size          bytes left in this entry, used to skip ahead
        u32 infoState, u32 lastUpdated, u64 picsToken
        b20 sha1(text), u32 changeNumber
        b20 sha1(binary)                (v28+)
        binary-KV blob
    v29 string table: u32 count, then count NUL-terminated UTF-8 strings

In v29 a KV key is a u32 index into that string table; earlier versions inline
the key as a NUL-terminated string.
"""

from __future__ import annotations

import json
import os
import struct
from typing import Any

from . import paths

__all__ = ["load_names", "resolve", "clear"]

MAGIC_V27 = 0x07564427
MAGIC_V28 = 0x07564428
MAGIC_V29 = 0x07564429

# Binary KeyValues type tags.
_TYPE_MAP = 0x00
_TYPE_STRING = 0x01
_TYPE_INT32 = 0x02
_TYPE_FLOAT32 = 0x03
_TYPE_POINTER = 0x04
_TYPE_WSTRING = 0x05
_TYPE_COLOR = 0x06
_TYPE_UINT64 = 0x07
_TYPE_END = 0x08
_TYPE_INT64 = 0x0A
_TYPE_END_ALT = 0x0B

_FIXED_WIDTH = {_TYPE_INT32: 4, _TYPE_FLOAT32: 4, _TYPE_POINTER: 4,
                _TYPE_COLOR: 4, _TYPE_UINT64: 8, _TYPE_INT64: 8}


class _Reader:
    """Cursor over the appinfo blob. Every read is bounds-checked."""

    __slots__ = ("data", "pos", "size")

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos
        self.size = len(data)

    def u32(self) -> int:
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i64(self) -> int:
        value = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return value

    def skip(self, count: int) -> None:
        self.pos += count

    def cstring(self) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end == -1:
            raise ValueError("unterminated string")
        raw = self.data[self.pos:end]
        self.pos = end + 1
        return raw.decode("utf-8", errors="replace")


def _read_string_table(data: bytes, offset: int) -> list[str]:
    reader = _Reader(data, offset)
    count = reader.u32()
    # A corrupt offset would otherwise allocate an enormous list.
    if count > 5_000_000:
        raise ValueError("implausible string table size")
    return [reader.cstring() for _ in range(count)]


def _find_name(reader: _Reader, limit: int, table: list[str] | None) -> str:
    """Walk one app's KV blob and return common/name.

    Returns as soon as the name is found; the caller skips to the next entry
    using the header's size field, so an early exit costs nothing.
    """
    # Path of map keys we are currently inside, lowercased.
    stack: list[str] = []

    def key_of() -> str:
        if table is not None:
            index = reader.u32()
            return table[index].lower() if 0 <= index < len(table) else ""
        return reader.cstring().lower()

    while reader.pos < limit:
        tag = reader.data[reader.pos]
        reader.pos += 1

        if tag in (_TYPE_END, _TYPE_END_ALT):
            if not stack:
                return ""
            stack.pop()
            continue

        key = key_of()

        if tag == _TYPE_MAP:
            stack.append(key)
        elif tag == _TYPE_STRING:
            value = reader.cstring()
            # only the "name" directly inside common/ is the store title
            if key == "name" and stack[-1:] == ["common"]:
                return value
        elif tag == _TYPE_WSTRING:
            end = reader.data.find(b"\x00\x00", reader.pos)
            reader.pos = limit if end == -1 else end + 2
        elif tag in _FIXED_WIDTH:
            reader.skip(_FIXED_WIDTH[tag])
        else:
            raise ValueError(f"unknown KV type 0x{tag:02x}")
    return ""


def parse_appinfo(path: str, max_apps: int = 400_000) -> dict[str, str]:
    """Return {appid: name} for every app in the cache."""
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 16:
        raise ValueError("appinfo.vdf is truncated")

    reader = _Reader(data)
    magic = reader.u32()
    if magic not in (MAGIC_V27, MAGIC_V28, MAGIC_V29):
        raise ValueError(f"unsupported appinfo magic 0x{magic:08x}")
    reader.u32()  # universe

    table: list[str] | None = None
    if magic == MAGIC_V29:
        table_offset = reader.i64()
        if 0 < table_offset < len(data):
            table = _read_string_table(data, table_offset)

    names: dict[str, str] = {}
    for _ in range(max_apps):
        if reader.pos + 8 > len(data):
            break
        appid = reader.u32()
        if appid == 0:
            break
        size = reader.u32()
        entry_end = reader.pos + size
        if size <= 0 or entry_end > len(data):
            break

        reader.skip(4 + 4 + 8 + 20 + 4)          # infoState..changeNumber
        if magic in (MAGIC_V28, MAGIC_V29):
            reader.skip(20)                      # sha1 of the binary section

        try:
            name = _find_name(reader, entry_end, table)
        except (ValueError, struct.error, IndexError):
            name = ""
        if name:
            names[str(appid)] = name

        # resync from the header size so a bad blob cannot derail the loop
        reader.pos = entry_end
    return names


## ---- Cached access ----
# parsing takes a moment, so the result is kept in memory and on disk,
# keyed on the file's mtime and size
_memory: tuple[str, dict[str, str]] | None = None


def _cache_path() -> str:
    return os.path.join(paths.data_dir(), "appnames.json")


def _signature(path: str) -> str:
    stat = os.stat(path)
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def load_names(steam_root: str) -> dict[str, str]:
    """{appid: name} for every app Steam has cached. {} if unavailable."""
    global _memory
    source = os.path.join(steam_root, "appcache", "appinfo.vdf")
    try:
        signature = _signature(source)
    except OSError:
        return {}

    if _memory is not None and _memory[0] == signature:
        return _memory[1]

    try:
        with open(_cache_path(), encoding="utf-8") as handle:
            cached = json.load(handle)
        if cached.get("signature") == signature and isinstance(cached.get("names"), dict):
            _memory = (signature, cached["names"])
            return _memory[1]
    except (OSError, ValueError):
        pass

    try:
        names = parse_appinfo(source)
    except (OSError, ValueError, struct.error):
        names = {}

    _memory = (signature, names)
    if names:
        try:
            temp = _cache_path() + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"signature": signature, "names": names}, handle)
            os.replace(temp, _cache_path())
        except OSError:
            pass
    return names


def resolve(appid: str | int, steam_root: str, fallback: str | None = None) -> str:
    name = load_names(steam_root).get(str(appid))
    return name or (fallback if fallback is not None else f"App {appid}")


def clear() -> None:
    global _memory
    _memory = None
    try:
        os.remove(_cache_path())
    except OSError:
        pass
