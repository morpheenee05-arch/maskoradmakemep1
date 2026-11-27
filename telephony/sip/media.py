"""
WebRTC media for the SIP backend (aiortc).

Google Voice's web phone is a plain WebRTC endpoint: the INVITE carries an
SDP offer (opus, UDP/TLS/RTP/SAVPF), the 183/200 carries the answer, then
audio flows as SRTP. This module owns exactly one call's media:

  SipMediaSession
    create_offer()   → SDP string for sip_ws.GVWebPhone.start_call()
    accept_answer()  → feed the 183 (early media) or 200 OK SDP
    push_pcm()       → float32 48kHz mono playback (TTS etc.)
    on_audio         → callback(float32 48kHz mono bytes) remote audio
    on_state         → callback("connected" | "disconnected" | "failed" | "closed")

Playback is a pull-model AudioStreamTrack: whatever is queued is sent,
silence otherwise (keeps packet cadence so the far end never sees gaps).
DTMF digits arrive two ways: in-band in the audio (Goertzel detector in
audio/) or out-of-band as RFC 4733 telephone-event RTP — the far end's
carrier often sends DTMF as signaling, which never appears in the opus
stream at all, so telephone-event support here is what makes key presses
reliable on those calls (see _patch_chrome_codec_table).
"""
from __future__ import annotations

import asyncio
import time
from fractions import Fraction
from typing import Callable, Optional

import numpy as np
from loguru import logger

try:
    import av
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import AudioStreamTrack
    _HAS_AIORTC = True
except ImportError:  # pragma: no cover - depends on agent env
    _HAS_AIORTC = False


def _patch_dtls12_only():
    """Fix aiortc's DTLS context to interoperate with Google's media servers.

    Two patches on top of aiortc's RTCCertificate._create_ssl_context:

    1. Pin DTLS to 1.2 — OpenSSL 3.2+ offers DTLS 1.3 by default; Google's
       media servers predate it (pyOpenSSL has no DTLS version constants,
       0xFEFD is DTLS 1.2).
    2. Add ECDHE-RSA cipher suites — aiortc offers ECDHE-ECDSA only (its own
       cert is ECDSA), but the cipher list also constrains the SERVER's cert
       type. Google's media servers answer an all-ECDSA ClientHello with
       fatal handshake_failure (confirmed by wire dump: alert desc=40 right
       after our ClientHello), i.e. they present an RSA certificate. Chrome
       offers both families, which is why the browser works. Server
       authenticity is still enforced via the SDP fingerprint check, so
       accepting an RSA cert weakens nothing.
    """
    if not _HAS_AIORTC:
        return
    try:
        from OpenSSL import SSL
        from aiortc.rtcdtlstransport import RTCCertificate
        if getattr(RTCCertificate._create_ssl_context, "_dtls12_pinned", False):
            return
        orig = RTCCertificate._create_ssl_context
        DTLS1_2 = 0xFEFD
        # aiortc's list + RSA-auth counterparts (mirrors Chrome's WebRTC offer)
        CIPHER_LIST = (
            b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
            b"ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
            b"ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES128-SHA:"
            b"ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA"
        )

        def patched(self, srtp_profiles):
            ctx = orig(self, srtp_profiles)
            ctx.set_min_proto_version(DTLS1_2)
            ctx.set_max_proto_version(DTLS1_2)
            ctx.set_cipher_list(CIPHER_LIST)
            return ctx

        patched._dtls12_pinned = True
        RTCCertificate._create_ssl_context = patched
        logger.info(
            "sip media: DTLS pinned to 1.2 + ECDHE-RSA suites enabled "
            f"(pyOpenSSL={__import__('OpenSSL').__version__} "
            f"{SSL.SSLeay_version(SSL.SSLEAY_VERSION).decode()})")
    except Exception as e:
        logger.warning(f"sip media: could not patch DTLS context: {e}")


_patch_dtls12_only()


