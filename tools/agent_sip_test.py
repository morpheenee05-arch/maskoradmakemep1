#!/usr/bin/env python3
"""
On-agent live test of the SIP backend (phases 1–2).

Run ON AN AGENT (Windows RDP with a logged-in worker Chrome profile).
The agent service for the chosen worker must be stopped — two Chromes cannot
share one profile.

    py -3 tools/agent_sip_test.py --worker 1 --call +14036139786
    py -3 tools/agent_sip_test.py --worker 1                 # register only
    py -3 tools/agent_sip_test.py --worker 1 --visible       # watch Chrome

What it does:
  1. Spawns uc.Chrome on .chrome_profile_N, opens voice.google.com, harvests
     document.cookie (SAPISID & co are not HttpOnly) → tools/cookies_wN.json
  2. Closes Chrome
  3. Mints SIP credentials (sipregisterinfo/get) and prints the RAW response
     so we can verify the parser on first run
  4. REGISTERs on the SIP WebSocket, optionally places a call with a template
     SDP (signaling only — no media until phase 3), logs every event
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from telephony.sip.gv_auth import GVAuthError, VoiceAPIClient  # noqa: E402
from telephony.sip.sip_ws import GVWebPhone, SipError  # noqa: E402
from tools.gv_sip_test import TEMPLATE_SDP, on_event  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("agent_sip_test")

GV_URL = "https://voice.google.com/u/0/calls"
NEEDED = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")


def harvest_cookies(worker: int, visible: bool, timeout: int = 60) -> dict:
    """Spawn Chrome on the worker profile and read document.cookie."""
    import undetected_chromedriver as uc
    from selenium.webdriver.chrome.service import Service

    profile_dir = config.BASE_DIR / f".chrome_profile_{worker}"
    if not profile_dir.exists():
        raise SystemExit(f"profile not found: {profile_dir}")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    if not visible:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.add_argument("--window-size=1024,720")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.page_load_strategy = "eager"

    driver_path = config.resolve_chromedriver_path()
    service = Service(executable_path=str(driver_path), port=19500 + worker,
                      service_args=["--host=127.0.0.1"])
    log.info("spawning Chrome on %s …", profile_dir.name)
    driver = uc.Chrome(
        options=options,
        service=service,
        driver_executable_path=str(driver_path),
        use_subprocess=True,
        version_main=config.resolve_chrome_version_main(),
        keep_alive=True,
    )
    try:
        driver.get(GV_URL)
        deadline = time.time() + timeout
        cookies = {}
        while time.time() < deadline:
            try:
                raw = driver.execute_script("return document.cookie") or ""
            except Exception as e:
                log.debug("cookie read failed (%s), retrying", e)
                time.sleep(2)
                continue
            cookies = dict(
                part.strip().split("=", 1)
                for part in raw.split(";")
                if "=" in part
            )
            if any(k in cookies for k in NEEDED):
                break
            log.info("waiting for GV session cookies… (%s)", list(cookies)[:5])
            time.sleep(3)
        found = [k for k in NEEDED if k in cookies]
        if not found:
            raise SystemExit(
                f"no SAPISID-family cookies on {driver.current_url} — "
                "profile not logged in?"
            )
        log.info("harvested cookies: %s", found)
        out = Path(__file__).parent / f"cookies_w{worker}.json"
        out.write_text(json.dumps(cookies, indent=1), encoding="utf-8")
        log.info("saved → %s", out)
        return cookies
    finally:
        try:
            driver.quit()
        except Exception:
            pass


async def sip_phase(cookies: dict, call: str, hold: int) -> int:
    api = VoiceAPIClient(cookies)
    log.info("── phase 1: minting SIP credentials …")
    try:
        creds = api.get_sip_register_info()
    except GVAuthError as e:
        log.error("credential mint failed: %s", e)
        return 2
    log.info("SIP username: %s", creds.username)
    log.info("SIP password: %s…%s (len=%d)",
             creds.password[:2], creds.password[-2:], len(creds.password))
    log.info("RAW sipregisterinfo response: %s", json.dumps(creds.raw))

    log.info("── phase 2: REGISTER …")
    phone = GVWebPhone(creds, on_event=on_event)
    try:
        await phone.connect()
        await phone.register()
    except SipError as e:
        log.error("REGISTER failed: %s", e)
        await phone.close()
        return 3
    log.info("REGISTER OK — SIP signaling works on this account")

    if call:
        log.info("── calling %s (signaling only, template SDP) …", call)
        await phone.start_call(call, TEMPLATE_SDP)
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=hold)
        except asyncio.TimeoutError:
            pass
        log.info("── hanging up …")
        await phone.hangup()
        await asyncio.sleep(2)
    else:
        await asyncio.sleep(5)

    await phone.close()
    log.info("done")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worker", type=int, required=True)
    ap.add_argument("--call", default="")
    ap.add_argument("--hold", type=int, default=25)
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--cookies", type=Path, default=None,
                    help="skip Chrome harvest, use existing cookies json")
    args = ap.parse_args()

    if args.cookies:
        cookies = json.loads(args.cookies.read_text(encoding="utf-8"))
    else:
        cookies = harvest_cookies(args.worker, args.visible)
    sys.exit(asyncio.run(sip_phase(cookies, args.call, args.hold)))


if __name__ == "__main__":
    main()
