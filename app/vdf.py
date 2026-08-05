"""Valve KeyValues (VDF) text parser and serializer.

Steam stores loginusers.vdf, libraryfolders.vdf, localconfig.vdf and the
appmanifest_*.acf files in this format.  We need a parser that round-trips
byte-for-byte against Steam's own writer, because we rewrite loginusers.vdf
in place and a malformed file locks the user out of every account.

Steam's writer emits:
    "key"
    {
        "subkey"<TAB><TAB>"value"
    }
with TAB indentation and CRLF-free LF line endings.
"""

from __future__ import annotations

from typing import Any

__all__ = ["loads", "dumps", "load", "save", "VDFError"]


class VDFError(ValueError):
    """Raised when a VDF document cannot be parsed."""


# Escapes Valve uses in the "escaped" KeyValues dialect that Steam writes.
_UNESCAPE = {"n": "\n", "t": "\t", "v": "\v", "b": "\b", "r": "\r",
             "f": "\f", "\\": "\\", '"': '"', "'": "'", "?": "?"}
_ESCAPE = {"\n": "\\n", "\t": "\\t", "\v": "\\v", "\b": "\\b", "\r": "\\r",
           "\f": "\\f", "\\": "\\\\", '"': '\\"'}


def _escape(text: str) -> str:
    return "".join(_ESCAPE.get(ch, ch) for ch in text)


class _Parser:
    def __init__(self, text: str) -> None:
        # A UTF-8 BOM is legal in Steam's files and must not become part of a key.
        self.s = text.lstrip("﻿")
        self.i = 0
        self.n = len(self.s)

    def _line(self) -> int:
        return self.s.count("\n", 0, self.i) + 1

    def _fail(self, msg: str) -> None:
        raise VDFError(f"{msg} at line {self._line()}")

    def _skip(self) -> None:
        """Consume whitespace, // comments, and [$CONDITION] platform guards."""
        while self.i < self.n:
            ch = self.s[self.i]
            if ch in " \t\r\n":
                self.i += 1
            elif ch == "/" and self.s.startswith("//", self.i):
                nl = self.s.find("\n", self.i)
                self.i = self.n if nl == -1 else nl + 1
            elif ch == "[":
                end = self.s.find("]", self.i)
                self.i = self.n if end == -1 else end + 1
            else:
                return

    def _token(self) -> str:
        """Read one quoted or bare token."""
        ch = self.s[self.i]
        if ch == '"':
            self.i += 1
            out = []
            while self.i < self.n:
                c = self.s[self.i]
                if c == "\\" and self.i + 1 < self.n:
                    nxt = self.s[self.i + 1]
                    # Unknown escapes are kept literally, which is what Valve does.
                    out.append(_UNESCAPE.get(nxt, "\\" + nxt))
                    self.i += 2
                elif c == '"':
                    self.i += 1
                    return "".join(out)
                else:
                    out.append(c)
                    self.i += 1
            self._fail("unterminated quoted string")
        start = self.i
        while self.i < self.n and self.s[self.i] not in ' \t\r\n"{}':
            self.i += 1
        if start == self.i:
            self._fail(f"unexpected character {ch!r}")
        return self.s[start:self.i]

    def parse(self) -> dict[str, Any]:
        root: dict[str, Any] = {}
        self._skip()
        while self.i < self.n:
            # Some Steam files (appworkshop, sharedconfig) omit the outer braces
            # and just list top-level pairs, so parse repeatedly until exhausted.
            key = self._token()
            self._skip()
            if self.i < self.n and self.s[self.i] == "{":
                self.i += 1
                root[key] = self._block()
            else:
                root[key] = self._token()
            self._skip()
        return root

    def _block(self) -> dict[str, Any]:
        node: dict[str, Any] = {}
        while True:
            self._skip()
            if self.i >= self.n:
                self._fail("unterminated block")
            if self.s[self.i] == "}":
                self.i += 1
                return node
            key = self._token()
            self._skip()
            if self.i >= self.n:
                self._fail("key without value")
            if self.s[self.i] == "{":
                self.i += 1
                node[key] = self._block()
            elif self.s[self.i] == "}":
                # A key with no value; Valve treats it as empty rather than an error.
                node[key] = ""
            else:
                node[key] = self._token()


def loads(text: str) -> dict[str, Any]:
    """Parse a VDF document into nested dicts."""
    return _Parser(text).parse()


def dumps(data: dict[str, Any], indent: int = 0) -> str:
    """Serialize nested dicts back to Steam's exact on-disk layout."""
    pad = "\t" * indent
    out = []
    for key, value in data.items():
        if isinstance(value, dict):
            out.append(f'{pad}"{_escape(str(key))}"\n{pad}{{\n')
            out.append(dumps(value, indent + 1))
            out.append(f"{pad}}}\n")
        else:
            out.append(f'{pad}"{_escape(str(key))}"\t\t"{_escape(str(value))}"\n')
    return "".join(out)


def load(path: str) -> dict[str, Any]:
    """Read and parse a VDF file, tolerating the mixed encodings Steam writes."""
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return loads(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return loads(raw.decode("utf-8", errors="replace"))


def save(path: str, data: dict[str, Any]) -> None:
    """Write a VDF file using LF endings, as Steam does."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(data))