_DTLS_ALERTS = {
    20: "close_notify", 21: "unexpected_message", 22: "bad_record_mac",
    40: "handshake_failure", 42: "bad_certificate",
    43: "unsupported_certificate", 44: "certificate_revoked",
    45: "certificate_expired", 46: "certificate_unknown",
    47: "illegal_parameter", 48: "unknown_ca", 49: "access_denied",
    50: "decode_error", 51: "decrypt_error", 70: "protocol_version",
    71: "insufficient_security", 80: "internal_error",
    86: "inappropriate_fallback", 90: "user_canceled",
    109: "missing_extension", 110: "unsupported_extension",
    112: "unrecognized_name", 113: "bad_certificate_status_response",
    115: "unknown_psk_identity", 116: "certificate_required",
    120: "no_application_protocol",
}

_DTLS_HANDSHAKES = {
    0: "hello_request", 1: "client_hello", 2: "server_hello",
    3: "hello_verify_request", 4: "new_session_ticket",
    8: "encrypted_extensions", 11: "certificate",
    12: "server_key_exchange", 13: "certificate_request",
    14: "server_hello_done", 15: "certificate_verify",
    16: "client_key_exchange", 20: "finished",
}


def _describe_dtls(direction: str, data: bytes) -> str:
    """Human-readable one-liner for a DTLS record (RFC 7983 demuxed)."""
    ct = data[0]
    ver = f"{data[1]:02x}{data[2]:02x}"
    epoch = int.from_bytes(data[3:5], "big")
    body = data[13:]
    parts = [
        f"{direction} len={len(data)} type={ct} ver={ver} epoch={epoch}"
    ]
    full_hex = False
    if epoch > 0:
        # post-handshake records are encrypted — decoding the payload as
        # plaintext alert/handshake types only produces garbage
        parts.append("(encrypted)")
        parts.append(f"hex={data[:48].hex()}")
        return " ".join(parts)
    if ct == 21 and len(body) >= 2:  # alert — the answer we are looking for
        parts.append(
            f"ALERT level={body[0]} desc={body[1]}"
            f"({_DTLS_ALERTS.get(body[1], '?')})")
        full_hex = True
    elif ct == 22 and len(body) >= 4:  # handshake
        ht = body[0]
        hlen = int.from_bytes(body[1:4], "big")
        parts.append(f"handshake={_DTLS_HANDSHAKES.get(ht, ht)} hlen={hlen}")
        if ht == 3 and len(body) >= 7:  # HelloVerifyRequest → cookie size
            parts.append(f"cookie_len={body[6]}")
        full_hex = ht in (1, 2, 3)  # client/server hello in full
    limit = len(data) if full_hex else 48
    parts.append(f"hex={data[:limit].hex()}")
    return " ".join(parts)


def _patch_dtls_wire_dump():
    """Hexdump DTLS datagrams on the wire (first 40 per connection).

    Google's media server kills our handshake with a bare SSL.Error — the
    only way to see *why* is to look at what it actually sends back
    (alert? HelloVerifyRequest? garbage?). Logs via loguru INFO so the
    agent's sink keeps them.
    """
    if not _HAS_AIORTC:
        return
    try:
        from aioice.ice import Connection
        if getattr(Connection.send, "_dtls_dump", False):
            return
        orig_send = Connection.send
        orig_recv = Connection.recv
        counts: dict = {}

        def _is_dtls(data: bytes) -> bool:
            return (
                len(data) > 13
                and 20 <= data[0] <= 24      # ccs/alert/handshake/appdata
                and data[1] == 0xFE          # DTLS version family
            )

        def _allow(conn) -> bool:
            n = counts.get(id(conn), 0)
            if n >= 40:
                return False
            counts[id(conn)] = n + 1
            return True

        async def send_dump(self, data: bytes) -> None:
            if _is_dtls(data) and _allow(self):
                logger.info(f"sip dtls-wire {_describe_dtls('tx', data)}")
            return await orig_send(self, data)

        async def recv_dump(self) -> bytes:
            data = await orig_recv(self)
            if _is_dtls(data) and _allow(self):
                logger.info(f"sip dtls-wire {_describe_dtls('rx', data)}")
            return data

        send_dump._dtls_dump = True
        Connection.send = send_dump
        Connection.recv = recv_dump
        logger.info("sip media: DTLS wire dump enabled (40 datagrams/call)")
    except Exception as e:
        logger.warning(f"sip media: could not enable DTLS wire dump: {e}")


