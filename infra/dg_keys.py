"""
Deepgram API key pool with automatic rotation.

Several Deepgram keys can be active at once (DEEPGRAM_API_KEYS env, or set
remotely from the controller bot). Callers ask the pool for the current
healthy key; when Deepgram rejects one (401/403 auth, 402/429 quota) the
caller marks it bad and the pool hands out the next key on the following
request. Bad keys recover automatically after a cooldown.

The pool is a process-wide singleton (`get_pool()`) shared by the TTS
generator, the STT websocket and the Voice Agent connect.
"""
import re
import threading
import time

from loguru import logger


def parse_keys(raw: str) -> list[str]:
    """Split a comma/space/newline separated blob into clean key strings."""
    if not raw:
        return []
    return [k.strip() for k in re.split(r"[,\s;]+", str(raw)) if k.strip()]


def mask_key(key: str) -> str:
    """ab12...yz — safe to show in logs / Telegram."""
    key = key or ""
    if len(key) <= 8:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-2:]}"


class DGKeyPool:
    """Round-robin pool of Deepgram keys with per-key cooldowns."""

    AUTH_COOLDOWN_SEC = 3600   # 401/403 — key is dead or suspended
    QUOTA_COOLDOWN_SEC = 300   # 402/429 — balance/rate limit, retry soon

    def __init__(self, keys: list[str] | None = None):
        self._lock = threading.Lock()
        self._keys: list[str] = []
        self._bad: dict[str, float] = {}  # key -> cooldown-until (epoch)
        self._idx = 0
        if keys:
            self.set_keys(keys)

    # ── management ────────────────────────────────────────────────────

    def set_keys(self, keys: list[str]):
        keys = [k.strip() for k in keys if k and k.strip()]
        with self._lock:
            changed = keys != self._keys
            self._keys = keys
            self._bad = {k: v for k, v in self._bad.items() if k in keys}
            self._idx = 0
        if changed:
            logger.info(
                f"Deepgram key pool updated: {len(keys)} key(s) "
                f"({', '.join(mask_key(k) for k in keys) or 'none'})"
            )

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._keys)

    # ── usage ─────────────────────────────────────────────────────────

    def current(self) -> str:
        """Next healthy key (round-robin), or '' when the pool is empty.

        If every key is cooling down, the one whose cooldown ends soonest
        is returned — a degraded key beats no key at all.
        """
        with self._lock:
            if not self._keys:
                return ""
            now = time.time()
            n = len(self._keys)
            for i in range(n):
                key = self._keys[(self._idx + i) % n]
                if self._bad.get(key, 0) <= now:
                    self._idx = (self._idx + i + 1) % n
                    return key
            soonest = min(self._keys, key=lambda k: self._bad.get(k, 0))
            return soonest

    def mark_bad(
        self,
        key: str,
        reason: str = "auth",
        cooldown_sec: float | None = None,
    ):
        """Put a key on cooldown after Deepgram rejected it."""
        if cooldown_sec is None:
            cooldown_sec = (
                self.QUOTA_COOLDOWN_SEC if reason == "quota"
                else self.AUTH_COOLDOWN_SEC
            )
        with self._lock:
            if key not in self._keys:
                return
            self._bad[key] = time.time() + cooldown_sec
            left = sum(
                1 for k in self._keys if self._bad.get(k, 0) <= time.time()
            )
        logger.warning(
            f"Deepgram key {mask_key(key)} rejected ({reason}) — "
            f"cooldown {cooldown_sec:.0f}s, {left}/{len(self._keys)} "
            f"key(s) still healthy"
        )

    def mark_good(self, key: str):
        with self._lock:
            self._bad.pop(key, None)

    # ── introspection ─────────────────────────────────────────────────

    def status(self) -> list[dict]:
        """Per-key health for dashboards / the bot settings screen."""
        now = time.time()
        with self._lock:
            return [
                {
                    "key": mask_key(k),
                    "ok": self._bad.get(k, 0) <= now,
                    "cooldown_left": max(0, int(self._bad.get(k, 0) - now)),
                }
                for k in self._keys
            ]


def classify_http_status(status: int) -> str | None:
    """Map an HTTP status to a cooldown reason ('auth'/'quota') or None."""
    if status in (401, 403):
        return "auth"
    if status in (402, 429):
        return "quota"
    return None


def looks_like_auth_error(exc: BaseException) -> str | None:
    """Classify a websocket/connect exception as 'auth'/'quota'/None."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    if status:
        reason = classify_http_status(int(status))
        if reason:
            return reason
    text = f"{type(exc).__name__}: {exc}".lower()
    if "401" in text or "403" in text or "unauthor" in text or "forbidden" in text \
            or "invalid credentials" in text or "invalid api key" in text:
        return "auth"
    if "402" in text or "429" in text or "quota" in text or "insufficient" in text \
            or "rate limit" in text or "out of credits" in text:
        return "quota"
    return None


_POOL: DGKeyPool | None = None
_POOL_LOCK = threading.Lock()


def get_pool() -> DGKeyPool:
    """Process-wide pool, seeded from config on first use."""
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            import config

            keys = list(getattr(config, "DEEPGRAM_API_KEYS", None) or [])
            if not keys:
                single = getattr(config, "DEEPGRAM_API_KEY", "")
                keys = [single] if single else []
            _POOL = DGKeyPool(keys)
    return _POOL
