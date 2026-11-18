"""
SIP telephony backend (Google Voice web-client replay).

Modules:
    gv_auth.py   — Google cookies → per-session SIP username/password mint
    sip_ws.py    — asyncio WebSocket transport + SIP dialog state machine
    media.py     — (phase 3) ICE/DTLS-SRTP media (aiortc) bridged to PCM
    sip_pool.py  — (phase 4) PhonePool-compatible pool of SIP lines

Everything here is built from captured traffic (tools/netlog_sip_dump.py
output in captures/), never from guesses — when Google rotates a string we
re-capture and diff.
"""
from .gv_auth import GVAuthError, SipCredentials, VoiceAPIClient, sapisid_hash
from .sip_ws import GVWebPhone, SipError, SipMessage

__all__ = [
    "GVAuthError",
    "SipCredentials",
    "VoiceAPIClient",
    "sapisid_hash",
    "GVWebPhone",
    "SipError",
    "SipMessage",
]