_patch_dtls_wire_dump()


def _patch_chrome_codec_table():
    """Shape aiortc's static codec table like Chrome 158's audio offer.

    The captured web client offers 'm=audio … 111 63 9 0 8 13 110 101'
    (opus, red, G722, PCMU, PCMA, CN, telephone-event/48000, telephone-event
    /8000) and Google's SBC mirrors the offer's payload types in its answer,
    so the pts must be exact, not arbitrary. aiortc's stock table is
    '96 9 0 8' (+101 te via our old patch) — visibly non-Chrome to anyone
    inspecting the INVITE.

    red (63) and CN (13) have no encoder/decoder in aiortc — that is fine:
    the send codec is negotiated as opus (Google always picks 111), and
    incoming red/CN packets never arrive because the answer omits them.

    Incoming telephone-event packets are intercepted in _handle_rtp_data
    before the jitter buffer: aiortc has no decoder for them, and feeding
    one into the decode thread raises ValueError and kills audio decoding
    for the rest of the call.
    """
    if not _HAS_AIORTC:
        return
    try:
        from aiortc.codecs import CODECS
        from aiortc.rtcrtpparameters import RTCRtpCodecParameters
        audio = CODECS["audio"]
        if any(c.payloadType == 111 for c in audio):
            return                          # already shaped
        keep = {c.payloadType: c for c in audio}  # reuse G722/PCMU/PCMA objs
        CODECS["audio"] = [
            RTCRtpCodecParameters(
                mimeType="audio/opus", clockRate=48000, channels=2,
                payloadType=111),
            RTCRtpCodecParameters(
                mimeType="audio/red", clockRate=48000, channels=2,
                payloadType=63),
            keep[9],                        # G722
            keep[0],                        # PCMU
            keep[8],                        # PCMA
            RTCRtpCodecParameters(
                mimeType="audio/CN", clockRate=8000, channels=1,
                payloadType=13),
            RTCRtpCodecParameters(
                mimeType="audio/telephone-event", clockRate=48000,
                payloadType=110),
            RTCRtpCodecParameters(
                mimeType="audio/telephone-event", clockRate=8000,
                payloadType=101),
        ]
        logger.info("sip media: codec table shaped like Chrome "
                    "(111 63 9 0 8 13 110 101)")
    except Exception as e:
        logger.warning(f"sip media: codec table patch failed: {e}")


_patch_chrome_codec_table()


