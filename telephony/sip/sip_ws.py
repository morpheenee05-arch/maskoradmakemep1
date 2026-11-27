"""
SIP-over-WebSocket client replicating the Google Voice web client.

Transport : wss://web.voice.telephony.goog/websocket (subprotocol "sip")
Auth      : SIP Digest (MD5) with per-session credentials from gv_auth
Flow      : REGISTER → 401 → REGISTER+digest → 200
            INVITE(+SDP) → 100 → 183(PRACK) → 180(PRACK) → 200 → ACK → … → BYE

The wire profile mirrors the CURRENT web client ("Birdsong v2"), captured
from Chrome 158 (tools/our_test/decoded_frames.txt):
  * Via "SIP/2.0/WSS <token10>.invalid:5061;branch=z9hG4bK-524287-1---<hex20>;rport"
    (";keep" only on REGISTER)
  * Contact @[::]:5061 with +sip.instance (UPPERCASE uuid), reg-id=1 (REGISTER
    only), +g.3gpp.icsi-ref and device-id params
  * Call-ID "<base64url40>_<10-digit counter>", From-tag 8 hex chars
  * dialog CSeq starts at 1 (INVITE cseq 1, PRACK 2/3…, ACK keeps INVITE's)
  * first REGISTER already carries an empty-nonce Authorization header
  * Max-Forwards 70, UA "GoogleVoice voice.web-frontend_…_p4/Birdsong v2.2.74"
  * 200 OK to BYE carries P-RTP-Stat filled from the media layer
  * CRLFCRLF WebSocket keepalive (server asks Via keep=240)
  * session timer: 200 OK says refresher=uas → the far end refreshes
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from loguru import logger as log
from websockets.asyncio.client import connect as ws_connect

WSS_URL = "wss://web.voice.telephony.goog/websocket"
WSS_ORIGIN = "https://voice.google.com"
USER_AGENT_SIP = "GoogleVoice voice.web-frontend_20261104.07_p4/Birdsong v2.2.74"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
# base64 proto with client-info (Birdsong version string + Chrome build);
# verbatim copy from the captured REGISTER/INVITE
X_GOOGLE_CLIENT_INFO = (
    "Ci1Hb29nbGVWb2ljZSB2b2ljZS53ZWItZnJvbnRlbmRfMjAyNjExMDQuMDdfcDQSEEJp"
    "cmRzb25nIHYyLjIuNzQYBSoQQ2hyb21lIDE1OC4wLjAuMA=="
)
ALLOW = "INVITE, UPDATE, ACK, CANCEL, OPTIONS, BYE, PRACK, NOTIFY"
SUPPORTED_REGISTER = "outbound, path, record-aware, replaces"
SUPPORTED_INVITE = "outbound, path, record-aware, replaces, 100rel"
ICSI_REF = "urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel"
KEEPALIVE_INTERVAL = 150          # server asks keep=240; stay well below
REGISTER_REFRESH_MARGIN = 300     # re-register this many seconds before expiry
SESSION_REFRESH_DIV = 2           # refresh session timer at expires/2

# One compact INFO line per SIP message on the wire (investigating the
# "BYE: ACK not received" storm — need to see whether 200 OK ever arrives
# and what exactly our ACK looks like). Disable with SIP_TRACE=0.
_TRACE = os.getenv("SIP_TRACE", "1").strip().lower() not in ("0", "no", "off")


def _trace_line(direction: str, msg: "SipMessage") -> str:
    if msg.is_request:
        first = f"{msg.method} {msg.uri[:60]}"
    else:
        first = f"{msg.status_code} {msg.reason}".strip()
        if msg.method:
            first += f" ({msg.method})"
    cseq = msg.header("CSeq") or "-"
    call_id = (msg.header("Call-ID") or "-")[:24]
    via = msg.header("Via") or ""
    m = re.search(r"branch=([^;\s]+)", via)
    branch = m.group(1) if m else "-"
    to = msg.header("To") or ""
    m = re.search(r";tag=([^;>\s]+)", to)
    to_tag = m.group(1) if m else "-"
    extra = ""
    if msg.is_request and msg.method in ("BYE", "CANCEL"):
        extra = f" reason={msg.header('Reason') or ''!r}"
    elif not msg.is_request and msg.method == "INVITE" and msg.status_code >= 200:
        extra = (f" contact={'Y' if msg.header('Contact') else 'N'}"
                 f" rr={len(msg.headers_all('Record-Route'))}"
                 f" rseq={msg.header('RSeq') or '-'}")
    elif msg.is_request and msg.method == "ACK":
        extra = f" routes={len(msg.headers_all('Route'))}"
    return (f"{direction} {first} | id={call_id} cseq={cseq} "
            f"br={branch} totag={to_tag}{extra}")


class SipError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


# ── Birdsong-style identity generators (captured shapes) ─────────────────────

_ALNUM = string.ascii_letters + string.digits
_HEX = "0123456789abcdef"
_CALLID_ALPHABET = _ALNUM + "-_"


def _rand_token(n: int, alphabet: str = _ALNUM) -> str:
    return "".join(random.choice(alphabet) for _ in range(n))


def _rand_branch() -> str:
    # every captured Birdsong/SBC branch: z9hG4bK-524287-1---<20 hex>
    return f"z9hG4bK-524287-1---{_rand_token(20, _HEX)}"


def _rand_tag() -> str:
    # captured From/tags are 8 lowercase hex chars
    return _rand_token(8, _HEX)


def _via_host_token() -> str:
    # captured: "EZsl14-aqD", "hpmJMVHZ-T" — 9 alnum + one dash inside
    pos = random.randint(3, 7)
    return _rand_token(pos) + "-" + _rand_token(9 - pos)


_CALL_ID_COUNTER = random.randint(1_000_000_000, 2_000_000_000)


def _new_call_id() -> str:
    # captured: "<base64url-ish 40 chars>_<10 digits>", counter creeps upward
    global _CALL_ID_COUNTER
    _CALL_ID_COUNTER += random.randint(5, 60)
    if _CALL_ID_COUNTER >= 9_999_999_999:
        _CALL_ID_COUNTER = random.randint(1_000_000_000, 2_000_000_000)
    return f"{_rand_token(40, _CALLID_ALPHABET)}_{_CALL_ID_COUNTER}"


# ── SIP message ──────────────────────────────────────────────────────────────

@dataclass
class SipMessage:
    is_request: bool
    method: str = ""                # request method, or method from CSeq for responses
    uri: str = ""
    status_code: int = 0
    reason: str = ""
    headers: list = field(default_factory=list)   # list[(name, value)]
    body: str = ""

    @classmethod
    def parse(cls, data: str) -> "SipMessage":
        head, _, body = data.partition("\r\n\r\n")
        lines = head.split("\r\n")
        first = lines[0]
        msg = cls(is_request=not first.startswith("SIP/2.0"))
        if msg.is_request:
            parts = first.split(" ", 2)
            msg.method = parts[0]
            msg.uri = parts[1] if len(parts) > 1 else ""
        else:
            m = re.match(r"SIP/2\.0 (\d{3})\s*(.*)", first)
            msg.status_code = int(m.group(1)) if m else 0
            msg.reason = m.group(2) if m else ""
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                msg.headers.append((name.strip(), value.strip()))
        msg.body = body
        # CSeq-based method for responses
        if not msg.is_request:
            cseq = msg.header("CSeq") or ""
            parts = cseq.split()
            if len(parts) == 2:
                msg.method = parts[1]
        return msg

    def header(self, name: str) -> Optional[str]:
        name = name.lower()
        for n, v in self.headers:
            if n.lower() == name:
                return v
        return None

    def headers_all(self, name: str) -> list:
        name = name.lower()
        return [v for n, v in self.headers if n.lower() == name]

    def to_bytes(self) -> bytes:
        if self.is_request:
            first = f"{self.method} {self.uri} SIP/2.0"
        else:
            first = f"SIP/2.0 {self.status_code} {self.reason}"
        lines = [first] + [f"{n}: {v}" for n, v in self.headers]
        body = self.body or ""
        lines.append(f"Content-Length: {len(body.encode())}")
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


# ── dialog state for one outgoing call ───────────────────────────────────────

@dataclass
class Dialog:
    call_id: str
    local_tag: str
    local_uri: str
    remote_uri: str
    cseq: int                                # dialog local cseq (post-INVITE)
    invite_branch: str
    invite_cseq: int = 0                     # INVITE's own CSeq — ACK must reuse it
    remote_tag: str = ""
    remote_target: str = ""                  # Contact from 1xx/2xx
    route_set: list = field(default_factory=list)   # reversed Record-Route
    rseq_seen: int = 0
    session_expires: int = 0
    refresher_uac: bool = False
    answered: bool = False
    remote_cseq: int = 0                     # last CSeq of inbound requests


# ── the phone ────────────────────────────────────────────────────────────────

EventCallback = Callable[[str, dict], None]
SdpAnswerer = Callable[[str], str]


class GVWebPhone:
    """One Google Voice web-phone registration; at most one active call."""

    def __init__(self, creds, on_event: Optional[EventCallback] = None,
                 place_call_context: str = ""):
        self.creds = creds
        self.on_event = on_event or (lambda kind, info: None)
        self.place_call_context = place_call_context   # X-GV-PlaceCallContext jspb

        # Birdsong-style stable identities for the registration
        self._via_host = f"{_via_host_token()}.invalid"
        self._device_id = _rand_token(12)
        self._instance_id = str(uuid.uuid4()).upper()
        self._reg_call_id = _new_call_id()
        self._reg_from_tag = _rand_tag()
        self._reg_cseq = 0
        self._service_route: Optional[str] = None
        self._reg_expires = 3600

        self._ws = None
        self._reader_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._reregister_task: Optional[asyncio.Task] = None
        self._session_refresh_task: Optional[asyncio.Task] = None

        self._pending: dict[str, asyncio.Future] = {}   # branch → Future[SipMessage]
        self.dialog: Optional[Dialog] = None
        self.sdp_answerer: Optional[SdpAnswerer] = None  # for in-dialog offers
        # Fills P-RTP-Stat on our 200 OK to BYE (wired to the media layer).
        self.rtp_stat_provider: Optional[Callable[[], str]] = None
        # INVITEs we abandoned via CANCEL: their final failure (487) still
        # needs a transaction-layer ACK or the SBC retransmits it ~32 s.
        self._zombies: dict[str, dict] = {}             # call_id → invite data
        self._p_associated = ""
        self._closed = False
        self._is_registered = False

    # ── plumbing ──

    def _emit(self, kind: str, info: Optional[dict] = None, **kw):
        payload = dict(info or {})
        payload.update(kw)
        try:
            self.on_event(kind, payload)
        except Exception:
            log.exception("on_event callback failed")

    @property
    def registered(self) -> bool:
        return self._is_registered and not self._closed

    @staticmethod
    def _trace(direction: str, msg: "SipMessage"):
        if _TRACE:
            log.info("sip-trace {}", _trace_line(direction, msg))

    @property
    def _reg_uri(self) -> str:
        # REGISTER identity — the long SIP username, sent RAW (the current
        # web client no longer percent-encodes the trailing '=')
        return f"sip:{self.creds.username}@{self.creds.domain}"

    @property
    def _device_uri(self) -> str:
        # dialog identity (INVITE From / Contact user) — the device-id
        return f"sip:{self._device_id}@{self.creds.domain}"

    def _reg_contact(self) -> str:
        # REGISTER Contact: long username + reg-id (captured shape)
        return (f"<sip:{self.creds.username}@[::]:5061;transport=wss>"
                f';+sip.instance="<urn:uuid:{self._instance_id}>"'
                f";reg-id=1"
                f';+g.3gpp.icsi-ref="{ICSI_REF}"'
                f';device-id="{self._device_id}"')

    def _dialog_contact(self) -> str:
        # in-dialog Contact: device-id user, no reg-id (captured shape)
        return (f"<sip:{self._device_id}@[::]:5061;transport=wss>"
                f';+sip.instance="<urn:uuid:{self._instance_id}">'
                f';+g.3gpp.icsi-ref="{ICSI_REF}"'
                f';device-id="{self._device_id}"')

    async def _send(self, msg: SipMessage, branch: Optional[str] = None) -> str:
        branch = branch or _rand_branch()
        via = (f"SIP/2.0/WSS {self._via_host}:5061;branch={branch};rport")
        if msg.method == "REGISTER":
            via += ";keep"            # captured: ";keep" only on REGISTER
        msg.headers.insert(0, ("Via", via))
        data = msg.to_bytes()
        log.debug("SIP C→S\n{}", data.decode(errors="replace"))
        self._trace("C→S", msg)
        await self._ws.send(data)
        return branch

    async def _send_tracked(self, msg: SipMessage) -> tuple:
        """Register the pending-answer future BEFORE sending, so a fast
        response (same event-loop tick) can never be dropped as unmatched."""
        branch = _rand_branch()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[branch] = fut
        await self._send(msg, branch)
        return branch, fut

    async def _request(self, msg: SipMessage, timeout: float = 20.0) -> SipMessage:
        """Send a request and wait for its final response."""
        branch, fut = await self._send_tracked(msg)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(branch, None)

    # ── connection ──

    async def connect(self):
        self._ws = await ws_connect(
            WSS_URL,
            subprotocols=["sip"],
            origin=WSS_ORIGIN,
            additional_headers={"User-Agent": BROWSER_UA},
            ping_interval=None,            # we do SIP-level CRLF keepalive
            max_size=1 << 20,
        )
        self._reader_task = asyncio.create_task(self._reader())
        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def close(self):
        self._closed = True
        self._is_registered = False
        for task in (self._reader_task, self._keepalive_task,
                     self._reregister_task, self._session_refresh_task):
            if task:
                task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _reader(self):
        try:
            async for frame in self._ws:
                data = frame.decode() if isinstance(frame, (bytes, bytearray)) else frame
                data = data.strip("\r\n")
                if not data:
                    continue                      # keepalive pong
                try:
                    msg = SipMessage.parse(data)
                except Exception:
                    log.warning("unparseable SIP frame: {!r}", data[:200])
                    continue
                log.debug("SIP S→C\n{}", data)
                self._trace("S→C", msg)
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("SIP reader ended: {}", e)
            self._is_registered = False
            self._emit("transport_lost", {"error": str(e)})

    async def _keepalive(self):
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await self._ws.send("\r\n\r\n")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("keepalive failed: {}", e)

    async def _dispatch(self, msg: SipMessage):
        if not msg.is_request:
            await self._handle_response(msg)
        else:
            await self._handle_incoming_request(msg)

    # ── registration ──

    async def register(self):
        """REGISTER with digest retry; returns when 200 OK received."""
        self._reg_cseq += 1
        msg = self._register_message(self._reg_cseq, self._auth_empty())
        resp = await self._request(msg)

        if resp.status_code == 401:
            auth = self._digest_authorization(
                resp.header("WWW-Authenticate") or "", "REGISTER",
                f"sip:{self.creds.domain}",
            )
            self._reg_cseq += 1
            msg = self._register_message(self._reg_cseq, auth)
            resp = await self._request(msg)

        if resp.status_code != 200:
            raise SipError(f"REGISTER failed: {resp.status_code} {resp.reason}",
                           resp.status_code)

        service_route = resp.header("Service-Route")
        if service_route:
            self._service_route = service_route.strip("<>")
        p_assoc = resp.header("P-Associated-URI")
        if p_assoc:
            self.note_p_associated_uri(p_assoc)
        contact = resp.header("Contact") or ""
        m = re.search(r"expires=(\d+)", contact)
        if m:
            self._reg_expires = int(m.group(1))
        self._is_registered = True
        self._reregister_task = asyncio.create_task(self._reregister_loop())
        self._emit("registered", {"expires": self._reg_expires,
                                  "p_associated": bool(self._p_associated)})

    def _auth_empty(self) -> str:
        # captured: the FIRST REGISTER already carries a Digest header with
        # empty nonce/response (no algorithm param, no spaces after commas)
        return (f'Digest username="{self.creds.username}",'
                f'realm="{self.creds.realm}",'
                f'uri="sip:{self.creds.domain}",nonce="",response=""')

    def _register_message(self, cseq: int, auth: str) -> SipMessage:
        # captured header order: Via, Max-Forwards, Contact, To, From,
        # Call-ID, CSeq, Expires, Allow, Supported, User-Agent,
        # Authorization, X-Google-Client-Info
        msg = SipMessage(is_request=True, method="REGISTER",
                         uri=f"sip:{self.creds.domain}")
        msg.headers = [
            ("Max-Forwards", "70"),
            ("Contact", self._reg_contact()),
            ("To", f"<{self._reg_uri}>"),
            ("From", f"<{self._reg_uri}>;tag={self._reg_from_tag}"),
            ("Call-ID", self._reg_call_id),
            ("CSeq", f"{cseq} REGISTER"),
            ("Expires", "3600"),
            ("Allow", ALLOW),
            ("Supported", SUPPORTED_REGISTER),
            ("User-Agent", USER_AGENT_SIP),
            ("Authorization", auth),
            ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
        ]
        return msg

    async def _reregister_loop(self):
        try:
            await asyncio.sleep(max(60, self._reg_expires - REGISTER_REFRESH_MARGIN))
            while not self._closed:
                try:
                    await self.register()
                except Exception as e:
                    log.warning("re-REGISTER failed: {}", e)
                    self._emit("register_failed", {"error": str(e)})
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    def _digest_authorization(self, challenge: str, method: str, uri: str) -> str:
        params = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
        realm = params.get("realm", self.creds.realm)
        nonce = params.get("nonce", "")
        ha1 = hashlib.md5(
            f"{self.creds.username}:{realm}:{self.creds.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        # captured param order: username,realm,nonce,uri,response,algorithm
        return (f'Digest username="{self.creds.username}",'
                f'realm="{realm}",nonce="{nonce}",uri="{uri}",'
                f'response="{response}",algorithm=MD5')

    # ── outgoing call ──

    async def start_call(self, number: str, sdp_offer: str) -> Dialog:
        """Send INVITE; returns the dialog once early/confirmed media exists.

        Events drive the rest: 'ringing', 'answered', 'failed', 'bye'.
        """
        if self.dialog and not self.dialog.answered:
            raise SipError("call already in progress")
        number = re.sub(r"[^\d+]", "", number)
        remote_uri = f"sip:{number}@{self.creds.domain}"
        call_id = _new_call_id()
        local_tag = _rand_tag()

        # captured header order: Via, Max-Forwards, Route, Contact, To,
        # From, Call-ID, CSeq, Allow, Content-Type, Supported, User-Agent,
        # P-Preferred-Identity, X-Google-Client-Info, X-GV-PlaceCallContext
        msg = SipMessage(is_request=True, method="INVITE", uri=remote_uri)
        msg.headers = [("Max-Forwards", "70")]
        if self._service_route:
            msg.headers.append(("Route", f"<{self._service_route}>"))
        msg.headers += [
            ("Contact", self._dialog_contact()),
            ("To", f"<{remote_uri}>"),
            ("From", f"<{self._device_uri}>;tag={local_tag}"),
            ("Call-ID", call_id),
            ("CSeq", "1 INVITE"),
            ("Allow", ALLOW),
            ("Content-Type", "application/sdp"),
            ("Supported", SUPPORTED_INVITE),
            ("User-Agent", USER_AGENT_SIP),
            ("P-Preferred-Identity",
             f"<{self._p_associated or self._reg_uri}>"),
            ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
        ]
        if self.place_call_context:
            msg.headers.append(
                ("X-GV-PlaceCallContext", f"jspb={self.place_call_context}"))
        msg.body = sdp_offer

        dialog = Dialog(
            call_id=call_id, local_tag=local_tag,
            local_uri=self._device_uri, remote_uri=remote_uri,
            cseq=1, invite_branch="", invite_cseq=1,
        )
        self.dialog = dialog

        # send with the future already registered — a fast 183/200 must
        # never arrive to an empty pending table
        branch, _fut = await self._send_tracked(msg)
        dialog.invite_branch = branch
        self._emit("calling", {"number": number, "call_id": call_id,
                               "ppi": bool(self._p_associated),
                               "pcc": bool(self.place_call_context)})
        return dialog

    async def hangup(self):
        d = self.dialog
        if not d:
            return
        self._pending.pop(d.invite_branch, None)
        try:
            if d.answered:
                await self._send_in_dialog("BYE")
            else:
                # RFC 3261 §9.1: CANCEL must reuse the INVITE's Via branch and
                # CSeq number, or the SBC answers 481 and keeps the INVITE
                # alive (the "late 200 OK after our timeout" storm).
                msg = SipMessage(is_request=True, method="CANCEL",
                                 uri=d.remote_uri)
                msg.headers = [("Max-Forwards", "70")]
                if self._service_route:
                    msg.headers.append(("Route", f"<{self._service_route}>"))
                msg.headers += [
                    ("Contact", self._dialog_contact()),
                    ("To", f"<{d.remote_uri}>"),
                    ("From", f"<{d.local_uri}>;tag={d.local_tag}"),
                    ("Call-ID", d.call_id),
                    ("CSeq", f"{d.invite_cseq} CANCEL"),
                    ("User-Agent", USER_AGENT_SIP),
                    ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
                    ("Reason", 'SIP ;cause=487 ;text="Cancelled"'),
                ]
                await self._send(msg, branch=d.invite_branch)
                self._stash_zombie(d)
        finally:
            # The dialog is over once BYE/CANCEL is on the wire — keeping it
            # blocked every later start_call with "call already in progress".
            self.dialog = None
        self._emit("hangup_sent", {})

    # ── response handling ──

    async def _handle_response(self, msg: SipMessage):
        branch = self._via_branch(msg)
        if branch and branch in self._pending:
            fut = self._pending[branch]
            final = msg.status_code >= 200
            provisional = 100 <= msg.status_code < 200

            if provisional and msg.method == "INVITE":
                await self._handle_provisional(msg)
                return                          # future stays open for final
            if final and not fut.done():
                fut.set_result(msg)
            if msg.method == "INVITE":
                await self._handle_invite_response(msg)
            return
        # 2xx for our INVITE whose Via branch is no longer in _pending.
        d = self.dialog
        if (d is not None and msg.method == "INVITE"
                and 200 <= msg.status_code < 300
                and branch and branch == d.invite_branch):
            if d.answered:
                # Retransmitted 2xx (branch was popped from _pending with the
                # first 200 OK): the UAS has not seen our ACK.
                # RFC 3261 §13.2.2.4 — re-send ACK for every 2xx
                # retransmission, or the far end's timer H (64*T1 ≈ 32s)
                # tears the dialog down with a BYE.
                await self._send_ack(d)
                self._emit("ack_resent", {"code": msg.status_code})
            else:
                # The pending entry was lost (race) — never drop a real
                # answer: the far side is waiting for our ACK and will
                # BYE with "ACK not received" if we stay silent.
                log.warning(
                    "late 2xx for INVITE (pending entry gone) — answering now")
                await self._handle_invite_response(msg)
            return
        if msg.method == "INVITE" and 200 <= msg.status_code < 300:
            log.warning(
                "orphan 2xx INVITE (dialog already gone) — ACK+BYE to close "
                "it cleanly instead of letting the far end retransmit until "
                "timer H: branch={} call-id={}", branch, msg.header("Call-ID"))
            await self._ack_orphan_2xx(msg)
            return
        if msg.method == "INVITE" and msg.status_code >= 300:
            # Final failure for an INVITE we already abandoned via CANCEL
            # (typically 487). It still needs its transaction-layer ACK.
            z = self._zombies.get(msg.header("Call-ID") or "")
            if z:
                await self._ack_zombie(msg, z)
            return
        # responses to in-dialog requests we sent (BYE/UPDATE/CANCEL/PRACK w/o track)
        log.debug("unmatched response {} {}", msg.status_code, msg.method)

    async def _handle_provisional(self, msg: SipMessage):
        d = self.dialog
        if not d:
            return
        if msg.status_code in (180, 183):
            self._update_dialog_from_response(d, msg)
            if (msg.header("Require") or "").lower() == "100rel":
                # PRACK waits for its 200 OK — must run as a task, otherwise
                # the reader loop blocks here and can never read that very
                # response (self-deadlock until the timeout).
                asyncio.create_task(self._send_prack(d, msg))
            try:
                rseq = int(msg.header("RSeq") or 0)
            except ValueError:
                rseq = 0
            if msg.status_code == 183 and msg.body:
                if rseq > d.rseq_seen:
                    d.rseq_seen = rseq
                    self._emit("early_media", {"sdp": msg.body, "rseq": rseq})
            elif msg.status_code == 180:
                self._emit("ringing", {})

    async def _handle_invite_response(self, msg: SipMessage):
        d = self.dialog
        if not d:
            return
        code = msg.status_code
        if 200 <= code < 300:
            self._pending.pop(d.invite_branch, None)
            self._update_dialog_from_response(d, msg)
            d.answered = True
            se = msg.header("Session-Expires") or ""
            m = re.match(r"(\d+)", se)
            if m:
                d.session_expires = int(m.group(1))
                d.refresher_uac = "refresher=uac" in se
                self._arm_session_refresh(d)
            await self._send_ack(d)
            self._emit("answered", {"sdp": msg.body,
                                    "call_leg_id": msg.header("X-GV-CallLegId") or ""})
        elif code >= 300:
            self._pending.pop(d.invite_branch, None)
            self._update_dialog_from_response(d, msg)
            # RFC 3261 §17.1.1.3: a non-2xx final response is ACKed hop-by-hop
            # with the SAME Via branch as the INVITE (transaction-layer ACK).
            # Without it the UAS retransmits the failure until timer H (~32s):
            # Google was seen re-sending 487s for 35s+ after our CANCEL.
            try:
                ack = self._dialog_request(
                    "ACK", increment=False,
                    cseq_override=d.invite_cseq or d.cseq)
                await self._send(ack, branch=d.invite_branch)
            except Exception as e:
                log.warning("transaction ACK for {} failed: {}", code, e)
            self._emit("failed", {"code": code, "reason": msg.reason})
            self.dialog = None

    def _stash_zombie(self, d: Dialog):
        self._zombies[d.call_id] = {
            "ts": time.time(),
            "remote_uri": d.remote_uri,
            "local_uri": d.local_uri,
            "local_tag": d.local_tag,
            "invite_cseq": d.invite_cseq,
            "invite_branch": d.invite_branch,
            "route_set": list(d.route_set),
        }
        for cid, z in list(self._zombies.items()):
            if time.time() - z["ts"] > 90:
                self._zombies.pop(cid, None)

    async def _ack_zombie(self, msg: SipMessage, z: dict):
        """Transaction-layer ACK (same branch) for a final failure of an
        INVITE we already abandoned with CANCEL — the 487 otherwise gets
        retransmitted by the SBC for ~32 s for no reason."""
        try:
            to = msg.header("To") or f"<{z['remote_uri']}>"
            ack = SipMessage(is_request=True, method="ACK",
                             uri=z["remote_uri"])
            ack.headers = [("Max-Forwards", "70")]
            ack.headers += [("Route", r) for r in z["route_set"]]
            ack.headers += [
                ("Contact", self._dialog_contact()),
                ("To", to),
                ("From", f"<{z['local_uri']}>;tag={z['local_tag']}"),
                ("Call-ID", msg.header("Call-ID") or ""),
                ("CSeq", f"{z['invite_cseq']} ACK"),
                ("User-Agent", USER_AGENT_SIP),
                ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
            ]
            await self._send(ack, branch=z["invite_branch"])
        except Exception as e:
            log.warning("zombie ACK failed: {}", e)

    async def _ack_orphan_2xx(self, msg: SipMessage):
        """ACK + BYE a 2xx INVITE response whose dialog is already gone
        (typical: we CANCELed on the ring timeout and the callee / voicemail
        answered a second later). Without this the UAS retransmits 200 OK
        until timer H and then tears the leg down with a BYE carrying
        Reason "ACK not received" — which also polluted our stats."""
        to = msg.header("To") or ""
        frm = msg.header("From") or ""
        call_id = msg.header("Call-ID") or ""
        cseq_hdr = msg.header("CSeq") or ""
        cseq_num = cseq_hdr.split()[0] if cseq_hdr else ""
        contact = (msg.header("Contact") or "").strip().strip("<>")
        if not (to and call_id and cseq_num and contact):
            log.warning("orphan 2xx ACK skipped — missing headers")
            return
        routes = [r.strip() for r in reversed(msg.headers_all("Record-Route"))]

        def _req(method: str, cseq: str, extra: Optional[list] = None):
            req = SipMessage(is_request=True, method=method, uri=contact)
            req.headers = [("Max-Forwards", "70")]
            req.headers += [("Route", r) for r in routes]
            req.headers += [
                ("Contact", self._dialog_contact()),
                ("To", to),
                ("From", frm),
                ("Call-ID", call_id),
                ("CSeq", cseq),
                ("User-Agent", USER_AGENT_SIP),
                ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
            ]
            for h in (extra or []):
                req.headers.append(h)
            return req

        await self._send(_req("ACK", f"{cseq_num} ACK"))
        await self._send(_req("BYE", "1 BYE", extra=[
            ("Reason", 'SIP ;cause=200 ;text="Normal call clearing"'),
        ]))

    def _update_dialog_from_response(self, d: Dialog, msg: SipMessage):
        to = msg.header("To") or ""
        m = re.search(r";tag=([^;>\s]+)", to)
        if m:
            d.remote_tag = m.group(1)
        contact = msg.header("Contact")
        if contact:
            d.remote_target = contact.strip("<>")
        routes = msg.headers_all("Record-Route")
        if routes:
            d.route_set = [r.strip() for r in reversed(routes)]

    async def _send_prack(self, d: Dialog, prov: SipMessage):
        rseq = prov.header("RSeq") or "0"
        cseq_hdr = prov.header("CSeq") or ""
        invite_cseq = cseq_hdr.split()[0] if cseq_hdr else str(d.invite_cseq)
        # captured order: …, CSeq, User-Agent, RAck, X-Google-Client-Info
        msg = self._dialog_request(
            "PRACK", extra=[("RAck", f"{rseq} {invite_cseq} INVITE")])
        try:
            resp = await self._request(msg, timeout=15)
            log.info("PRACK rseq={} -> {}", rseq, resp.status_code)
        except Exception as e:
            log.warning("PRACK rseq={} failed: {}", rseq, e)
            self._emit("prack_failed", {"rseq": rseq, "error": str(e)})

    async def _send_ack(self, d: Dialog):
        # RFC 3261 §13.2.2.4: the ACK's CSeq number MUST equal the INVITE's —
        # NOT the dialog's current cseq (PRACKs have incremented it). A strict
        # UAS/SBC can't match a wrong-CSeq ACK to the INVITE transaction,
        # keeps retransmitting 200 OK, then drops the call: BYE with
        # Reason "ACK not received" — seen in production at 1–2 s of talk.
        msg = self._dialog_request("ACK", increment=False,
                                   cseq_override=d.invite_cseq or d.cseq)
        await self._send(msg)
        log.info("sip ACK sent for INVITE (cseq={} call-id={})",
                 d.invite_cseq or d.cseq, d.call_id)

    def _dialog_request(self, method: str, increment: bool = True,
                        cseq_override: Optional[int] = None,
                        extra: Optional[list] = None) -> SipMessage:
        # captured order (PRACK/ACK/BYE): Via, Max-Forwards, Route…,
        # Contact, To, From, Call-ID, CSeq, User-Agent, [extra],
        # X-Google-Client-Info
        d = self.dialog
        assert d
        if increment:
            d.cseq += 1
        cseq = cseq_override if cseq_override is not None else d.cseq
        target = d.remote_target or d.remote_uri
        to = f"<{d.remote_uri}>" + (f";tag={d.remote_tag}" if d.remote_tag else "")
        msg = SipMessage(is_request=True, method=method, uri=target)
        msg.headers = [("Max-Forwards", "70")]
        msg.headers += [("Route", r) for r in d.route_set]
        msg.headers += [
            ("Contact", self._dialog_contact()),
            ("To", to),
            ("From", f"<{d.local_uri}>;tag={d.local_tag}"),
            ("Call-ID", d.call_id),
            ("CSeq", f"{cseq} {method}"),
            ("User-Agent", USER_AGENT_SIP),
        ]
        for h in (extra or []):
            msg.headers.append(h)
        msg.headers.append(("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO))
        return msg

    async def _send_in_dialog(self, method: str, body: str = "",
                              content_type: str = ""):
        extra = [("Content-Type", content_type)] if content_type else None
        msg = self._dialog_request(method, extra=extra)
        if body:
            msg.body = body
        return await self._request(msg)

    # ── session timer (only if the 200 OK makes us the refresher) ──

    def _arm_session_refresh(self, d: Dialog):
        if self._session_refresh_task:
            self._session_refresh_task.cancel()
        if d.refresher_uac and d.session_expires > 0:
            self._session_refresh_task = asyncio.create_task(
                self._session_refresh_loop(d))

    async def _session_refresh_loop(self, d: Dialog):
        try:
            while not self._closed and self.dialog is d and d.answered:
                await asyncio.sleep(max(10, d.session_expires // SESSION_REFRESH_DIV))
                if self.dialog is not d or not d.answered:
                    return
                try:
                    msg = self._dialog_request("UPDATE", extra=[
                        ("Session-Expires",
                         f"{d.session_expires};refresher=uac"),
                    ])
                    await self._request(msg, timeout=15)
                except Exception as e:
                    log.warning("session refresh failed: {}", e)
                    self._emit("session_refresh_failed", {"error": str(e)})
                    return
        except asyncio.CancelledError:
            pass

    # ── incoming requests ──

    async def _handle_incoming_request(self, msg: SipMessage):
        method = msg.method
        if method == "BYE":
            stat = ""
            if self.rtp_stat_provider:
                try:
                    stat = self.rtp_stat_provider() or ""
                except Exception:
                    stat = ""
            if not stat:
                stat = "PS=0, OS=0, PR=0, OR=0, PL=0, JI=0, LA=0, DU=0, NP=0"
            await self._respond(msg, 200, "OK", extra=[
                ("P-RTP-Stat", stat),
            ])
            if self.dialog:
                self._pending.pop(self.dialog.invite_branch, None)
            self._emit("bye", {"reason": msg.header("Reason") or ""})
            self.dialog = None
            return
        if method == "INFO":
            await self._respond(msg, 200, "OK")
            body = msg.body or ""
            m = re.search(r"Signal=([0-9#*A-D])", body)
            if m:
                self._emit("dtmf_info", {"digit": m.group(1)})
            return
        if method == "UPDATE":
            # session refresh or offer from the network side
            if msg.body and self.sdp_answerer:
                try:
                    answer = self.sdp_answerer(msg.body)
                except Exception as e:
                    log.warning("sdp_answerer failed: {}", e)
                    await self._respond(msg, 488, "Not Acceptable Here")
                    return
                await self._respond(msg, 200, "OK", body=answer,
                                    content_type="application/sdp")
            else:
                await self._respond(msg, 200, "OK")
            return
        if method in ("OPTIONS", "MESSAGE", "NOTIFY", "REFER"):
            await self._respond(msg, 200, "OK")
            return
        if method == "INVITE":
            # inbound call (forked to every binding) — decline for now
            self._emit("inbound_call", {"from": msg.header("From") or ""})
            await self._respond(msg, 603, "Decline")
            return
        await self._respond(msg, 200, "OK")

    async def _respond(self, req: SipMessage, code: int, reason: str,
                       body: str = "", content_type: str = "",
                       extra: Optional[list] = None):
        # captured 200-to-BYE order: Via…, Record-Route…, Contact, To, From,
        # Call-ID, CSeq, User-Agent, X-Google-Client-Info, P-RTP-Stat
        resp = SipMessage(is_request=False, status_code=code, reason=reason)
        resp.headers = [(n, v) for n, v in req.headers
                        if n.lower() == "via"]
        resp.headers += [(n, v) for n, v in req.headers
                         if n.lower() == "record-route"]
        d = self.dialog
        if d:
            resp.headers.append(("Contact", self._dialog_contact()))
        resp.headers += [(n, v) for n, v in req.headers
                         if n.lower() in ("to", "from", "call-id", "cseq")]
        # make sure To carries our local tag for in-dialog responses
        if d and req.method != "INVITE":
            for i, (n, v) in enumerate(resp.headers):
                if n.lower() == "to" and "tag=" not in v:
                    resp.headers[i] = (n, f"{v};tag={d.local_tag}")
        resp.headers.append(("User-Agent", USER_AGENT_SIP))
        resp.headers.append(("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO))
        for h in (extra or []):
            resp.headers.append(h)
        if body:
            resp.headers.append(("Content-Type", content_type))
            resp.body = body
        data = resp.to_bytes()
        log.debug("SIP C→S\n{}", data.decode(errors="replace"))
        self._trace("C→S", resp)
        await self._ws.send(data)

    # ── DTMF (SIP INFO path; RTP telephone-event is the primary path) ──

    async def send_dtmf_info(self, digit: str, duration: int = 250):
        if not self.dialog or not self.dialog.answered:
            return
        body = f"Signal={digit}\r\nDuration={duration}"
        await self._send_in_dialog("INFO", body, "application/dtmf-relay")

    # ── misc ──

    @staticmethod
    def _via_branch(msg: SipMessage) -> str:
        via = msg.header("Via") or ""
        m = re.search(r";branch=([^;\s]+)", via)
        return m.group(1) if m else ""

    def note_p_associated_uri(self, uri: str):
        """Store P-Associated-URI from REGISTER 200 for P-Preferred-Identity."""
        self._p_associated = uri.strip("<>")
