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

# Offer shaped exactly like the captured web-client one, with fresh-looking
# ICE creds. Signaling doesn't validate them; media (phase 3) replaces this.
TEMPLATE_SDP = """v=0\r
o=- 6279769904123977737 2 IN IP4 127.0.0.1\r
s=-\r
t=0 0\r
a=group:BUNDLE 0\r
a=extmap-allow-mixed\r
a=msid-semantic: WMS\r
m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126\r
c=IN IP4 0.0.0.0\r
a=rtcp:9 IN IP4 0.0.0.0\r
a=ice-ufrag:mAS9\r
a=ice-pwd:0JtffAzPf/4Zqn5LSFiuLfL6\r
a=ice-options:trickle\r
a=fingerprint:sha-256 20:B3:DC:33:62:75:DE:63:A5:21:ED:EF:5C:2A:47:14:F6:C6:28:1F:45:33:95:64:5D:93:F1:BD:18:17:20:CA\r
a=setup:actpass\r
a=mid:0\r
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r
a=sendrecv\r
a=msid:- 55e8e7ac-259b-4445-b1d3-93f6a3f0496c\r
a=rtcp-mux\r
a=rtpmap:111 opus/48000/2\r
a=fmtp:111 minptime=10;useinbandfec=1\r
a=rtpmap:63 red/48000/2\r
a=fmtp:63 111/111\r
a=rtpmap:9 G722/8000\r
a=rtpmap:0 PCMU/8000\r
a=rtpmap:8 PCMA/8000\r
a=rtpmap:13 CN/8000\r
a=rtpmap:110 telephone-event/48000\r
a=rtpmap:126 telephone-event/8000\r
a=ssrc:2407426248 cname:TSEsOzc+jKql3A2g\r
a=ssrc:2407426248 msid:- 55e8e7ac-259b-4445-b1d3-93f6a3f0496c\r
"""


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