def _chrome_shaped_offer(sdp: str) -> str:
    """Rewrite aiortc's offer SDP into Chrome 158's exact layout.

    The payload types are already correct (the codec table is patched), so
    this only re-orders attributes, drops aiortc-isms (3 fingerprints,
    uuid cname, 'msid-semantic:WMS *', real c= IP/port) and adds Chrome-isms
    (extmap-allow-mixed, ice-options:trickle, rtcp-rsize/xr/fb, fmtp lines,
    ssrc-msid). ICE credentials, candidates and the sha-256 fingerprint are
    copied verbatim — those are validated by the DTLS/ICE layers.

    Applied AFTER setLocalDescription, so aiortc's internal state always
    matches its own offer; only the on-wire text is shaped. Returns "" when
    the input can't be parsed (caller falls back to the raw aiortc offer).
    """
    try:
        lines = sdp.replace("\r\n", "\n").split("\n")
        ufrag = pwd = fp = ssrc = ""
        msid_parts = []
        candidates = []
        for line in lines:
            if line.startswith("a=ice-ufrag:"):
                ufrag = line.split(":", 1)[1]
            elif line.startswith("a=ice-pwd:"):
                pwd = line.split(":", 1)[1]
            elif line.startswith("a=fingerprint:sha-256 "):
                fp = line[len("a=fingerprint:sha-256 "):]
            elif line.startswith("a=ssrc:"):
                rest = line[len("a=ssrc:"):]
                ssrc = rest.split(" ", 1)[0]
            elif line.startswith("a=msid:"):
                msid_parts = line[len("a=msid:"):].split(" ")
            elif line.startswith("a=candidate:"):
                candidates.append(line)
        if not (ufrag and pwd and fp and ssrc and len(msid_parts) >= 2):
            return ""
        import random as _rnd
        import string as _str
        sess_id = _rnd.randint(10**18, 10**19 - 1)
        cname = "".join(_rnd.choice(
            _str.ascii_letters + _str.digits + "+/") for _ in range(16))
        out = [
            "v=0",
            f"o=- {sess_id} 2 IN IP4 127.0.0.1",
            "s=-",
            "t=0 0",
            "a=group:BUNDLE 0",
            "a=extmap-allow-mixed",
            f"a=msid-semantic: WMS {msid_parts[0]}",
            "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 101",
            "c=IN IP4 0.0.0.0",
            "a=rtpmap:111 opus/48000/2",
            "a=fmtp:111 minptime=10;useinbandfec=1",
            "a=rtpmap:63 red/48000/2",
            "a=fmtp:63 111/111",
            "a=rtpmap:9 G722/8000",
            "a=rtpmap:0 PCMU/8000",
            "a=rtpmap:8 PCMA/8000",
            "a=rtpmap:13 CN/8000",
            "a=rtpmap:110 telephone-event/48000",
            "a=rtpmap:101 telephone-event/8000",
            "a=rtcp:9 IN IP4 0.0.0.0",
            f"a=ice-ufrag:{ufrag}",
            f"a=ice-pwd:{pwd}",
            "a=ice-options:trickle",
            f"a=fingerprint:sha-256 {fp}",
            "a=setup:actpass",
            "a=mid:0",
            "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level",
            "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time",
            "a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01",
            "a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid",
            "a=sendrecv",
            f"a=msid:{msid_parts[0]} {msid_parts[1]}",
            "a=rtcp-mux",
            "a=rtcp-rsize",
            "a=rtcp-xr:rcvr-rtt=all",
            f"a=ssrc:{ssrc} cname:{cname}",
            f"a=ssrc:{ssrc} msid:{msid_parts[0]} {msid_parts[1]}",
            "a=rtcp-fb:111 transport-cc",
        ]
        out += candidates
        out.append("a=end-of-candidates")
        return "\r\n".join(out) + "\r\n"
    except Exception as e:
        logger.warning(f"sip media: Chrome SDP shaping failed: {e}")
        return ""


# RFC 4733 event codes → keypad characters
_TE_EVENTS = {**{i: str(i) for i in range(10)},
              10: "*", 11: "#", 12: "A", 13: "B", 14: "C", 15: "D"}

import os

# ICE never converges without STUN (host candidates only) — Chrome's web
# client uses Google's own STUN. Override via SIP_STUN_SERVERS="stun:host:port,...".
DEFAULT_STUN = os.getenv("SIP_STUN_SERVERS", "stun:stun.l.google.com:19302")

# Bumped on every media-related fix — the agent startup self-check logs it,
# so a stale deploy is obvious from the first log lines.
SIP_BUILD = "2026-11-26.1"


