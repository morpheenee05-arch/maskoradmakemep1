"""
SIP telephony backend (Google Voice web-client replay).

Planned modules (Phase 1+ of the SIP migration, see README section
"Fleet updates" discussion and docs/SIP_CAPTURE_CHECKLIST.md):

    gv_auth.py   — Google cookies → per-session SIP username/password mint
    sip_ws.py    — asyncio WebSocket transport + SIP dialog state machine
    media.py     — ICE/DTLS-SRTP media (aiortc) bridged to our PCM pipeline
    sip_pool.py  — PhonePool-compatible pool of SIP lines (drop-in backend)

Everything here is built from captured traffic (tools/netlog_sip_dump.py
output in captures/), never from guesses — when Google rotates a string we
re-capture and diff.
"""
