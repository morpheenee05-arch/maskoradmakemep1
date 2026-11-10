"""
Per-call taps on the VAC pair (capture = remote party, playback = our TTS).

One 24 kHz stereo WAV per call. Left is the cable (the other party). Right is
our playback, placed on that same timeline (silence where we were not talking).
Non-P1 calls delete the file when the call ends. P1 keeps it until Telegram
upload, and only presses 1/2 are uploaded.
"""
from __future__ import annotations

import base64
import os
import tempfile
import threading
import uuid
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger

try:
    import soxr
    _HAS_SOXR = True
except ImportError:
    _HAS_SOXR = False

RATE = 24000
SAMPLE_WIDTH = 2  # int16


def _record_root() -> Path:
    try:
        import config
        custom = getattr(config, "CALL_RECORD_DIR", None) or ""
    except Exception:
        custom = os.getenv("GV_CALL_RECORD_DIR", "").strip()
    if custom:
        root = Path(custom)
    else:
        root = Path(tempfile.gettempdir()) / "gv_call_recordings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _max_bytes() -> int:
    try:
        import config
        mb = int(getattr(config, "P1_RECORD_MAX_MB", 3) or 3)
    except Exception:
        mb = 3
    return max(1, mb) * 1024 * 1024


def _even(pcm: bytes) -> bytes:
    if len(pcm) % 2:
        return pcm[:-1]
    return pcm


def _trim_tail(pcm: bytes, max_bytes: int) -> bytes:
    pcm = _even(pcm)
    if len(pcm) <= max_bytes:
        return pcm
    out = pcm[-max_bytes:]
    return _even(out)


def _fit_pair(remote: bytes, local: bytes, max_bytes: int) -> tuple[bytes, bytes]:
    """Pad the shorter leg, then keep the same tail of both so they still match."""
    remote, local = _even(remote), _even(local)
    n = max(len(remote), len(local))
    if len(remote) < n:
        remote += b"\x00" * (n - len(remote))
    if len(local) < n:
        local += b"\x00" * (n - len(local))
    if n > max_bytes:
        remote = remote[-max_bytes:]
        local = local[-max_bytes:]
    remote, local = _even(remote), _even(local)
    n = min(len(remote), len(local))
    n -= n % 2
    return remote[-n:], local[-n:]


def _write_stereo(path: Path, remote: bytes, local: bytes) -> None:
    """Interleave two equal mono legs: left = remote, right = our audio."""
    left = np.frombuffer(remote, dtype=np.int16)
    right = np.frombuffer(local, dtype=np.int16)
    n = min(left.size, right.size)
    if n <= 0:
        return
    stereo = np.empty(n * 2, dtype=np.int16)
    stereo[0::2] = left[:n]
    stereo[1::2] = right[:n]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(RATE)
        wf.writeframes(stereo.tobytes())


def _resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    if samples.size == 0 or src == dst:
        return samples.astype(np.float32, copy=False)
    if _HAS_SOXR:
        return soxr.resample(samples, src, dst).astype(np.float32)
    new_len = max(1, int(len(samples) * dst / src))
    return np.interp(
        np.linspace(0, len(samples) - 1, new_len),
        np.arange(len(samples)),
        samples,
    ).astype(np.float32)


class CallRecorder:
    """Thread-safe PCM buffers. finalize() writes WAV files."""

    def __init__(self, tag: str = "call"):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:40]
        self._tag = f"{safe or 'call'}_{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._remote = bytearray()
        # (capture sample index, source rate, int16 pcm at source rate)
        self._events: list[tuple[int, int, bytes]] = []
        self._closed = False

    def set_output_rate(self, hz: int) -> None:
        # Playback chunks carry their own rate; kept for callers that set it.
        return

    def append_remote(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            if self._closed:
                return
            self._remote.extend(chunk)

    def append_playback_float32(self, raw_bytes: bytes, source_rate: int) -> None:
        """Queue float32 frames. Resample once in finalize, aligned to capture."""
        if not raw_bytes:
            return
        audio = np.frombuffer(raw_bytes, dtype=np.float32)
        if audio.size == 0:
            return
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        with self._lock:
            if self._closed:
                return
            pos = len(self._remote) // SAMPLE_WIDTH
            self._events.append((pos, int(source_rate or RATE), pcm))

    def _render_local(self, events: list, remote_samples: int) -> bytes:
        if not events:
            return b""
        rendered: list[tuple[int, np.ndarray]] = []
        end = remote_samples
        for pos, src_rate, pcm in events:
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            samples = _resample(samples, int(src_rate or RATE), RATE)
            rendered.append((pos, samples))
            end = max(end, pos + int(samples.size))
        if end <= 0:
            return b""
        timeline = np.zeros(end, dtype=np.float32)
        for pos, samples in rendered:
            n = min(int(samples.size), end - pos)
            if n > 0:
                timeline[pos:pos + n] += samples[:n]
        np.clip(timeline, -1.0, 1.0, out=timeline)
        return (timeline * 32767.0).astype(np.int16).tobytes()

    def finalize(self) -> Optional[dict[str, Path]]:
        """Write WAV files. Returns paths, or None if both legs are empty."""
        with self._lock:
            if self._closed:
                return None
            self._closed = True
            remote = bytes(self._remote)
            events = list(self._events)
            self._remote.clear()
            self._events.clear()

        local = self._render_local(events, len(remote) // SAMPLE_WIDTH)
        cap_limit = _max_bytes()
        if remote and local:
            remote, local = _fit_pair(remote, local, cap_limit)
        elif remote:
            remote = _trim_tail(remote, cap_limit)
        elif local:
            local = _trim_tail(local, cap_limit)
        else:
            return None

        if not remote:
            remote = b"\x00" * len(local)
        if not local:
            local = b"\x00" * len(remote)
        path = _record_root() / f"{self._tag}.wav"
        _write_stereo(path, remote, local)
        if not path.is_file():
            return None
        return {"call": path}

    @staticmethod
    def delete_paths(paths: Optional[dict]) -> None:
        if not paths:
            return
        for p in paths.values():
            try:
                Path(p).unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"call recording delete failed: {e}")


def read_recording_b64(path: Path, max_b64_bytes: int = 9 * 1024 * 1024) -> Optional[str]:
    """Read a WAV for Redis transport. None if missing or too large."""
    p = Path(path)
    if not p.is_file():
        return None
    size = p.stat().st_size
    if size > max_b64_bytes:
        logger.warning(f"call recording too large for bus ({size} bytes): {p.name}")
        return None
    return base64.b64encode(p.read_bytes()).decode("ascii")