def enable_aiortc_logging():
    """Forward aiortc/aioice records into loguru so ICE/DTLS failure
    reasons are visible in the agent log (DEBUG with SIP_MEDIA_DEBUG=1)."""
    import logging

    class _LoguruHandler(logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
                level = record.levelname
                # The agent's loguru sink usually filters DEBUG out — but
                # aiortc reports every failure reason at DEBUG only ("x ..."
                # is its error marker). Escalate those so they survive.
                if level == "DEBUG" and (msg.startswith("x ") or "DTLS" in msg):
                    level = "INFO"
                logger.log(level, f"{record.name}: {msg}")
            except Exception:
                pass

    # aiortc always at DEBUG: DTLS failure reasons (fingerprint mismatch,
    # no SRTP profile, SSL error text) are only logged there, and the
    # per-call volume is small. aioice stays INFO (its per-check lines are
    # noisy enough); SIP_MEDIA_DEBUG=1 unlocks its DEBUG too.
    handler = _LoguruHandler()
    for name, level in (
        ("aiortc", logging.DEBUG),
        ("aioice", logging.DEBUG if os.getenv("SIP_MEDIA_DEBUG") else logging.INFO),
    ):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        if handler not in lg.handlers:
            lg.addHandler(handler)

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960          # 20 ms @ 48 kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 4  # float32


class AiortcMissingError(RuntimeError):
    def __init__(self):
        super().__init__(
            "aiortc is not installed on this agent — run "
            "'py -3 -m pip install aiortc av' (media is required for SIP calls)"
        )


if _HAS_AIORTC:

    class _PcmQueueTrack(AudioStreamTrack):
        """48kHz mono PCM source: queued frames, silence when empty."""

        kind = "audio"

        def __init__(self):
            super().__init__()
            # 20 ms frames: 3000 = 60 s of buffered audio. The IVR pushes a
            # whole clip at once (intro ~19 s); a 250-frame (5 s) buffer made
            # push() drop everything but the clip's tail, so the callee heard
            # ~5 s of mid-phrase audio then silence and hung up.
            self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3000)
            self._leftover = b""
            self._start: Optional[float] = None
            self._timestamp = 0
            self.enabled = True
            # One increment per recv() == one RTP packet on the wire.
            self.packets_sent = 0
            # Tapped per transmitted frame (real-time pace) so the call
            # recorder captures what actually went on the wire.
            self.on_frame_sent: Optional[Callable[[bytes], None]] = None

        def flush(self):
            self._leftover = b""
            try:
                while True:
                    self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

        def push(self, pcm: bytes):
            """Queue float32 48kHz mono bytes for transmission."""
            data = self._leftover + pcm
            whole = len(data) // FRAME_BYTES * FRAME_BYTES
            self._leftover = data[whole:]
            data = data[:whole]
            for off in range(0, len(data), FRAME_BYTES):
                try:
                    self._queue.put_nowait(data[off:off + FRAME_BYTES])
                except asyncio.QueueFull:
                    # drop the oldest frame — better to skip than to lag
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._queue.put_nowait(data[off:off + FRAME_BYTES])
                    except asyncio.QueueFull:
                        pass

        async def recv(self):
            if self._start is None:
                self._start = time.time()
                self._timestamp = 0
            else:
                self._timestamp += SAMPLES_PER_FRAME
                wait = self._start + (self._timestamp / SAMPLE_RATE) - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
            self.packets_sent += 1

            try:
                payload = self._queue.get_nowait()
                got_audio = True
            except asyncio.QueueEmpty:
                payload = b"\x00" * FRAME_BYTES
                got_audio = False
            if got_audio:
                cb = self.on_frame_sent
                if cb is not None:
                    try:
                        cb(payload)
                    except Exception:
                        pass
            samples = np.frombuffer(payload, dtype=np.float32)
            pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)

            frame = av.AudioFrame.from_ndarray(
                pcm16.reshape(1, -1), format="s16", layout="mono",
            )
            frame.sample_rate = SAMPLE_RATE
            frame.pts = self._timestamp
            # PyAV >= 19 dropped av.Fraction — stdlib one is what aiortc
            # itself uses everywhere.
            frame.time_base = Fraction(1, SAMPLE_RATE)
            return frame


