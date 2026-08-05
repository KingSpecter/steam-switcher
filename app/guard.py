"""Steam Guard two-factor code generation.

Steam uses a TOTP variant: standard HMAC-SHA1 dynamic truncation, but the
truncated integer is rendered in base-26 over Valve's own alphabet instead of
as decimal digits.  Feeding it a shared_secret (the one inside a Steam Desktop
Authenticator .maFile) reproduces the same 5-character code the phone app shows.

This is optional - the token-based switch never triggers a Guard prompt.  It
exists for the first login on a new account, where Steam does ask.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
import time

__all__ = ["generate_code", "seconds_remaining", "validate_secret", "STEP_SECONDS"]

# Valve's code alphabet: digits and letters that cannot be misread for each
# other (no 0/O, 1/I/L, 5/S, 8/B, U/V).
_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"
_CODE_LENGTH = 5
STEP_SECONDS = 30


# A Steam shared_secret is the 20-byte key for HMAC-SHA1. Requiring a sensible
# minimum rejects pasted junk that merely happens to be decodable - "zzzz" is
# valid base64 for three bytes and would otherwise produce plausible codes.
_MIN_KEY_BYTES = 16


def _decode_secret(shared_secret: str) -> bytes:
    """Accept the secret as base64 (normal) or hex (some exporters)."""
    secret = shared_secret.strip().replace(" ", "")
    if not secret:
        raise ValueError("shared secret is empty")

    # Hex must be tried first. A 40-character hex string is also syntactically
    # valid base64, so testing base64 first would silently decode it to the
    # wrong 30 bytes. Real base64 secrets are 28 chars ending in '=', which is
    # not a hex digit, so this check can never steal one.
    if len(secret) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in secret):
        raw = bytes.fromhex(secret)
        if len(raw) >= _MIN_KEY_BYTES:
            return raw

    try:
        raw = base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("shared secret is not valid base64 or hex") from exc

    if len(raw) < _MIN_KEY_BYTES:
        raise ValueError(f"shared secret is only {len(raw)} bytes; "
                         f"a Steam shared_secret decodes to 20")
    return raw


def generate_code(shared_secret: str, timestamp: float | None = None) -> str:
    """Return the 5-character Steam Guard code for the given moment."""
    key = _decode_secret(shared_secret)
    counter = int(timestamp if timestamp is not None else time.time()) // STEP_SECONDS
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()

    # Dynamic truncation: the low nibble of the last byte picks the 4-byte window.
    offset = digest[19] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF

    code = []
    for _ in range(_CODE_LENGTH):
        code.append(_ALPHABET[value % len(_ALPHABET)])
        value //= len(_ALPHABET)
    return "".join(code)


def seconds_remaining(timestamp: float | None = None) -> int:
    """Seconds until the current code rolls over - drives the countdown ring."""
    now = timestamp if timestamp is not None else time.time()
    return STEP_SECONDS - int(now) % STEP_SECONDS


def validate_secret(shared_secret: str) -> bool:
    """True when the secret is well-formed enough to produce codes."""
    try:
        _decode_secret(shared_secret)
        generate_code(shared_secret)
        return True
    except (ValueError, TypeError):
        return False
