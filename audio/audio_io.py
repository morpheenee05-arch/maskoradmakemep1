"""
Audio manager.

Architecture:
  - PLAYBACK: Runs in a subprocess (engine.py playback_process_main).
              Receives audio frames via IPC queue, writes to DAC.
  - CAPTURE:  Runs in a subprocess (engine.py capture_process_main).
              Captures caller audio, resamples to 24kHz, sends to main via capture_q.
"""
import multiprocessing
import threading
import queue
import numpy as np
from typing import Optional, Callable, TYPE_CHECKING
from loguru import logger

if TYPE_CHECKING:
    from audio.call_recorder import CallRecorder

try:
    import soxr
    _HAS_SOXR = True
except ImportError:
    _HAS_SOXR = False


def _resample_quality() -> str:
    try:
        import config
        return getattr(config, "AUDIO_RESAMPLE_QUALITY", "LQ") or "LQ"
    except Exception:
        import os
        return os.getenv("AUDIO_RESAMPLE_QUALITY", "LQ").strip() or "LQ"


class AudioManager:
    """Manages audio output (subprocess) and capture (subprocess)."""

    def __init__(self, capture_device: str = "Line 1", playback_device: str = "Line 2"):
        """Initialize the single node audio component with dynamic devices for parallel isolation."""
        self.capture_device = capture_device
        self.playback_device = playback_device
        
        # Subprocesses
        self._cap_process = None
        self._out_process = None
        self._capture_q = None
        self._playback_q = None
        self._stop_event = None

        # Callbacks & Worker Threads
        self._send_callback = None
        self._forwarder = None
        self._forwarding = False
        self._initialized = False

        # Device Scan Cache
        self._out_device = None
        self._output_rate = None
        self._out_ch = 1

        # Playback ID (Sync)
        self._playback_id = 0
        self._id_lock = threading.Lock()
        self._sounds = {}
        self._recorder: Optional["CallRecorder"] = None

    def set_recorder(self, recorder: Optional["CallRecorder"]) -> None:
        self._recorder = recorder
        if recorder is not None and self._output_rate:
            recorder.set_output_rate(self._output_rate)

    def clear_recorder(self, retain: bool = False) -> Optional[dict]:
        """Stop tapping; finalize WAVs when retain=True, else delete immediately."""
        rec = self._recorder
        self._recorder = None
        if rec is None:
            return None
        paths = rec.finalize()
        if not retain and paths:
            from audio.call_recorder import CallRecorder
            CallRecorder.delete_paths(paths)
            return None
        return paths

    async def initialize(self) -> bool:
        if self._initialized:
            return True
        try:
            import sounddevice as sd
            from audio.engine import find_wasapi
            cap = find_wasapi(self.capture_device, "input")
            out = find_wasapi(self.playback_device, "output")
            if cap is None or out is None:
                
                logger.error(f"❌ Required audio devices not found {cap} {out}")
                return False
            logger.debug(f"📥 Capture: [{cap}] {sd.query_devices(cap)['name']}")
            out_info = sd.query_devices(out)
            self._output_rate = int(out_info['default_samplerate'])
            self._out_ch = min(int(out_info['max_output_channels']), 2) or 1
            self._out_device = out
            logger.debug(f"📤 Output:  [{out}] {out_info['name']} @ {self._output_rate}Hz ch={self._out_ch}")
            if self._recorder is not None:
                self._recorder.set_output_rate(self._output_rate)
            self._initialized = True
            return True
        except Exception as e:
            logger.error(f"Device scan failed: {e}")
            return False

    def get_output_rate(self) -> int:
        return self._output_rate or 24000

    def _any_process_alive(self) -> bool:
        """Check if any audio subprocess is still running."""
        for proc in [self._cap_process, self._out_process]:
            if proc is not None and proc.is_alive():
                return True
        return False

    def prepare_streams(self) -> bool:
        """Start both capture and playback subprocesses."""
        # Only create a fresh stop event if no processes are alive
        if self._stop_event is None or not self._any_process_alive():
            self._stop_event = multiprocessing.Event()

        # ── Start Capture Process ──
        if self._cap_process is None or not self._cap_process.is_alive():
            self._capture_q = multiprocessing.Queue(maxsize=500)
            from audio.engine import capture_process_main
            self._cap_process = multiprocessing.Process(
                target=capture_process_main,
                args=(self._capture_q, self._stop_event, self.capture_device),
                name=f"audio-capture-{self.capture_device.replace(' ', '')}", daemon=True
            )
            self._cap_process.start()
            logger.debug(f"🚀 Capture subprocess spawned (PID {self._cap_process.pid})")

        # ── Start Playback Process ──
        if self._out_process is None or not self._out_process.is_alive():
            self._playback_q = multiprocessing.Queue(maxsize=500)
            from audio.engine import playback_process_main
            self._out_process = multiprocessing.Process(
                target=playback_process_main,
                args=(self._playback_q, self._stop_event, self.playback_device),
                name=f"audio-playback-{self.playback_device.replace(' ', '')}", daemon=True
            )
            self._out_process.start()
            logger.debug(f"🚀 Playback subprocess spawned (PID {self._out_process.pid})")

        return True

    def set_audio_callback(self, callback: Callable[[bytes], None]):
        self._send_callback = callback

    def start(self):
        if self._cap_process is None or not self._cap_process.is_alive():
            self.prepare_streams()
        if self._forwarding:
            return
        self._forwarding = True
        self._forwarder = threading.Thread(
            target=self._forward_loop, daemon=True, name="cap-fwd"
        )
        self._forwarder.start()
        logger.debug("📡 Capture forwarding started")

    def _forward_loop(self):
        consecutive_errors = 0
        while self._forwarding:
            try:
                data = self._capture_q.get(timeout=0.05)
                if self._recorder is not None:
                    self._recorder.append_remote(data)
                if self._send_callback:
                    self._send_callback(data)
                consecutive_errors = 0  # Reset on success
            except (EOFError, BrokenPipeError, OSError) as e:
                consecutive_errors += 1
                if consecutive_errors == 1:
                    logger.warning(f"Capture queue error (subprocess may be dead): {e}")
                if consecutive_errors >= 20:
                    logger.error("Capture subprocess appears dead — stopping forward loop")
                    break
            except Exception:
                pass

    # ── Playback (main process, persistent worker) ──

    def register_sound(self, key: str, audio_bytes: bytes):
        """Register a pre-resampled soundboard clip."""
        self._sounds[key] = audio_bytes
        logger.debug(f"🎹 Registered soundboard clip: {key}")

    def play_sound_by_key(self, key: str):
        """Play a registered soundboard clip atomically."""
        audio = self._sounds.get(key)
        if not audio:
            logger.error(f"Sound clip not found: {key}")
            return
        self.play_raw(audio)

    def _drain_playback_q(self):
        """Drop queued PLAY/FLUSH the subprocess has not consumed yet."""
        q = self._playback_q
        if q is None:
            return
        try:
            while True:
                q.get_nowait()
        except Exception:
            pass

    def play_raw(self, raw_bytes: bytes):
        """Send audio to the playback subprocess."""
        if self._playback_q is None:
            logger.error("Playback subprocess not active")
            return

        with self._id_lock:
            self._playback_id += 1
            current_id = self._playback_id

        audio = np.frombuffer(raw_bytes, dtype=np.float32)
        audio = np.clip(audio, -1.0, 1.0)

        CHUNK = 4096  # Larger IPC chunks for efficiency
        ch = self._out_ch
        dropped = 0
        for i in range(0, len(audio), CHUNK):
            # Atomic ID check
            with self._id_lock:
                if current_id != self._playback_id:
                    break
                
            chunk = audio[i:i + CHUNK]
            if ch > 1:
                frames = np.column_stack([chunk] * ch)
            else:
                frames = chunk.reshape(-1, 1)

            if self._recorder is not None:
                self._recorder.append_playback_float32(
                    chunk.tobytes(), self._output_rate or 24000
                )

            sent = False
            for _ in range(40):
                try:
                    self._playback_q.put(("PLAY", current_id, frames), timeout=0.25)
                    sent = True
                    break
                except Exception:
                    with self._id_lock:
                        if current_id != self._playback_id:
                            break
            if not sent:
                dropped += 1
                break
        if dropped:
            logger.warning("Playback queue stalled — follow-up audio was truncated")

    def flush_output(self):
        """Stop current playback and discard queued frames so the next clip can start."""
        if self._playback_q is None:
            return
        with self._id_lock:
            self._playback_id += 1
            current_id = self._playback_id
        self._drain_playback_q()
        try:
            self._playback_q.put(("FLUSH", current_id, None), timeout=0.2)
        except Exception:
            pass
        logger.debug(f"🌊 Bridge: Output flush sent (ID: {current_id})")

    def play_tts(self, audio_bytes: bytes):
        """Play TTS audio (INT16 24kHz) — resamples and plays directly."""
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if self._output_rate and self._output_rate != 24000:
            if _HAS_SOXR:
                audio = soxr.resample(
                    audio, 24000, self._output_rate, quality=_resample_quality()
                ).astype(np.float32)
            else:
                ratio = self._output_rate / 24000
                new_len = int(len(audio) * ratio)
                audio = np.interp(
                    np.linspace(0, len(audio) - 1, new_len),
                    np.arange(len(audio)),
                    audio
                ).astype(np.float32)
        audio = np.clip(audio, -1.0, 1.0)
        self.play_raw(audio.tobytes())

    def stop(self):
        """Shutdown capture and playback engines."""
        self._forwarding = False
        if self._stop_event:
            self._stop_event.set()
            
        if self._forwarder:
            self._forwarder.join(timeout=2)
            self._forwarder = None

        # Clean up subprocesses
        for proc in [self._cap_process, self._out_process]:
            if proc and proc.is_alive():
                proc.join(timeout=2)
                if proc.is_alive():
                    proc.terminate()
        
        self._cap_process = None
        self._out_process = None
        self._stop_event = None  # Fresh event will be created on next prepare_streams()

        # Drain queues
        for q in [self._capture_q, self._playback_q]:
            if q:
                try:
                    while not q.empty():
                        q.get_nowait()
                except Exception:
                    pass
        
        logger.debug("🛑 Audio subprocesses stopped")


# Removed singleton get_audio_manager to ensure strict isolation per-worker.
