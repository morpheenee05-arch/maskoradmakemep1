"""
Scripted IVR Call Handler — P1 Mode.

Plays pre-scripted TTS lines via Deepgram TTS API, then listens for
the caller's response ("1" or "2") via strict Goertzel DTMF detection.
STT is used for voicemail/machine detection only.

Flow:
    1. Play intro script (press 1 / press 2)
    2. Listen for machine/voicemail signals during intro (STT)
    3. Open response gates for keypad tones only (DTMF)
    4. Play the matching follow-up script
    5. Return result ("1", "2", or "timeout")
"""
import asyncio
import json
import queue
import time
import re
import random
import urllib.error
import urllib.request
import numpy as np
from pathlib import Path
from typing import Optional
from loguru import logger

try:
    import websockets
except ImportError:
    websockets = None
    logger.error("websockets not installed")

try:
    import aiohttp
except ImportError:
    aiohttp = None
    logger.warning("aiohttp not installed — TTS will fall back to urllib")

import config
from audio.audio_io import AudioManager
from audio.dtmf import DTMFDetector
from console_ui import dashboard


# ── Script Profiles (imported from lightweight module) ───────────────
from audio.script_profiles import resolve_profile

# ── TTS Disk Cache ───────────────────────────────────────────────────

CACHE_DIR = Path(__file__).parent.parent / "storage" / "tts_cache"

# ── Machine Detection Keywords ──────────────────────────────────────────

VM_KEYWORDS = {
    "voicemail", "message", "mailbox", "tone", "record", "available", 
    "leave", "reaching", "reach", "unavailable", "office", "hours",
    "bip", "beep", "answering", "machine", "currently", "unable",
    "not in", "subscriber", "cellular", "not at", "moment", "please call",
    "automated voice", "voice messaging system", "telephone number", "forwarded",
    "extension", "directory", "departments"
}

# Extra VM phrase heuristics and spoken-digit normalization.
VM_STRONG_PHRASES = {
    "leave your message",
    "leave a message",
    "leave a detailed message",
    "send an sms",
    "send me a text",
    "verizon wireless",
    "voice mail",
    "you've reached",
    "you have reached",
    "your call has been",
    "has been forwarded",
    "your call has been forwarded",
    "inconvenience",
    "sorry but",
    "if you record",
    "if you're satisfied with the",
    "hi if you record your name",
    "record your name",
    "record your message",
    "please record your message",
    "recording press",
    "enter message",
    "add to your message",
    "to erase where you left off",
    "are you still there",
    "message press two",
    "please stay on the line",
    "continue recording",
    "you have finished",
    "when you have finished recording",
    "you may hang up",
    "i'm sorry i missed your call",
    "im sorry i missed your call",
    "i'll get back to you",
    "ill get back to you",
    "i can't get to the phone",
    "i cant get to the phone",
    "i can't come to the phone",
    "i cant come to the phone",
    "will try to connect you",
    "google voice",
    "virtual assistant",
    "virtual assitant",
    "thank you for calling",
    "thanks for calling",
    "automated attendant",
    "business hours",
    "hang up and dial",
    "mailbox is full",
    "not enough space",
    "if you know your party",
    "the number you dialed",
    "the number you're trying to reach",
    "the number you are trying to reach",
    "not in service",
    "reason for calling",
    "after the tone",
    "after the beep",
    "at the tone",
    "voice mailbox",
    "voice messaging system",
    "trying to reach",
    "is not available",
    "cannot take your call",
    "not available",
    "please leave",
    "please state your name",
}

# Weaker indicators that should not trigger immediate voicemail by themselves.
VM_SCORE_PHRASES = {
    "your call",
    "as soon as possible",
    "hotline",
    "social security number",
    "press 1",
    "press one",
    "press 2",
    "press two",
}

NUMBER_TOKEN_MAP = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "won": "1",
    "wun": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
}

VM_SCORE_THRESHOLD = 3
VM_DIGIT_TRIGGER_MIN_DIGITS = 3
VM_DIGIT_TRIGGER_RATIO = 0.6
STT_QUEUE_MAX = 180
# TTS comes back from the API at full scale (peak = 1.0); resampling and
# device mixing then clip inter-sample overshoots. Keep headroom instead.
TTS_HEADROOM_GAIN = 0.85


