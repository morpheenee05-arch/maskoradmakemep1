#!/usr/bin/env python3
"""
Live test of the SIP backend phases 1–2 (auth + signaling, no media yet).

Usage:
    py -3 tools/gv_sip_test.py --cookies cookies.json [--call +17787961255] [--hold 25]

cookies.json — {"SAPISID": "...", "__Secure-1PAPISID": "...", ...}
(harvest on a logged-in agent Chrome via CDP: Runtime.evaluate document.cookie
 on https://voice.google.com — SAPISID is not HttpOnly)

What it does:
  1. POSTs sipregisterinfo/get → prints the minted SIP username (password masked)
  2. REGISTERs on wss://web.voice.telephony.goog with digest auth
  3. optionally INVITEs --call number with a template SDP offer and logs every
     event (ringing / early_media / answered / failed / bye) for --hold seconds,
     then hangs up

Phase 3 note: the SDP offer here is a static template with fake ICE creds, so
audio will never flow — this validates *signaling only*. If Google answers
(200 OK) the SIP side is correct and phase 3 (aiortc) is the only blocker.
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telephony.sip.gv_auth import GVAuthError, VoiceAPIClient  # noqa: E402
from telephony.sip.sip_ws import GVWebPhone, SipError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("gv_sip_test")

from telephony.sip.templates import TEMPLATE_SDP  # noqa: E402


def on_event(kind, info):
    if kind == "answered":
        log.info("EVENT answered — 200 OK, call connected (signaling OK!)")
    elif kind == "early_media":
        log.info("EVENT early_media (183, %d bytes SDP)", len(info.get("sdp", "")))
    else:
        log.info("EVENT %s %s", kind, {k: str(v)[:120] for k, v in info.items()})


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", required=True, type=Path)
    ap.add_argument("--call", default="")
    ap.add_argument("--hold", type=int, default=25)
    args = ap.parse_args()

    cookies = json.loads(args.cookies.read_text(encoding="utf-8"))
    api = VoiceAPIClient(cookies)

    log.info("── phase 1: minting SIP credentials …")
    try:
        creds = api.get_sip_register_info()
    except GVAuthError as e:
        log.error("auth failed: %s", e)
        return 2
    log.info("SIP username: %s", creds.username)
    log.info("SIP password: %s…%s (len=%d)",
             creds.password[:2], creds.password[-2:], len(creds.password))
    log.info("raw response: %s", json.dumps(creds.raw)[:500])

    log.info("── phase 2: REGISTER …")
    phone = GVWebPhone(creds, on_event=on_event)
    try:
        await phone.connect()
        await phone.register()
    except SipError as e:
        log.error("register failed: %s", e)
        await phone.close()
        return 3
    log.info("REGISTER OK (expires=%s)", phone._reg_expires)

    if args.call:
        log.info("── calling %s …", args.call)
        await phone.start_call(args.call, TEMPLATE_SDP)
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=args.hold)
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


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
