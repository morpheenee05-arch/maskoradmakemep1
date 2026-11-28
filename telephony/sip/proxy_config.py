"""
Per-account proxy routing for the SIP stack.

One proxy per worker (account): worker_id=i uses proxies[i]. Stored in
`.sip_proxies.json` next to config.py so the setting survives agent restarts;
managed remotely via the bot /proxy command (job type "proxies").

Only SIP SIGNALING goes through the proxy (the WSS connection in sip_ws and
the credential-mint POST in gv_auth — everything that carries the account's
identity). Media (SRTP/UDP via aiortc) cannot be proxied: aioice has no
UDP-proxy/TURN support, exactly like Chrome's own WebRTC. The callee-side
carrier only ever sees Google's media relays, so this does not weaken the
anti-throttling effect.

Accepted line formats (one per line):
    host:port
    host:port:user:pass
    user:pass:host:port
    user:pass@host:port
    scheme://any-of-the-above
Schemes: http, https (CONNECT), socks4, socks4a, socks5, socks5h.
A line without a scheme defaults to http — the common case for
ip:port:user:pass proxy lists.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote

from loguru import logger

_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
_lock = threading.Lock()


def _store_path() -> Path:
    try:
        import config
        return config.BASE_DIR / ".sip_proxies.json"
    except Exception:
        return Path(".sip_proxies.json")


def parse_proxy(line: str) -> str:
    """Normalize one proxy line to scheme://[user:pass@]host:port.

    Raises ValueError with a human-readable reason on bad input.
    """
    s = (line or "").strip()
    if not s:
        raise ValueError("empty line")
    scheme = "http"
    if "://" in s:
        scheme, _, s = s.partition("://")
        scheme = scheme.strip().lower()
        if scheme not in _SCHEMES:
            raise ValueError(
                f"unsupported scheme {scheme!r} — use http/https/socks4/socks5"
            )
    s = s.strip().strip("/")
    if not s:
        raise ValueError("no host")

    user = password = None
    if "@" in s:
        head, _, tail = s.rpartition("@")
        # The @ is an auth separator only when the tail really looks like
        # host:port — otherwise it belongs to a password in a colon format
        # (proxy-list passwords routinely contain @ or :).
        tparts = tail.rsplit(":", 1)
        if len(tparts) == 2 and tparts[0] and tparts[1].isdigit():
            if ":" in head:
                user, _, password = head.partition(":")
            else:
                user = head
            s = tail
    parts = s.split(":")
    if user is not None and len(parts) == 2:
        host, port_s = parts
    elif len(parts) >= 2 and parts[1].isdigit():
        # host:port[:user:pass] — pass may itself contain : or @
        host, port_s = parts[0], parts[1]
        if len(parts) > 2:
            user, password = parts[2], ":".join(parts[3:])
    elif len(parts) == 4 and parts[3].isdigit():
        # user:pass:host:port
        user, password, host, port_s = parts
    else:
        raise ValueError("expected host:port[:user:pass]")

    host = host.strip()
    if not host:
        raise ValueError("empty host")
    try:
        port = int(port_s)
    except (TypeError, ValueError):
        raise ValueError(f"bad port {port_s!r}")
    if not (1 <= port <= 65535):
        raise ValueError(f"port out of range: {port}")

    auth = ""
    if user:
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"{scheme}://{auth}{host}:{port}"


def mask_proxy(url: str) -> str:
    """Proxy URL safe for logs/chat — password replaced with ***."""
    try:
        scheme, _, rest = url.partition("://")
        if "@" in rest:
            auth, _, hostport = rest.rpartition("@")
            user = auth.split(":", 1)[0]
            return f"{scheme}://{user}:***@{hostport}"
        return url
    except Exception:
        return "proxy"


def load_state() -> dict:
    with _lock:
        try:
            data = json.loads(_store_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "enabled": bool(data.get("enabled")),
                    "proxies": [str(p) for p in data.get("proxies") or []],
                }
        except Exception:
            pass
    return {"enabled": False, "proxies": []}


def save_state(enabled: bool, proxies: List[str]) -> None:
    with _lock:
        try:
            _store_path().write_text(
                json.dumps(
                    {"enabled": bool(enabled), "proxies": list(proxies)},
                    indent=1, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"sip proxy: could not save {_store_path()}: {e}")


def set_proxies(lines) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Validate raw lines. Returns (ok_raw_lines, [(raw, error)]); saves nothing."""
    ok: List[str] = []
    bad: List[Tuple[str, str]] = []
    for raw in lines:
        s = (raw or "").strip()
        if not s or s.startswith("#"):
            continue
        try:
            parse_proxy(s)
            ok.append(s)
        except ValueError as e:
            bad.append((s, str(e)))
    return ok, bad


def proxy_for_worker(worker_id: int) -> Optional[str]:
    """Normalized proxy URL for this worker, or None for a direct connection."""
    st = load_state()
    if not st["enabled"]:
        return None
    proxies = st["proxies"]
    if 0 <= worker_id < len(proxies):
        try:
            return parse_proxy(proxies[worker_id])
        except ValueError as e:
            logger.error(
                f"sip proxy: stored proxy #{worker_id} is invalid ({e}) — "
                f"worker W{worker_id} goes DIRECT"
            )
    return None


def status_report(worker_count: int) -> dict:
    st = load_state()
    proxies = st["proxies"]
    rows = []
    for i, raw in enumerate(proxies):
        try:
            rows.append({
                "worker_id": i, "ok": True,
                "proxy": mask_proxy(parse_proxy(raw)),
            })
        except ValueError as e:
            rows.append({
                "worker_id": i, "ok": False, "proxy": raw, "error": str(e),
            })
    return {
        "enabled": st["enabled"],
        "count": len(proxies),
        "workers": worker_count,
        "missing": max(0, worker_count - len(proxies)) if st["enabled"] else 0,
        "invalid": sum(1 for r in rows if not r["ok"]),
        "rows": rows,
    }
