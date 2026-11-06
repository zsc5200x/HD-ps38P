"""RFC 6238 TOTP helper for the PS38 second factor.

Only the standard library is used so the app keeps its small dependency set.
The secret is read from the per-account credentials file (0600) and never
written to logs or exposed through the HTTP API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Optional


def normalize_secret(secret: str) -> str:
    return str(secret or "").replace(" ", "").replace("-", "").strip().upper()


def totp(secret: str, *, digits: int = 6, period: int = 30, at: Optional[float] = None) -> str:
    """Return the current TOTP code for a base32 secret."""
    raw = normalize_secret(secret)
    if not raw:
        raise ValueError("TOTP 密钥为空")
    padding = "=" * ((8 - len(raw) % 8) % 8)
    key = base64.b32decode(raw + padding)
    counter = int((at if at is not None else time.time()) // max(1, period))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def remaining_seconds(period: int = 30, at: Optional[float] = None) -> int:
    now = at if at is not None else time.time()
    return max(0, period - int(now) % max(1, period))
