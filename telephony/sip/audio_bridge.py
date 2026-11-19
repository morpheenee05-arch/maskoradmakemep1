"""
SipAudioManager — drop-in replacement for audio.audio_io.AudioManager
used when the call leg is our SIP/WebRTC stack instead of OS audio devices.

Same public surface the ScriptedCallHandler touches:
    initialize / get_output_rate / prepare_streams / start / stop
    set_audio_callback / set_recorder / clear_recorder
    register_sound / play_sound_by_key / play_raw / play_tts / flush_output

Instead of capture/playback subprocesses it talks to the active
telephony.sip.media.SipMediaSession of the owning SipPhoneClient:
  remote opus  → 48k float32 → resample to 24k → callback + recorder
  play_raw     → 48k float32 → SipMediaSession.push_pcm (opus → SRTP)

The handler works at 24kHz on capture (Deepgram rate) and at the device
output rate on playback; we fix the output rate at 48kHz (native opus).
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np
from loguru import logger

from audio.engine import resample

CAPTURE_RATE = 24000       # what the handler/STT/DTMF expects
OUTPUT_RATE = 48000        # native WebRTC rate


class SipAudioManager:
    def __init__(self, client=None):
        self._client = client            # SipPhoneClient; binds per call
        self._session = None             # active SipMediaSession
        self._send_callback: Optional[Callable[[bytes], None]] = None
        self._recorder = None
        self._output_rate = OUTPUT_RATE
        self._initialized = False
        self._running = False
        self._sounds: dict[str, bytes] = {}
        self._id_lock = threading.Lock()
        self._playback_id = 0

    # ── session binding (per call) ──

    def bind_session(self, session):
        """Attach the active call's media session."""
        self.detach_session()
        self._session = session
        session.on_audio = self._on_remote_audio

    def detach_session(self):
        if self._session is not None:
            try:
                self._session.on_audio = None
            except Exception:
                pass
        self._session = None

    # ── remote audio (from SIP media) ──

    def _on_remote_audio(self, pcm48: bytes):
        """float32 48k mono from the SIP stack → 24k for the handler."""
        if not self._running:
            return
        arr = np.frombuffer(pcm48, dtype=np.float32)
        dg = resample(arr, OUTPUT_RATE, CAPTURE_RATE)
        data = dg.tobytes()
        if self._recorder is not None:
            try:
                self._recorder.append_remote(data)
            except Exception:
                pass
        cb = self._send_callback
        if cb is not None:
            try:
                cb(data)
            except Exception:
                pass

    # ── AudioManager-compatible API ──

    async def initialize(self) -> bool:
        self._initialized = True
        return True

    def get_output_rate(self) -> int:
        return self._output_rate

    def prepare_streams(self) -> bool:
        return True

    def set_audio_callback(self, callback: Callable[[bytes], None]):
        self._send_callback = callback

    def start(self):
        self._running = True

    def set_recorder(self, recorder) -> None:
        self._recorder = recorder
        try:
            recorder.set_output_rate(self._output_rate)
        except Exception:
            pass

    def clear_recorder(self, retain: bool = False):
        rec = self._recorder
        self._recorder = None
        if rec is None:
            return None
        try:
            paths = rec.finalize()
        except Exception:
            return None
        if not retain and paths:
            try:
                from audio.call_recorder import CallRecorder
                CallRecorder.delete_paths(paths)
            except Exception:
                pass
            return None
        return paths

    # ── playback ──

    def register_sound(self, key: str, audio_bytes: bytes):
        self._sounds[key] = audio_bytes

    def play_sound_by_key(self, key: str):
        audio = self._sounds.get(key)
        if not audio:
            logger.error(f"Sound clip not found: {key}")
            return
        self.play_raw(audio)

    def play_raw(self, raw_bytes: bytes):
        """float32 at output rate (48k) → opus → SRTP (via media session)."""
        with self._id_lock:
            self._playback_id += 1
            current_id = self._playback_id
        audio = np.frombuffer(raw_bytes, dtype=np.float32)
        audio = np.clip(audio, -1.0, 1.0)
        if self._recorder is not None:
            try:
                self._recorder.append_playback_float32(
                    audio.tobytes(), self._output_rate)
            except Exception:
                pass
        session = self._session
        if session is None:
            return
        CHUNK = 960 * 4  # 20 ms frames
        data = audio.tobytes()
        for off in range(0, len(data), CHUNK):
            with self._id_lock:
                if current_id != self._playback_id:
                    break
            session.push_pcm(data[off:off + CHUNK])

    def flush_output(self):
        with self._id_lock:
            self._playback_id += 1
        session = self._session
        if session is not None:
            try:
                session.flush_playback()
            except Exception:
                pass

    def play_tts(self, audio_bytes: bytes):
        """INT16 24kHz TTS → float32 48k playback."""
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        audio = resample(audio, CAPTURE_RATE, self._output_rate)
        audio = np.clip(audio, -1.0, 1.0)
        self.play_raw(audio.tobytes())

    def stop(self):
        self._running = False
        self.detach_session()
