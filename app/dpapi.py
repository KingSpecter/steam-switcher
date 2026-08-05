"""Windows DPAPI wrapper for the optional secret vault.

Anything sensitive this app stores (Steam Guard shared secrets, and passwords
only if the user explicitly opts in) is sealed with CryptProtectData under the
current user's Windows credentials.  The ciphertext is useless on any other
machine or under any other Windows account, so a leaked or synced vault file
does not leak the secrets - which is exactly the failure mode of the plaintext
batch file this app replaces.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

__all__ = ["encrypt", "decrypt", "encrypt_str", "decrypt_str", "available", "DPAPIError"]

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DPAPIError(RuntimeError):
    """Raised when Windows refuses to seal or unseal a blob."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

    @classmethod
    def of(cls, data: bytes) -> "_Blob":
        buf = ctypes.create_string_buffer(data, len(data))
        return cls(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def value(self) -> bytes:
        return ctypes.string_at(self.pbData, self.cbData)


try:
    _crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    _protect = _crypt32.CryptProtectData
    _protect.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(_Blob)]
    _protect.restype = wintypes.BOOL

    _unprotect = _crypt32.CryptUnprotectData
    _unprotect.argtypes = [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR),
                           ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                           wintypes.DWORD, ctypes.POINTER(_Blob)]
    _unprotect.restype = wintypes.BOOL

    _local_free = _kernel32.LocalFree
    _local_free.argtypes = [ctypes.c_void_p]
    _local_free.restype = ctypes.c_void_p

    _SUPPORTED = True
except (OSError, AttributeError):  # non-Windows, or a stripped-down system
    _SUPPORTED = False


def available() -> bool:
    """True when this machine can seal secrets. The UI hides the vault if not."""
    return _SUPPORTED


def _require() -> None:
    if not _SUPPORTED:
        raise DPAPIError("DPAPI is unavailable on this system")


def encrypt(data: bytes, entropy: bytes = b"") -> bytes:
    """Seal bytes to the current Windows user."""
    _require()
    blob_in = _Blob.of(data)
    blob_entropy = _Blob.of(entropy)
    blob_out = _Blob()
    ok = _protect(ctypes.byref(blob_in), "SteamSwitcher",
                  ctypes.byref(blob_entropy) if entropy else None,
                  None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise DPAPIError(f"CryptProtectData failed (error {ctypes.get_last_error()})")
    try:
        return blob_out.value()
    finally:
        _local_free(blob_out.pbData)


def decrypt(data: bytes, entropy: bytes = b"") -> bytes:
    """Unseal bytes previously sealed by this user on this machine."""
    _require()
    blob_in = _Blob.of(data)
    blob_entropy = _Blob.of(entropy)
    blob_out = _Blob()
    ok = _unprotect(ctypes.byref(blob_in), None,
                    ctypes.byref(blob_entropy) if entropy else None,
                    None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise DPAPIError("CryptUnprotectData failed - the vault was written by "
                         "a different Windows user or on a different machine")
    try:
        return blob_out.value()
    finally:
        _local_free(blob_out.pbData)


def encrypt_str(text: str, entropy: bytes = b"") -> str:
    """Seal a string into a base64 blob safe to store in JSON."""
    return base64.b64encode(encrypt(text.encode("utf-8"), entropy)).decode("ascii")


def decrypt_str(blob: str, entropy: bytes = b"") -> str:
    return decrypt(base64.b64decode(blob), entropy).decode("utf-8")