class SipMediaSession:
    """One call's WebRTC media leg (offer/answer + opus audio both ways)."""

    def __init__(self,
                 on_audio: Optional[Callable[[bytes], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None,
                 ice_servers: Optional[list] = None):
        if not _HAS_AIORTC:
            raise AiortcMissingError()
        self.on_audio = on_audio
        self.on_state = on_state
        # RFC 4733 DTMF: callback(key: str) fired on telephone-event packets.
        self.on_dtmf: Optional[Callable[[str], None]] = None
        self._te_payload_type: Optional[int] = None  # from the answer SDP
        self._te_last_press = None                   # dedupe one press
        self._te_transport = None
        self._rtp_wrapped = False
        if ice_servers is None:
            ice_servers = [
                RTCIceServer(urls=[u.strip()])
                for u in DEFAULT_STUN.split(",") if u.strip()
            ]
        config = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration=config)
        self._out_track = _PcmQueueTrack()
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False
        self.state = "new"
        # Live RTP accounting for the P-RTP-Stat header of our 200 OK to BYE.
        self._in_pk = 0
        self._connected_at: Optional[float] = None

        self.pc.addTransceiver(self._out_track, direction="sendrecv")
        self._transceiver = self.pc.getTransceivers()[0]

        @self.pc.on("track")
        def _on_track(track):
            if track.kind == "audio" and self._recv_task is None:
                self._recv_task = asyncio.create_task(self._reader(track))

        @self.pc.on("connectionstatechange")
        async def _on_state():
            self.state = self.pc.connectionState
            if self.state == "connected" and self._connected_at is None:
                self._connected_at = time.time()
            if self.on_state:
                try:
                    self.on_state(self.state)
                except Exception:
                    pass
            logger.info(f"sip media: connectionState={self.state}")

        @self.pc.on("iceconnectionstatechange")
        async def _on_ice_state():
            logger.info(
                f"sip media: iceConnectionState={self.pc.iceConnectionState} "
                f"gathering={self.pc.iceGatheringState}")

    # ── SDP ──

    async def create_offer(self) -> str:
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        self._wrap_rtp_intercept()
        # aiortc trickles nothing by default — localDescription is complete
        # once ICE gathering finishes.
        sdp = self.pc.localDescription.sdp
        shaped = _chrome_shaped_offer(sdp)
        if shaped:
            sdp = shaped
        logger.info(
            f"sip media: offer ready "
            f"(local candidates={sdp.count('a=candidate')})")
        return sdp

    def _wrap_rtp_intercept(self):
        """Intercept telephone-event RTP before aiortc's receiver sees it.

        The receiver has no telephone-event decoder — a single such packet
        would crash its decode thread (ValueError from get_decoder) and kill
        inbound audio for the rest of the call. We consume matching packets
        here and turn them into on_dtmf callbacks instead.
        """
        if self._rtp_wrapped:
            return
        try:
            transport = self._transceiver.receiver.transport
            if transport is None:
                return
            orig = transport._handle_rtp_data

            async def _patched(data, arrival_time_ms, _orig=orig):
                te_pt = self._te_payload_type
                if (te_pt is not None and len(data) > 12
                        and data[0] >> 6 == 2
                        and (data[1] & 0x7F) == te_pt):
                    self._on_te_packet(data)
                    return
                await _orig(data, arrival_time_ms)

            transport._handle_rtp_data = _patched
            self._te_transport = transport
            self._rtp_wrapped = True
        except Exception as e:
            logger.debug(f"sip media: rtp intercept wrap failed: {e}")

    def _on_te_packet(self, data: bytes):
        """Parse one RFC 4733 telephone-event packet → on_dtmf(key)."""
        try:
            from aiortc.rtp import RtpPacket
            ext_map = self._te_transport._rtp_header_extensions_map
            packet = RtpPacket.parse(data, ext_map)
        except Exception:
            return
        payload = bytes(packet.payload or b"")
        if len(payload) < 4:
            return
        key = _TE_EVENTS.get(payload[0])
        if key is None:
            return
        end = bool(payload[1] & 0x80)
        volume = payload[1] & 0x3F
        duration = int.from_bytes(payload[2:4], "big")
        # One press = many packets sharing the same RTP timestamp (plus
        # retransmitted end markers) — emit only on the first packet seen.
        press_id = (packet.ssrc, packet.timestamp, payload[0])
        if press_id == self._te_last_press:
            return
        self._te_last_press = press_id
        logger.info(
            f"sip media: telephone-event DTMF '{key}' "
            f"(end={end} volume={volume} duration={duration})")
        cb = self.on_dtmf
        if cb is not None:
            try:
                cb(key)
            except Exception:
                pass

    async def accept_answer(self, sdp: str):
        if self.pc.signalingState != "have-local-offer":
            # A second SDP (200 OK after a reliable 183) must be identical —
            # already applied; ignore.
            return
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type="answer"))
        self._te_payload_type = self._sdp_telephone_event_pt(sdp)
        n_cand = sdp.count("a=candidate")
        setup = next((l for l in sdp.splitlines() if l.startswith("a=setup")), "?")
        codecs = self._sdp_codecs(sdp)
        logger.info(
            f"sip media: remote answer applied "
            f"(candidates={n_cand} {setup} ice-lite={'a=ice-lite' in sdp}) "
            f"codecs={codecs}")
        if self._te_payload_type is not None:
            logger.info(
                f"sip media: telephone-event negotiated (pt="
                f"{self._te_payload_type}) — out-of-band DTMF live")
        else:
            logger.info(
                "sip media: no telephone-event in answer — DTMF is "
                "in-band only on this call")

    @staticmethod
    def _sdp_telephone_event_pt(sdp: str) -> Optional[int]:
        """Payload type the answer assigned to telephone-event (m=audio)."""
        in_audio = False
        for line in sdp.splitlines():
            if line.startswith("m="):
                in_audio = line.startswith("m=audio")
            elif in_audio and line.startswith("a=rtpmap:"):
                try:
                    pt, desc = line[len("a=rtpmap:"):].split(None, 1)
                except ValueError:
                    continue
                if desc.lower().startswith("telephone-event"):
                    try:
                        return int(pt)
                    except ValueError:
                        return None
        return None

    @staticmethod
    def _sdp_codecs(sdp: str) -> str:
        """'m=audio ... 0 8 96' + rtpmap → '0=PCMU/8000, 96=opus/48000'."""
        payloads: list[str] = []
        rtpmap: dict[str, str] = {}
        for line in sdp.splitlines():
            if line.startswith("m=audio"):
                parts = line.split()
                payloads = parts[3:]
            elif line.startswith("a=rtpmap:"):
                try:
                    pt, desc = line[len("a=rtpmap:"):].split(None, 1)
                    if pt in payloads:
                        rtpmap[pt] = desc
                except ValueError:
                    pass
        return ", ".join(f"{pt}={rtpmap.get(pt, '?')}" for pt in payloads)

    # ── audio out ──

    def push_pcm(self, pcm: bytes):
        if not self._closed:
            self._out_track.push(pcm)

    def flush_playback(self):
        self._out_track.flush()

    def set_on_frame_sent(self, cb: Optional[Callable[[bytes], None]]):
        """Tap float32 48kHz frames at the moment they go on the wire."""
        self._out_track.on_frame_sent = cb

    # ── audio in ──

    async def _reader(self, track):
        frames = 0
        samples_total = 0
        rate_seen = None
        started = None
        try:
            while not self._closed:
                frame = await track.recv()
                frames += 1
                self._in_pk += 1
                if started is None:
                    started = time.time()
                samples_total += int(getattr(frame, "samples", 0) or 0)
                rate_seen = frame.sample_rate or rate_seen
                if frames == 1:
                    logger.info(
                        "sip media: remote audio flowing "
                        "(first inbound frame — far end is being heard)")
                if started is not None and frames % 250 == 0:
                    elapsed = time.time() - started
                    if elapsed > 0.5:
                        logger.info(
                            f"sip media inbound rate: {samples_total / elapsed:.0f} "
                            f"samples/s over {elapsed:.1f}s "
                            f"(frames={frames}, {samples_total / frames:.0f} "
                            f"samples/frame, frame_rate={rate_seen})")
                try:
                    arr = frame.to_ndarray()          # (ch, n) planar or (1, C*n) packed
                except Exception:
                    continue
                if arr.size == 0:
                    continue
                if frames == 1:
                    logger.info(
                        f"sip media inbound fmt: format={frame.format.name} "
                        f"layout={frame.layout.name} shape={tuple(arr.shape)} "
                        f"rate={frame.sample_rate}")
                # Channel count from the frame layout — needed to unpack
                # interleaved (packed) buffers, which arrive as (1, C*n).
                try:
                    n_ch = max(1, len(frame.layout.channels))
                except Exception:
                    n_ch = 1
                # Normalize BEFORE downmix: mean() on int16 promotes to
                # float64 while keeping the ±32768 scale, so the dtype check
                # must happen first or the samples stay int16-scaled floats
                # (and get hard-clipped to a square wave downstream).
                if arr.dtype == np.int16:
                    arr = arr.astype(np.float32) / 32768.0
                else:
                    arr = arr.astype(np.float32)
                if arr.ndim == 2 and arr.shape[0] == 1 and n_ch > 1:
                    # Packed/interleaved samples (LRLR…) laid out as a single
                    # plane. Reading them as mono doubles the duration and
                    # halves the pitch — reshape to (n, C) and downmix for real.
                    arr = arr.reshape(-1, n_ch).mean(axis=1, dtype=np.float32)
                elif arr.ndim == 2:
                    arr = arr.mean(axis=0, dtype=np.float32)
                rate = frame.sample_rate or SAMPLE_RATE
                if rate != SAMPLE_RATE:
                    from audio.engine import resample
                    arr = resample(arr, rate, SAMPLE_RATE)
                if self.on_audio is not None:
                    try:
                        self.on_audio(arr.tobytes())
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"sip media reader ended: {e}")

    # ── lifecycle ──

    def rtp_stat(self) -> str:
        """P-RTP-Stat value for our 200 OK to BYE (captured web client sends
        'PS=…, OS=…, PR=…, OR=…, PL=0, JI=0, LA=…, DU=…, NP=1').

        Packet counts are real (one per sent recv() / received frame); the
        octet figures approximate an opus payload of ~35 bytes/packet —
        Google only displays these, it never validates them.
        """
        ps = self._out_track.packets_sent
        pr = self._in_pk
        du = int(time.time() - self._connected_at) if self._connected_at else 0
        return (f"PS={ps}, OS={ps * 35}, PR={pr}, OR={pr * 38}, "
                f"PL=0, JI=0, LA=0, DU={du}, NP=1")

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        # Per-call RTP accounting: proves our outbound audio actually left the
        # host (OUT bytes ≈ intro length) instead of dying in the encoder.
        try:
            out_pk = out_b = in_pk = in_b = 0
            for r in (await self.pc.getStats()).values():
                if getattr(r, "type", None) == "outbound-rtp":
                    out_pk += getattr(r, "packetsSent", 0) or 0
                    out_b += getattr(r, "bytesSent", 0) or 0
                elif getattr(r, "type", None) == "inbound-rtp":
                    in_pk += getattr(r, "packetsReceived", 0) or 0
                    in_b += getattr(r, "bytesReceived", 0) or 0
            logger.info(
                f"sip media stats: OUT {out_pk} pkts/{out_b // 1024}KB · "
                f"IN {in_pk} pkts/{in_b // 1024}KB"
            )
        except Exception:
            pass
        try:
            await self.pc.close()
        except Exception:
            pass
