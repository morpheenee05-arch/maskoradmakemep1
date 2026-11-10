"""
Phase 1 self-test — verifies Redis bus connectivity and the agent registry
WITHOUT starting Chrome / Google Voice.

Run on the CONTROLLER box and on every AGENT box to confirm REDIS_URL /
TLS / password / network are correct before launching the telephony stack.

    python -m tools.bus_selftest

Exit code 0 = all good. Non-zero = something is misconfigured.
ASCII-only output (safe on Windows cp1251 consoles).
"""
import asyncio
import sys
import time

import config
from infra.redis_bus import RedisBus, AGENT_KEY_PREFIX


async def _main() -> int:
    print("-- GV bus self-test ----------------------------")
    print(f"NODE_ROLE = {config.NODE_ROLE}")
    print(f"AGENT_ID  = {config.AGENT_ID}")
    url = config.REDIS_URL
    masked = url
    if "@" in masked:  # hide password in the URL
        masked = masked.split("://")[0] + "://***@" + masked.split("@", 1)[1]
    print(f"REDIS_URL = {masked or '(empty!)'}")

    if not url:
        print("[FAIL] REDIS_URL is empty - set it in .env")
        return 2

    # URL diagnostics (catches quotes / spaces / empty password / bad scheme).
    import urllib.parse as _up
    pr = _up.urlparse(url)
    print(f"  scheme   = {pr.scheme!r}")
    print(f"  host     = {pr.hostname!r}")
    print(f"  port     = {pr.port!r}")
    print(f"  username = {pr.username!r}")
    pw = pr.password or ""
    print(f"  password : len={len(pw)} startswith_quote="
          f"{pw[:1] in (chr(34), chr(39))} has_space={' ' in pw}")
    if url != url.strip() or '"' in url or "'" in url:
        print("  [WARN] URL has surrounding spaces or quotes — remove them "
              "from the .env line (no quotes, no spaces around '=').")

    # Explicit connect so the REAL exception is printed (not hidden).
    try:
        from redis.asyncio import Redis
    except ImportError:
        print("[FAIL] 'redis' package not installed - run: py -m pip install redis")
        return 6

    t0 = time.time()
    try:
        r = Redis.from_url(url, decode_responses=True,
                           socket_timeout=8, socket_connect_timeout=8)
        pong = await r.ping()
        await r.aclose()
        print(f"[OK]   Redis reachable, PING={pong} "
              f"({(time.time() - t0) * 1000:.0f} ms)")
    except Exception as e:
        print(f"[FAIL] Redis connect/auth — {type(e).__name__}: {e}")
        print("  Hints: 'WRONGPASS' = password mismatch; "
              "'Connection refused' = wrong host/port or Redis down; "
              "'NOAUTH' = password missing in URL.")
        return 3

    bus = RedisBus()

    try:
        await bus.register_or_heartbeat({
            "state": "selftest",
            "total_workers": 0,
            "ready_workers": 0,
            "checked_out_workers": 0,
            "numbers_fetched_workers": 0,
        })
        print(f"[OK]   register_or_heartbeat ({AGENT_KEY_PREFIX}{config.AGENT_ID})")
    except Exception as e:
        print(f"[FAIL] register_or_heartbeat: {e}")
        await bus.close()
        return 4

    try:
        agents = await bus.list_agents()
        print(f"[OK]   list_agents - {len(agents)} agent(s) registered:")
        for a in agents:
            print(
                f"       - {a.get('agent_id')} role={a.get('role')} "
                f"state={a.get('state')} "
                f"ready={a.get('ready_workers')}/{a.get('total_workers')}"
            )
    except Exception as e:
        print(f"[FAIL] list_agents: {e}")
        await bus.close()
        return 5

    try:
        await bus.unregister()
        print("[OK]   unregister (self-test record removed)")
    except Exception as e:
        print(f"[WARN] unregister (non-fatal): {e}")

    await bus.close()
    print("-- ALL CHECKS PASSED ---------------------------")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(130)
