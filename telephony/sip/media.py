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
DTMF digits arrive in-band in the audio — the existing Goertzel detector
in audio/ handles them, so no telephone-event parsing is needed here.
"""
from __future__ import annotations

import asyncio
import time
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

import os

# ICE never converges without STUN (host candidates only) — Chrome's web
# client uses Google's own STUN. Override via SIP_STUN_SERVERS="stun:host:port,...".
DEFAULT_STUN = os.getenv("SIP_STUN_SERVERS", "stun:stun.l.google.com:19302")

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
            self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=250)
            self._leftover = b""
            self._start: Optional[float] = None
            self._timestamp = 0
            self.enabled = True

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

            try:
                payload = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                payload = b"\x00" * FRAME_BYTES
            samples = np.frombuffer(payload, dtype=np.float32)
            pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)

            frame = av.AudioFrame.from_ndarray(
                pcm16.reshape(1, -1), format="s16", layout="mono",
            )
            frame.sample_rate = SAMPLE_RATE
            frame.pts = self._timestamp
            frame.time_base = av.Fraction(1, SAMPLE_RATE)
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

        self.pc.addTransceiver(self._out_track, direction="sendrecv")

        @self.pc.on("track")
        def _on_track(track):
            if track.kind == "audio" and self._recv_task is None:
                self._recv_task = asyncio.create_task(self._reader(track))

        @self.pc.on("connectionstatechange")
        async def _on_state():
            self.state = self.pc.connectionState
            if self.on_state:
                try:
                    self.on_state(self.state)
                except Exception:
                    pass
            logger.info(f"sip media: connectionState={self.state}")

    # ── SDP ──

    async def create_offer(self) -> str:
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        # aiortc trickles nothing by default — localDescription is complete
        # once ICE gathering finishes.
        return self.pc.localDescription.sdp

    async def accept_answer(self, sdp: str):
        if self.pc.signalingState != "have-local-offer":
            # A second SDP (200 OK after a reliable 183) must be identical —
            # already applied; ignore.
            return
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type="answer"))
        logger.info("sip media: remote answer applied")

    # ── audio out ──

    def push_pcm(self, pcm: bytes):
        if not self._closed:
            self._out_track.push(pcm)

    def flush_playback(self):
        self._out_track.flush()

    # ── audio in ──

    async def _reader(self, track):
        try:
            while not self._closed:
                frame = await track.recv()
                try:
                    arr = frame.to_ndarray()          # (ch, n) — s16 or fltp
                except Exception:
                    continue
                if arr.size == 0:
                    continue
                if arr.ndim == 2:
                    arr = arr.mean(axis=0)
                if arr.dtype == np.int16:
                    arr = arr.astype(np.float32) / 32768.0
                else:
                    arr = arr.astype(np.float32)
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

    async def close(self):
        self._closed = True
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        try:
            await self.pc.close()
        except Exception:
            pass
