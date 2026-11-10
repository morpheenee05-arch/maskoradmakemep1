"""
Time-based one-time passwords (RFC 6238) on the standard library only.

Used by the auto-login to answer Google's "Google Authenticator" 2FA step.
The seed travels with the account credentials (typically pasted as a
2fa.live URL — only the base32 seed in its last path segment is used, the
service itself is never contacted).

Nothing here logs the seed or a generated code beyond its length.
"""
import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from typing import Optional, Tuple

# A bare base32 TOTP seed: at least 16 chars of the base32 alphabet.
_SEED_RE = re.compile(r"^[A-Za-z2-7]{16,}={0,6}$")
# 2fa.live (and similar) share links — the seed is the last path segment.
_SEED_URL_RE = re.compile(
    r"https?://[^\s/]+/tok(?:en)?/([A-Za-z2-7]{16,}={0,6})", re.IGNORECASE
)


def normalize_seed(seed: Optional[str]) -> Optional[str]:
    """Canonical base32 seed (upper, no spaces/dashes, padded), else None."""
    if not seed:
        return None
    raw = re.sub(r"[\s\-]+", "", str(seed)).upper()
    if not _SEED_RE.match(raw):
        return None
    return raw + "=" * ((-len(raw)) % 8)


def extract_seed(token: Optional[str]) -> Optional[str]:
    """Pull a TOTP seed out of one pasted token.

    Accepts a bare base32 seed or a share URL whose last path segment is
    the seed (https://2fa.live/tok/<seed>). Anything else returns None.
    """
    if not token:
        return None
    text = str(token).strip().strip(",;")
    m = _SEED_URL_RE.search(text)
    if m:
        return normalize_seed(m.group(1))
    if "/" not in text and ":" not in text:
        return normalize_seed(text)
    return None


def _decode(seed: str) -> bytes:
    return base64.b32decode(seed, casefold=True)


def totp_at(seed: str, for_time: float, step: int = 30, digits: int = 6) -> str:
    """The TOTP code for an explicit timestamp (kept for tests)."""
    key = _decode(seed)
    counter = int(for_time // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp_now(seed: str, step: int = 30, digits: int = 6) -> Tuple[str, int]:
    """Current code and how many seconds it stays valid.

    The caller should not submit a code with only a second or two left —
    Google may validate it after the window rolled over.
    """
    now = time.time()
    remaining = step - int(now % step)
    return totp_at(seed, now, step=step, digits=digits), remaining


def valid(seed: Optional[str]) -> bool:
    """True when the seed decodes and produces a code."""
    norm = normalize_seed(seed)
    if not norm:
        return False
    try:
        _decode(norm)
        return True
    except (binascii.Error, ValueError):
        return False