class ScriptedCallHandler:
    """Handles scripted IVR calls: STT voicemail detection + DTMF-only responses."""

    TTS_URL = "https://api.deepgram.com/v1/speak"
    STT_URL = "wss://api.deepgram.com/v1/listen"
    RATE = 24000  # 24kHz, matches audio engine
    INTRO_GATE_LEAD_SEC = 4.5
    RETRY_GATE_LEAD_SEC = 4.0
    _RESAMPLED_CACHE: dict[tuple, dict[str, bytes]] = {}

    def __init__(
        self,
        profile: str,
        capture_device: str,
        playback_device: str,
        worker_id: int = -1,
        scripts: dict | None = None,
        audio_manager=None,
    ):
        """Prepare IVR manager for a specific profile (voicemail mode) on a specific worker."""
        self.profile = profile
        self.worker_id = worker_id

        # Built-in, operator-made (disk), or an explicit bundle from the controller.
        self._scripts = scripts or resolve_profile(profile)
        self.audio = audio_manager or AudioManager(capture_device=capture_device, playback_device=playback_device)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._result: Optional[str] = None
        self._result_event = asyncio.Event()
        self._vm_detection_active = False        # STT voicemail detection (active until outcome)
        self._response_detection_active = False  # Strict DTMF gate (no STT response mapping)
        self._stt_ws = None
        self._stt_recv_task = None
        self._stt_send_task = None
        self._stt_reconnect_task = None
        self._keep_alive_task = None
        self._listening = False
        self._stt_connected = False
        self._stt_connect_failures = 0
        self._stt_send_drop_count = 0
        self._stt_last_reconnect_log_attempt = -1
        self._tts_cache: dict[str, bytes] = {}      # Raw PCM from disk (INT16 24kHz)
        self._ready_audio: dict[str, bytes] = {}     # Pre-resampled float32 at output rate
        self._prepared = False

        # STT audio queue (capture thread -> async sender task)
        self._stt_q = queue.Queue(maxsize=STT_QUEUE_MAX)
        self._stt_send_count = 0
        self._call_active = False  # heartbeats only while a call is live

        # VM detection state (reset each call run)
        self._vm_score = 0
        self._vm_digit_window: list[str] = []
        self._seen_vm_finals: set[str] = set()

        # DTMF Detector (production-grade sliding window)
        self._dtmf = DTMFDetector(sample_rate=self.RATE, window_ms=80, step_ms=40, persistence=2, cooldown_ms=300)

        self.retain_recording = False
        self._pending_recordings: Optional[dict] = None

    async def initialize(self) -> bool:
        """Initialize audio devices."""
        return await self.audio.initialize()

    async def prepare(self):
        """
        Load TTS from disk cache (or generate + save on first run).
        Pre-resample to output device rate for zero-latency playback.
        Pre-start audio streams and STT.
        """
        self._loop = asyncio.get_running_loop()
        if self._prepared:
            if not self._listening:
                logger.debug(f"[{self.profile}] Re-arming warm STT session")
                await self._start_stt()
            return
        try:
            import websockets
            logger.debug(f"ℹ️ websockets version: {websockets.__version__}")
        except Exception:
            pass

        # Load or generate TTS audio (INT16 24kHz PCM)
        await self._load_or_generate_tts()
        if not self._tts_cache.get("intro"):
            raise RuntimeError(
                "TTS intro missing — cannot start a scripted call "
                "(install aiohttp or restore storage/tts_cache)"
            )

        # Pre-start audio streams (need this before pre-resampling)
        self.audio.prepare_streams()

        # Pre-resample all clips to output device rate as float32
        out_rate = self.audio.get_output_rate()
        cache_key = self._resample_cache_key(out_rate)
        cached_ready = self._RESAMPLED_CACHE.get(cache_key)
        if cached_ready is not None and (
            not cached_ready.get("intro") or not cached_ready.get("pressed_1")
        ):
            self._RESAMPLED_CACHE.pop(cache_key, None)
            cached_ready = None
        if cached_ready is None:
            try:
                import config as _cfg
                _soxr_q = getattr(_cfg, "AUDIO_RESAMPLE_QUALITY", "LQ") or "LQ"
            except Exception:
                _soxr_q = "LQ"
            logger.debug(f"Pre-resampling TTS to {out_rate}Hz float32 (soxr {_soxr_q})...")

            try:
                import soxr
                _has_soxr = True
            except ImportError:
                _has_soxr = False
                logger.warning("soxr not installed - falling back to linear interpolation")

            built: dict[str, bytes] = {}
            for key, pcm_data in self._tts_cache.items():
                # INT16 -> float32
                audio = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
                # Resample 24kHz -> output rate (LQ/MQ enough for phone audio)
                if self.RATE != out_rate:
                    if _has_soxr:
                        audio = soxr.resample(
                            audio, self.RATE, out_rate, quality=_soxr_q
                        ).astype(np.float32)
                    else:
                        ratio = out_rate / self.RATE
                        new_len = int(len(audio) * ratio)
                        audio = np.interp(
                            np.linspace(0, len(audio) - 1, new_len),
                            np.arange(len(audio)),
                            audio
                        ).astype(np.float32)
                # Apply headroom after resampling so nothing downstream clips
                audio *= TTS_HEADROOM_GAIN
                built[key] = audio.tobytes()
            if not built.get("intro"):
                raise RuntimeError("TTS intro missing after resample")
            if built.get("pressed_1"):
                self._RESAMPLED_CACHE[cache_key] = built
            else:
                logger.error(
                    "TTS pressed_1 missing — follow-up after key 1 will be silent"
                )
            cached_ready = built
        else:
            logger.debug(f"Using warm resampled cache for profile={self.profile} rate={out_rate}")

        self._ready_audio = dict(cached_ready)
        for key, audio_bytes in self._ready_audio.items():
            self.audio.register_sound(key, audio_bytes)
            duration = (len(audio_bytes) // 4) / out_rate
            logger.debug(f"Registered '{key}': {duration:.1f}s @ {out_rate}Hz")

        # Prime playback path once to reduce first-audio lag.
        try:
            self.audio.play_raw(np.zeros(480, dtype=np.float32).tobytes())
            self.audio.flush_output()
        except Exception:
            pass
        logger.debug("Pre-connecting STT (priming for answer)...")
        await self._start_stt()

        if not self._stt_connected:
            logger.warning("STT not connected yet; reconnect loop will continue in background")

        self._prepared = True
        logger.debug(f"✅ [{self.profile}] All audio pre-resampled & STT primed — zero-latency ready")

    def _resample_cache_key(self, out_rate: int) -> tuple:
        """Include spoken text so a custom script never reuses another profile's PCM."""
        parts = []
        for slot in ("intro", "pressed_1", "pressed_2", "no_response", "retry"):
            val = (self._scripts or {}).get(slot)
            text = val[1] if isinstance(val, (list, tuple)) and len(val) >= 2 else ""
            parts.append(f"{slot}={(text or '')[:80]}")
        return (self.profile, int(out_rate or 0), hash(tuple(parts)))

    async def _load_or_generate_tts(self):
        """Load TTS from disk cache, or generate + save if missing."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Load scripts for the active profile
        script_keys = ["intro", "pressed_1", "pressed_2", "no_response", "retry"]
        for key in script_keys:
            filename, script_text = self._scripts[key]
            cache_path = CACHE_DIR / filename

            if cache_path.exists() and cache_path.stat().st_size > 0:
                # Load from disk (instant)
                self._tts_cache[key] = cache_path.read_bytes()
                duration = len(self._tts_cache[key]) / (self.RATE * 2)
                logger.debug(f"📂 Loaded cached TTS: {key} ({duration:.1f}s)")
            else:
                # Generate via API and save to disk
                logger.info(f"🔊 Generating TTS for '{key}' (first time)...")
                audio = await self._generate_tts(script_text)
                if audio:
                    cache_path.write_bytes(audio)
                    self._tts_cache[key] = audio
                    logger.debug(f"💾 Saved TTS cache: {cache_path}")
                else:
                    logger.error(f"Failed to generate TTS for {key}")

    async def _generate_tts(self, text: str) -> bytes:
        """Generate TTS audio via Deepgram REST API.

        Rotates through the shared key pool: a key rejected with an
        auth/quota error goes on cooldown and the next key is tried, so
        one exhausted key never silences the whole agent.
        """
        from infra.dg_keys import get_pool, classify_http_status

        url = (
            f"{self.TTS_URL}"
            f"?model={config.DG_SPEAK_MODEL}"
            f"&encoding=linear16"
            f"&sample_rate={self.RATE}"
        )
        pool = get_pool()
        keys = pool.keys() or ([config.DEEPGRAM_API_KEY] if config.DEEPGRAM_API_KEY else [])
        if not keys:
            logger.error("TTS: no Deepgram API keys configured")
            return b""
        for _ in range(len(keys)):
            key = pool.current()
            if not key:
                break
            status, audio = await self._tts_request(url, key, text)
            if status == 200 and audio:
                pool.mark_good(key)
                duration = len(audio) / (self.RATE * 2)
                logger.debug(f"🔊 TTS: {len(audio)} bytes ({duration:.1f}s)")
                return audio
            reason = classify_http_status(status)
            if reason:
                pool.mark_bad(key, reason=reason)
                continue  # try the next key
            return b""  # non-auth failure — retrying with another key won't help
        logger.error("TTS: all Deepgram keys failed")
        return b""

    async def _tts_request(self, url: str, api_key: str, text: str) -> tuple:
        """One TTS POST. Returns (http_status, audio_bytes)."""
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "text/plain",
        }
        try:
            if aiohttp is not None:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, data=text) as resp:
                        if resp.status != 200:
                            error = await resp.text()
                            logger.error(f"TTS API error {resp.status}: {error[:200]}")
                            return resp.status, b""
                        return 200, await resp.read()
            return await asyncio.to_thread(
                self._generate_tts_urllib, url, headers, text
            )
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            return 0, b""

    @staticmethod
    def _generate_tts_urllib(url: str, headers: dict, text: str) -> tuple:
        """Stdlib fallback when aiohttp is not installed on the agent."""
        req = urllib.request.Request(
            url,
            data=text.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if getattr(resp, "status", 200) != 200:
                    logger.error(f"TTS API error {resp.status}: {resp.read()[:300]!r}")
                    return getattr(resp, "status", 0), b""
                return 200, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300] if e.fp else b""
            logger.error(f"TTS API error {e.code}: {body!r}")
            return e.code, b""
        except Exception as e:
            logger.error(f"TTS urllib failed: {e}")
            return 0, b""

    # ── Main Flow ───────────────────────────────────────────────────

    def pop_call_recordings(self) -> Optional[dict]:
        """Paths from the last run() when retain_recording was set (P1)."""
        paths = self._pending_recordings
        self._pending_recordings = None
        return paths

    def _begin_call_recording(self) -> None:
        try:
            import config as _cfg
            if not getattr(_cfg, "GV_CALL_RECORD", True):
                return
        except Exception:
            return
        from audio.call_recorder import CallRecorder
        tag = f"w{self.worker_id}_{int(time.time())}"
        rec = CallRecorder(tag=tag)
        out_rate = self.audio.get_output_rate()
        if out_rate:
            rec.set_output_rate(out_rate)
        self.audio.set_recorder(rec)

    def _end_call_recording(self) -> None:
        paths = self.audio.clear_recorder(retain=self.retain_recording)
        if self.retain_recording and paths:
            from audio.call_recorder import CallRecorder
            if self._pending_recordings:
                CallRecorder.delete_paths(self._pending_recordings)
            self._pending_recordings = paths
        elif paths and not self.retain_recording:
            pass  # clear_recorder already deleted ephemeral files

    async def run(self) -> str:
        """
        Run the full scripted IVR flow.
        Returns "1", "2", "voicemail", or "timeout".
        """
        self._loop = asyncio.get_running_loop()
        self._result = None
        self._stop_done = False
        self._result_event.clear()
        self._vm_detection_active = False
        self._response_detection_active = False
        self._vm_score = 0
        self._vm_digit_window.clear()
        self._seen_vm_finals.clear()
        self._dtmf.reset()
        # The 8000-packet STT cap is per-call zombie protection; a warm
        # handler would otherwise carry it over and force-timeout every
        # later call the moment it connects.
        self._stt_send_count = 0
        self._call_active = True

        if not self._prepared:
            await self.prepare()
        elif not self._listening:
            # Re-arm STT loops if this handler was previously stopped.
            await self._start_stt()

        # Give reconnect loop a short window before prompt playback.
        if not self._stt_connected:
            for _ in range(15):
                if self._stt_connected:
                    break
                await asyncio.sleep(0.1)

        intro_audio = self._ready_audio.get("intro", b"")
        if not intro_audio:
            logger.error("No intro audio in ready cache")
            return "error"

        # NOTE: Keep-alive heartbeat stays ACTIVE for the entire call.
        # Real capture audio supplements it, but the heartbeat ensures
        # Deepgram never starves during silence gaps or bot speech.

        # Wire capture → STT + DTMF detection
        self.audio.set_audio_callback(self._on_captured_audio)
        self._begin_call_recording()
        self.audio.start()
        try:
            return await self._run_ivr_body()
        finally:
            self._call_active = False
            self._end_call_recording()

    async def _run_ivr_body(self) -> str:
        try:
            import config as _cfg
        except Exception:
            _cfg = None
        gate1_sec = float(getattr(_cfg, "P1_GATE1_SEC", 13) or 13)
        gate2_sec = float(getattr(_cfg, "P1_GATE2_SEC", 10) or 10)
        # Listen for 1/2 for the whole intro — people press as soon as they
        # hear the prompt, not only in the last 4.5s. The old lead-in gate
        # dropped that press, so pressed_1 never played.
        logger.info("▶️ Playing intro (from cache)...")
        self._vm_detection_active = True
        self._dtmf.reset()
        self._response_detection_active = True
        await self._play_script("intro", interruptible=True)
        self._response_detection_active = False
        if self._result == "voicemail":
            logger.info("📵 Voicemail detected — returning early for immediate hangup")
            return "voicemail"

        # ── Response Gate #1 ──
        if self._result is None:
            self._result_event.clear()
            self._dtmf.reset()
            self._response_detection_active = True
            logger.info(f"👂 Response gate OPEN ({gate1_sec:.0f}s timeout) — STRICT DTMF DETECTION ACTIVE")
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=gate1_sec)
            except asyncio.TimeoutError:
                logger.info("⏰ No response at gate #1 — will retry")
            self._response_detection_active = False

        # ── Retry phase (if no response at gate #1) ──
        if self._result is None:
            if "retry" in self._ready_audio:
                logger.info("🔁 Playing retry prompt...")
                self._dtmf.reset()
                self._response_detection_active = True
                await self._play_script("retry", interruptible=True)
                self._response_detection_active = False

                if self._result is None:
                    self._result_event.clear()
                    self._dtmf.reset()
                    self._response_detection_active = True
                    logger.info(f"👂 Response gate #2 OPEN ({gate2_sec:.0f}s timeout) — STRICT DTMF DETECTION ACTIVE")
                    try:
                        await asyncio.wait_for(self._result_event.wait(), timeout=gate2_sec)
                    except asyncio.TimeoutError:
                        self._result = "timeout"
                    self._response_detection_active = False
            else:
                self._result = "timeout"

        # ── Play follow-up based on result ──
        if self._result == "hangup":
            return "hangup"
        if self._result == "voicemail":
            # Voicemail confirmed during the gates — do not play the
            # no_response message into a mailbox (saves ~12s per call).
            logger.info("📵 Voicemail confirmed — skipping follow-up, hanging up")
            return "voicemail"

        # Outcome is now decided; disable VM detection for follow-up playback.
        self._vm_detection_active = False
        self._response_detection_active = False

        key = "no_response"
        if self._result == "1":
            key = "pressed_1"
        elif self._result == "2":
            key = "pressed_2"

        logger.info(f"▶️ Playing follow-up: {key}")
        try:
            self.audio.flush_output()
        except Exception:
            pass
        await asyncio.sleep(0.15)
        await self._play_script(key, interruptible=False)

        # GRACE PERIOD: short drain wait for the hardware buffer/subprocess
        await asyncio.sleep(0.8)
        return self._result or "timeout"

    async def _play_script(self, key: str, early_listen_start: Optional[float] = None, interruptible: bool = True):
        """Play a registered clip and wait until it finishes or is interrupted."""
        audio_bytes = self._ready_audio.get(key)
        if not audio_bytes:
            logger.error(f"No audio in ready cache for '{key}' — nothing to play")
            return

        self.audio.play_sound_by_key(key)

        out_rate = self.audio.get_output_rate()
        duration = (len(audio_bytes) // 4) / out_rate
        start_time = asyncio.get_running_loop().time()
        wait_duration = duration if early_listen_start is None else min(duration, max(0.0, early_listen_start))

        try:
            while (asyncio.get_running_loop().time() - start_time) < wait_duration:
                if self._result is not None and interruptible:
                    break
                await asyncio.sleep(0.05)

            if self._result is not None and interruptible:
                logger.info(f"⏹️ Soundboard interrupt: {key}")
                self.audio.flush_output()
        except Exception as e:
            logger.error(f"Playback error: {e}")
            self.audio.flush_output()

    # ── Capture Handler (STT + DTMF) ────────────────────────────────

    def _on_captured_audio(self, audio_bytes: bytes):
        """Called from worker thread with captured audio (INT16 24kHz PCM)."""
        # Vectorized INT16 → float32 (replaces slow struct.unpack loop)
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        # STT forwarding (for voicemail detection only)
        if self._listening:
            self._queue_stt_audio(audio_bytes)

        # DTMF Detection — feed samples to the sliding-window detector
        if self._response_detection_active and self._result is None:
            key = self._dtmf.feed(samples)
            if key and key in ("1", "2"):
                self.audio.flush_output()
                self._set_result(key, f"🎯 DTMF detected: key '{key}' pressed!")

    # ── STT (Speech-to-Text) ────────────────────────────────────────

    def _queue_stt_audio(self, audio_bytes: bytes):
        """Push audio into the STT queue, dropping oldest on overflow."""
        if not self._listening:
            return
        try:
            self._stt_q.put_nowait(audio_bytes)
            return
        except queue.Full:
            self._stt_send_drop_count += 1
        try:
            _ = self._stt_q.get_nowait()
            self._stt_q.put_nowait(audio_bytes)
        except Exception:
            pass

    def _set_result(self, result: str, log_line: str = ""):
        """Thread-safe result setter."""
        if self._result is not None:
            return
        self._result = result
        if log_line:
            logger.info(log_line)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._result_event.set)
        else:
            self._result_event.set()

    @staticmethod
    def _tokenize_transcript(transcript: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", transcript.lower())

    def _extract_digit_tokens(self, transcript: str) -> list[str]:
        """Normalize spoken/numeric transcript tokens into a digit stream."""
        digits: list[str] = []
        for token in self._tokenize_transcript(transcript):
            mapped = NUMBER_TOKEN_MAP.get(token)
            if mapped is not None:
                digits.append(mapped)
                continue
            if token.isdigit():
                if len(token) == 1:
                    digits.append(token)
                elif len(token) <= 8:
                    digits.extend(list(token))
        return digits

    def _detect_voicemail(self, transcript: str) -> Optional[str]:
        """Return a reason string when voicemail/machine is detected."""
        tokens = self._tokenize_transcript(transcript)
        digits = self._extract_digit_tokens(transcript)
        if digits:
            self._vm_digit_window.extend(digits)
            if len(self._vm_digit_window) > 12:
                self._vm_digit_window = self._vm_digit_window[-12:]

        digit_ratio = len(digits) / max(1, len(tokens))
        if len(digits) >= VM_DIGIT_TRIGGER_MIN_DIGITS and digit_ratio >= VM_DIGIT_TRIGGER_RATIO:
            return f"spoken-digit-sequence:{''.join(digits[:6])}"

        strong_hits = [phrase for phrase in VM_STRONG_PHRASES if phrase in transcript]
        weak_phrase_hits = [phrase for phrase in VM_SCORE_PHRASES if phrase in transcript]
        keyword_hits = [kw for kw in VM_KEYWORDS if kw in transcript]
        has_tone_hint = "beep" in transcript or "tone" in transcript

        if strong_hits:
            return f"strong-phrase:{strong_hits[0]}"

        if weak_phrase_hits:
            self._vm_score += 1

        if len(keyword_hits) >= 2:
            self._vm_score += 2
        elif len(keyword_hits) == 1:
            self._vm_score += 1

        if has_tone_hint and not ({"beep", "tone"} & set(keyword_hits)):
            self._vm_score += 1

        if self._vm_score >= VM_SCORE_THRESHOLD:
            reason = keyword_hits[0] if keyword_hits else "scored-signals"
            return f"keyword-score:{reason}"
        return None

    async def _start_stt(self):
        """Start STT send/recv/reconnect loops."""
        if self._listening:
            return
        self._listening = True

        if not self._stt_send_task or self._stt_send_task.done():
            self._stt_send_task = asyncio.create_task(self._stt_send_loop())
        if not self._keep_alive_task or self._keep_alive_task.done():
            self._keep_alive_task = asyncio.create_task(self._stt_keep_alive_loop())
        if not self._stt_reconnect_task or self._stt_reconnect_task.done():
            self._stt_reconnect_task = asyncio.create_task(self._stt_reconnect_loop())

        await self._connect_stt_once()

    def _stt_ws_url(self) -> str:
        params = (
            f"?model={config.DG_LISTEN_MODEL}"
            f"&encoding=linear16"
            f"&sample_rate={self.RATE}"
            f"&channels=1"
            f"&interim_results=true"
            f"&no_delay=true"
            f"&endpointing=150"
            f"&utterance_end_ms=1000"
        )
        return self.STT_URL + params

    async def _close_stt_socket(self, ws):
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass

    async def _mark_stt_disconnected(self, reason: str, ws=None):
        """Mark current STT session as down; reconnect loop will recover."""
        if ws is None:
            ws = self._stt_ws
        if ws is self._stt_ws:
            self._stt_ws = None
        if self._stt_connected:
            logger.warning(f"STT disconnected: {reason}")
        self._stt_connected = False
        await self._close_stt_socket(ws)

    async def _connect_stt_once(self) -> bool:
        """Attempt one STT websocket connection.

        The key comes from the shared pool; an auth/quota rejection cools
        that key down so the reconnect loop's next attempt uses another one.
        """
        if not self._listening:
            return False
        if "websockets" not in globals():
            logger.error("websockets package unavailable; cannot connect STT")
            return False

        from infra.dg_keys import get_pool, looks_like_auth_error

        pool = get_pool()
        api_key = pool.current() or config.DEEPGRAM_API_KEY
        headers = {"Authorization": f"Token {api_key}"}
        try:
            ws = await websockets.connect(
                self._stt_ws_url(),
                additional_headers=headers,
                # Deepgram can intermittently delay ping/pong under load.
                # We keep the socket alive via active-silence packets instead.
                ping_interval=None,
                ping_timeout=None,
                close_timeout=5,
                max_size=2**20,
                open_timeout=12,
            )
        except Exception as e:
            reason = looks_like_auth_error(e)
            if reason:
                pool.mark_bad(api_key, reason=reason)
            self._stt_connect_failures += 1
            if self._stt_connect_failures != self._stt_last_reconnect_log_attempt:
                logger.warning(
                    f"STT connect attempt {self._stt_connect_failures} failed: "
                    f"{type(e).__name__}: {e}"
                )
                self._stt_last_reconnect_log_attempt = self._stt_connect_failures
            return False

        pool.mark_good(api_key)

        self._stt_ws = ws
        self._stt_connected = True
        self._stt_connect_failures = 0
        self._stt_last_reconnect_log_attempt = -1

        if self._stt_recv_task and not self._stt_recv_task.done():
            self._stt_recv_task.cancel()
            try:
                await self._stt_recv_task
            except (asyncio.CancelledError, Exception):
                pass
        self._stt_recv_task = asyncio.create_task(self._stt_recv_loop(ws))
        logger.debug("STT websocket connected")
        return True

    async def _stt_keep_alive_loop(self):
        """
        Send "active silence" heartbeats to Deepgram continuously.
        Uses true digital silence (all zeroes).
        """
        call_silence = np.zeros(2400, dtype=np.int16).tobytes()  # ~100ms at 24kHz
        idle_silence = np.zeros(960, dtype=np.int16).tobytes()   # ~20ms at 24kHz
        try:
            while self._listening:
                if self._call_active:
                    # During a call: top up whenever the capture stream
                    # starves, so Deepgram never sees a 10s gap.
                    if self._stt_q.qsize() < 6:
                        self._queue_stt_audio(call_silence)
                    await asyncio.sleep(0.25)
                else:
                    # Between calls the socket must still be fed: Deepgram
                    # kills an idle socket after ~10s (net1011), and the
                    # reconnect churn lands exactly on the next call's
                    # greeting window — voicemails were being misclassified
                    # as timeouts. 20ms every ~4s is ~0.5% duty cycle
                    # (negligible billing) and keeps the socket warm.
                    if self._stt_connected and self._stt_q.qsize() < 2:
                        self._queue_stt_audio(idle_silence)
                    await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _stt_reconnect_loop(self):
        """Reconnect STT websocket when it drops mid-call."""
        while self._listening:
            if self._stt_connected and self._stt_ws is not None:
                await asyncio.sleep(0.5)
                continue
            backoff = min(6.0, 0.5 * (2 ** min(self._stt_connect_failures, 4))) + random.uniform(0.0, 0.3)
            if self._stt_connect_failures > 0:
                await asyncio.sleep(backoff)
            await self._connect_stt_once()

    @staticmethod
    def _queue_get_with_timeout(q: queue.Queue, timeout: float) -> Optional[bytes]:
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    async def _stt_send_loop(self):
        """Drain STT queue and send over websocket (async, no cross-thread futures)."""
        # Hard cap: zombie capture must not stream forever if stop() was missed.
        max_packets = 8000
        while self._listening:
            if self._stt_send_count >= max_packets:
                logger.warning(
                    f"STT send cap reached ({max_packets}) — forcing handler stop"
                )
                self._listening = False
                try:
                    self.force_result("timeout")
                except Exception:
                    pass
                break
            data = await asyncio.to_thread(self._queue_get_with_timeout, self._stt_q, 0.2)
            if data is None:
                continue
            if not self._listening:
                break
            ws = self._stt_ws
            if not ws or not self._stt_connected:
                self._stt_send_drop_count += 1
                continue
            try:
                await ws.send(data)
                # Count only in-call streaming toward the zombie cap — idle
                # keep-alive heartbeats are intentional and must never trip it.
                if self._call_active:
                    self._stt_send_count += 1
                if self._stt_send_count and self._stt_send_count % 500 == 0:
                    logger.debug(f"STT: {self._stt_send_count} packets sent")
            except Exception as e:
                logger.debug(f"STT send error: {type(e).__name__}: {e}")
                await self._mark_stt_disconnected(f"send failure ({type(e).__name__})", ws)

    async def _stt_recv_loop(self, ws):
        """Receive transcription results from Deepgram STT."""
        try:
            async for msg in ws:
                if not self._listening or ws is not self._stt_ws:
                    break
                if isinstance(msg, str):
                    try:
                        data = json.loads(msg)
                        self._handle_stt_result(data)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            if self._listening:
                logger.warning(f"STT recv issue: {type(e).__name__}: {e}")
        finally:
            if ws is self._stt_ws:
                await self._mark_stt_disconnected("receiver loop ended", ws)
            logger.debug("STT receiver loop terminated")

    def _handle_stt_result(self, data: dict):
        """Handle transcription for telemetry + voicemail detection."""
        if self._result is not None:
            return

        if not self._vm_detection_active and not self._response_detection_active:
            return

        if data.get("type") != "Results":
            return

        channel = data.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        transcript = alternatives[0].get("transcript", "").lower().strip()
        if not transcript:
            return

        is_final = data.get("is_final", False)
        prefix = "[FINAL] " if is_final else ""
        if self.worker_id >= 0:
            dashboard.update_worker(self.worker_id, stt=f"{prefix}{transcript}")

        if is_final:
            logger.info(f"STT heard (final): '{transcript}'")
        else:
            logger.debug(f"STT (interim): '{transcript}'")
            return

        if self._vm_detection_active:
            if transcript in self._seen_vm_finals:
                return
            self._seen_vm_finals.add(transcript)
            vm_reason = self._detect_voicemail(transcript)
            if vm_reason:
                self.audio.flush_output()
                self._set_result("voicemail", f"Detected VOICEMAIL ({vm_reason}): '{transcript}'")
                return

        # Spoken one/two must never trigger call result; DTMF is Goertzel-only.
        if self._response_detection_active:
            tokens = set(self._tokenize_transcript(transcript))
            if tokens & {"1", "2", "one", "two", "to", "too", "won", "wun"}:
                logger.debug(f"Ignoring spoken digits during DTMF-only gate: '{transcript}'")

    def force_result(self, result: str):
        """Force a result externally (e.g., remote hangup detected). Thread-safe."""
        self._set_result(result, f"Forced result externally: '{result}'")

    # ── Cleanup ─────────────────────────────────────────────────────

    async def stop(self):
        """Stop audio streams and STT WebSocket (idempotent, never hang long)."""
        if getattr(self, "_stop_done", False):
            return
        self._stop_done = True

        self._listening = False
        self._stt_connected = False
        self._prepared = False
        self._vm_detection_active = False
        self._response_detection_active = False
        # Unblock any waiter in run() gates.
        try:
            if self._result is None:
                self._result = "hangup"
            self._result_event.set()
        except Exception:
            pass
        try:
            self.audio.flush_output()
        except Exception:
            pass
        try:
            self.audio.stop()
        except Exception:
            pass

        for task_attr in ("_keep_alive_task", "_stt_send_task", "_stt_recv_task", "_stt_reconnect_task"):
            task = getattr(self, task_attr, None)
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.5)
                except asyncio.CancelledError:
                    # Awaiting a task we just cancelled re-raises CancelledError
                    # into US. It must NEVER escape stop(): it would fly out of
                    # the caller's `finally` in p1_runner and REPLACE the call's
                    # real result ('voicemail'/'hangup'/'timeout') with a phantom
                    # 'cancelled'. Stopping is driven by the stop flag anyway —
                    # a genuine external cancel re-arrives at the next await.
                    pass
                except Exception:
                    pass
                setattr(self, task_attr, None)

        if self._stt_ws:
            try:
                await asyncio.wait_for(self._stt_ws.close(), timeout=2.0)
            except Exception:
                pass
            self._stt_ws = None

        while not self._stt_q.empty():
            try:
                self._stt_q.get_nowait()
            except Exception:
                break

        logger.info(
            "Scripted call handler stopped "
            f"(STT packets sent: {self._stt_send_count}, dropped: {self._stt_send_drop_count})"
        )




