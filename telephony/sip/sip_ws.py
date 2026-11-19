"""
SIP-over-WebSocket client replicating the Google Voice web client.

Transport : wss://web.voice.telephony.goog/websocket (subprotocol "sip")
Auth      : SIP Digest (MD5) with per-session credentials from gv_auth
Flow      : REGISTER → 401 → REGISTER+digest → 200
            INVITE(+SDP) → 100 → 183(PRACK) → 180(PRACK) → 200 → ACK → … → BYE

Everything here mirrors frames captured from the real web client
(captures/sip_flow_full.txt), including its JsSIP quirks:
  * Via host "<rand12>.invalid", branch "z9hG4bK<rand7>", ";keep" param
  * Contact user part is a separate random token
  * initial INVITE CSeq is random; ACK reuses the dialog local CSeq
    (i.e. after PRACKs have incremented it — non-standard but accepted)
  * CRLFCRLF WebSocket keepalive (server asks Via keep=240)
  * session timer: we are refresher=uac → UPDATE refresh at expires/2
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from websockets.asyncio.client import connect as ws_connect

log = logging.getLogger(__name__)

WSS_URL = "wss://web.voice.telephony.goog/websocket"
WSS_ORIGIN = "https://voice.google.com"
USER_AGENT_SIP = "GoogleVoice voice.web-frontend_20261104.07_p3"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
# base64 proto with client-info (version string + Chrome build); verbatim copy
X_GOOGLE_CLIENT_INFO = (
    "Ci1Hb29nbGVWb2ljZSB2b2ljZS53ZWItZnJvbnRlbmRfMjAyNjExMDQuMDdfcDMSLUdvb2"
    "dsZVZvaWNlIHZvaWNlLndlYi1mcm9udGVuZF8yMDI2MTEwNC4wN19wMxgFKhBDaHJvbWUgMTU"
    "3LjAuMC4w"
)
ALLOW = "INVITE,ACK,CANCEL,BYE,UPDATE,MESSAGE,OPTIONS,REFER,INFO,PRACK"
KEEPALIVE_INTERVAL = 150          # server asks keep=240; stay well below
REGISTER_REFRESH_MARGIN = 300     # re-register this many seconds before expiry
SESSION_REFRESH_DIV = 2           # refresh session timer at expires/2


class SipError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


def _rand_token(n: int, alphabet: str = string.ascii_lowercase + string.digits) -> str:
    return "".join(random.choice(alphabet) for _ in range(n))


def _rand_branch() -> str:
    return f"z9hG4bK{random.randint(1_000_000, 99_999_999)}"


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

        # JsSIP-style stable identities for the registration
        self._via_host = f"{_rand_token(12)}.invalid"
        self._contact_user = _rand_token(8)
        self._instance_id = str(uuid.uuid4())
        self._reg_call_id = _rand_token(20, string.hexdigits.lower()[:16] + "abcdef")
        self._reg_from_tag = _rand_token(10)
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

    @property
    def _local_uri(self) -> str:
        # JsSIP percent-encodes the user part: base64 '=' shows up as %3D on
        # the wire (digest still uses the raw username)
        user = (self.creds.username
                .replace("%", "%25").replace("+", "%2B")
                .replace("/", "%2F").replace("=", "%3D"))
        return f"sip:{user}@{self.creds.domain}"

    async def _send(self, msg: SipMessage, branch: Optional[str] = None) -> str:
        branch = branch or _rand_branch()
        msg.headers.insert(0, (
            "Via",
            f"SIP/2.0/wss {self._via_host};branch={branch};keep",
        ))
        data = msg.to_bytes()
        log.debug("SIP C→S\n%s", data.decode(errors="replace"))
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

    def _new_request(self, method: str, uri: str, *, call_id: str,
                     from_uri: str, from_tag: str, to_uri: str, to_tag: str = "",
                     cseq: int) -> SipMessage:
        to = f"<{to_uri}>" + (f";tag={to_tag}" if to_tag else "")
        msg = SipMessage(is_request=True, method=method, uri=uri)
        msg.headers = [
            ("Max-Forwards", "69"),
            ("To", to),
            ("From", f"<{from_uri}>;tag={from_tag}"),
            ("Call-ID", call_id),
            ("CSeq", f"{cseq} {method}"),
            ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
            ("Allow", ALLOW),
            ("User-Agent", USER_AGENT_SIP),
        ]
        return msg

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
                    log.warning("unparseable SIP frame: %r", data[:200])
                    continue
                log.debug("SIP S→C\n%s", data)
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("SIP reader ended: %s", e)
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
            log.warning("keepalive failed: %s", e)

    async def _dispatch(self, msg: SipMessage):
        if not msg.is_request:
            await self._handle_response(msg)
        else:
            await self._handle_incoming_request(msg)

    # ── registration ──

    async def register(self):
        """REGISTER with digest retry; returns when 200 OK received."""
        self._reg_cseq += 1
        msg = self._register_message(self._reg_cseq)
        resp = await self._request(msg)

        if resp.status_code == 401:
            auth = self._digest_authorization(
                resp.header("WWW-Authenticate") or "", "REGISTER",
                f"sip:{self.creds.domain}",
            )
            self._reg_cseq += 1
            msg = self._register_message(self._reg_cseq)
            msg.headers.insert(4, ("Authorization", auth))
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

    def _register_message(self, cseq: int) -> SipMessage:
        msg = self._new_request(
            "REGISTER", f"sip:{self.creds.domain}",
            call_id=self._reg_call_id,
            from_uri=self._local_uri, from_tag=self._reg_from_tag,
            to_uri=self._local_uri,
            cseq=cseq,
        )
        # order matches the capture: Contact/Expires before Allow
        msg.headers = [
            ("Max-Forwards", "69"),
            ("To", f"<{self._local_uri}>"),
            ("From", f"<{self._local_uri}>;tag={self._reg_from_tag}"),
            ("Call-ID", self._reg_call_id),
            ("CSeq", f"{cseq} REGISTER"),
            ("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO),
            ("Contact",
             f"<sip:{self._contact_user}@{self._via_host};transport=wss>"
             f';+sip.ice;reg-id=1;+sip.instance="<urn:uuid:{self._instance_id}>"'
             ";expires=3600"),
            ("Expires", "3600"),
            ("Allow", ALLOW),
            ("Supported", "path,gruu,outbound,record-aware"),
            ("User-Agent", USER_AGENT_SIP),
        ]
        return msg

    async def _reregister_loop(self):
        try:
            await asyncio.sleep(max(60, self._reg_expires - REGISTER_REFRESH_MARGIN))
            while not self._closed:
                try:
                    await self.register()
                except Exception as e:
                    log.warning("re-REGISTER failed: %s", e)
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
        return (
            f'Digest algorithm=MD5, username="{self.creds.username}", '
            f'realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'response="{response}"'
        )

    # ── outgoing call ──

    async def start_call(self, number: str, sdp_offer: str) -> Dialog:
        """Send INVITE; returns the dialog once early/confirmed media exists.

        Events drive the rest: 'ringing', 'answered', 'failed', 'bye'.
        """
        if self.dialog and not self.dialog.answered:
            raise SipError("call already in progress")
        number = re.sub(r"[^\d+]", "", number)
        remote_uri = f"sip:{number}@{self.creds.domain}"
        call_id = str(uuid.uuid4()).upper()
        local_tag = _rand_token(10)
        cseq = random.randint(1000, 8000)

        msg = self._new_request(
            "INVITE", remote_uri,
            call_id=call_id, from_uri=self._local_uri, from_tag=local_tag,
            to_uri=remote_uri, cseq=cseq,
        )
        if self._service_route:
            # capture: lr=lr quirk preserved verbatim
            msg.headers.insert(0, ("Route", self._service_route.replace(";lr", ";lr=lr", 1)))
        msg.headers.append(("P-Preferred-Identity", self._p_associated or self._local_uri))
        if self.place_call_context:
            msg.headers.append(
                ("X-GV-PlaceCallContext", f"jspb={self.place_call_context}"))
        msg.headers.append((
            "Contact",
            f"<sip:{self._contact_user}@{self._via_host};transport=wss>",
        ))
        msg.headers.append(("Content-Type", "application/sdp"))
        msg.headers.append(("Session-Expires", "90"))
        msg.headers.append(("Supported", "timer,100rel,ice,replaces,outbound,record-aware"))
        msg.body = sdp_offer

        dialog = Dialog(
            call_id=call_id, local_tag=local_tag,
            local_uri=self._local_uri, remote_uri=remote_uri,
            cseq=cseq, invite_branch="",
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
        if d.answered:
            await self._send_in_dialog("BYE")
        else:
            # cancel the pending INVITE
            d.cseq += 1
            msg = self._dialog_request("CANCEL")
            msg.headers.append(("Reason", 'SIP ;cause=487 ;text="Cancelled"'))
            await self._send(msg)
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
        # responses to in-dialog requests we sent (BYE/UPDATE/CANCEL/PRACK w/o track)
        log.debug("unmatched response %s %s", msg.status_code, msg.method)

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
            self._emit("failed", {"code": code, "reason": msg.reason})
            self.dialog = None

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
        invite_cseq = cseq_hdr.split()[0] if cseq_hdr else str(d.cseq)
        msg = self._dialog_request("PRACK")
        msg.headers.append(("RAck", f"{rseq} {invite_cseq} INVITE"))
        try:
            resp = await self._request(msg, timeout=15)
            log.info("PRACK rseq=%s -> %s", rseq, resp.status_code)
        except Exception as e:
            log.warning("PRACK rseq=%s failed: %s", rseq, e)
            self._emit("prack_failed", {"rseq": rseq, "error": str(e)})

    async def _send_ack(self, d: Dialog):
        msg = self._dialog_request("ACK", increment=False)
        await self._send(msg)

    def _dialog_request(self, method: str, increment: bool = True) -> SipMessage:
        d = self.dialog
        assert d
        if increment:
            d.cseq += 1
        target = d.remote_target or d.remote_uri
        msg = self._new_request(
            method, target,
            call_id=d.call_id,
            from_uri=d.local_uri, from_tag=d.local_tag,
            to_uri=d.remote_uri, to_tag=d.remote_tag,
            cseq=d.cseq,
        )
        msg.headers = [("Route", r) for r in d.route_set] + msg.headers
        msg.headers.append(("Supported", "outbound,record-aware"))
        return msg

    async def _send_in_dialog(self, method: str, body: str = "",
                              content_type: str = ""):
        msg = self._dialog_request(method)
        if body:
            msg.headers.append(("Content-Type", content_type))
            msg.body = body
        return await self._request(msg)

    # ── session timer (we are refresher=uac) ──

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
                    msg = self._dialog_request("UPDATE")
                    msg.headers.append(
                        ("Session-Expires", f"{d.session_expires};refresher=uac"))
                    await self._request(msg, timeout=15)
                except Exception as e:
                    log.warning("session refresh failed: %s", e)
                    self._emit("session_refresh_failed", {"error": str(e)})
                    return
        except asyncio.CancelledError:
            pass

    # ── incoming requests ──

    async def _handle_incoming_request(self, msg: SipMessage):
        method = msg.method
        if method == "BYE":
            await self._respond(msg, 200, "OK", extra=[
                ("P-RTP-Stat", ""),           # filled by media layer later
                ("Reason", 'Google ;cause=0 ;text=""'),
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
                    log.warning("sdp_answerer failed: %s", e)
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
        resp = SipMessage(is_request=False, status_code=code, reason=reason)
        resp.headers = [(n, v) for n, v in req.headers
                        if n.lower() in ("via", "to", "from", "call-id", "cseq")]
        # make sure To carries our local tag for in-dialog responses
        d = self.dialog
        if d and req.method != "INVITE":
            for i, (n, v) in enumerate(resp.headers):
                if n.lower() == "to" and "tag=" not in v:
                    resp.headers[i] = (n, f"{v};tag={d.local_tag}")
        resp.headers.append(("Supported", "outbound,record-aware"))
        resp.headers.append(("X-Google-Client-Info", X_GOOGLE_CLIENT_INFO))
        for h in (extra or []):
            resp.headers.append(h)
        if body:
            resp.headers.append(("Content-Type", content_type))
            resp.body = body
        data = resp.to_bytes()
        log.debug("SIP C→S\n%s", data.decode(errors="replace"))
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
