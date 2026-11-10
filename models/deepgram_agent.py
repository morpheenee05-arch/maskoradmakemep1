"""
Deepgram Voice Agent — async WebSocket with dedicated send loop.

Architecture:
    - Async receive loop (asyncio task) for incoming messages/audio
    - Dedicated send loop that drains a thread-safe queue
    - No run_coroutine_threadsafe overhead per audio chunk
"""
import asyncio
import concurrent.futures
import json
import queue
import threading
from typing import Optional, Callable
from loguru import logger

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logger.error("websockets not installed: pip install websockets")

import config


class VoiceAgent:
    """
    Deepgram Voice Agent with optimized audio transport.
    
    Audio: 24kHz 16-bit PCM linear16 in/out.
    
    Callbacks:
        on_transcript(role, text)
        on_audio(bytes)
        on_speaking_start()
        on_speaking_stop()
    """
    
    RATE = 24000
    WS_URL = "wss://agent.deepgram.com/v1/agent/converse"
    
    def __init__(self):
        self._ws = None
        self._active = False
        self._settings_ok = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Async tasks
        self._recv_task: Optional[asyncio.Task] = None
        self._send_task: Optional[asyncio.Task] = None
        
        # Thread-safe send queue (audio bytes from capture thread)
        self._send_q: queue.Queue = queue.Queue(maxsize=500)
        
        # Dedicated single-thread executor for drain loop (avoids saturating default pool)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dg-drain")
        
        # Callbacks
        self.on_transcript: Optional[Callable] = None
        self.on_audio: Optional[Callable] = None
        self.on_speaking_start: Optional[Callable] = None
        self.on_speaking_stop: Optional[Callable] = None
        
        self.system_prompt = ""
        self.greeting = ""
    
    def configure(self, prompt: str, greeting: str = ""):
        self.system_prompt = prompt
        self.greeting = greeting
    
    async def connect(self) -> bool:
        if not WEBSOCKETS_AVAILABLE:
            return False

        from infra.dg_keys import get_pool, looks_like_auth_error

        pool = get_pool()
        api_key = pool.current() or config.DEEPGRAM_API_KEY
        if not api_key:
            logger.error("DEEPGRAM_API_KEY not set")
            return False

        try:
            self._loop = asyncio.get_running_loop()
            headers = {"Authorization": f"Token {api_key}"}
            
            logger.info("🔌 Connecting to Deepgram...")
            self._ws = await websockets.connect(
                self.WS_URL,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**20,  # 1MB max message
            )
            self._active = True
            self._settings_ok = False
            pool.mark_good(api_key)
            logger.info("✅ Deepgram connected")
            
            # Start receive loop
            self._recv_task = asyncio.create_task(self._recv_loop())
            
            # Start send loop
            self._send_task = asyncio.create_task(self._send_loop())
            
            # Send settings
            await self._send_settings()
            
            # Wait for confirmation (max 3s)
            for _ in range(30):
                if self._settings_ok:
                    break
                await asyncio.sleep(0.1)
            
            if not self._settings_ok:
                logger.warning("⚠️ Settings not confirmed — forcing _settings_ok=True to prevent silent audio drop")
                self._settings_ok = True
            
            return True
        except Exception as e:
            reason = looks_like_auth_error(e)
            if reason:
                pool.mark_bad(api_key, reason=reason)
            logger.error(f"❌ Connection failed: {e}")
            await self.disconnect()
            return False
    
    # ── Receive Loop ─────────────────────────────────────────────────
    
    async def _recv_loop(self):
        """Async loop receiving messages from Deepgram."""
        try:
            async for msg in self._ws:
                if not self._active:
                    break
                self._handle(msg)
        except websockets.exceptions.ConnectionClosed as e:
            if self._active:
                logger.info(f"WS closed: {e.code}")
        except Exception as e:
            if self._active:
                logger.error(f"WS recv error: {e}")
        finally:
            self._active = False
    
    def _handle(self, msg):
        """Process a single WebSocket message."""
        if isinstance(msg, bytes):
            if self.on_audio:
                self.on_audio(msg)
            return
        
        try:
            data = json.loads(msg)
            t = data.get("type", "")
            
            if t == "SettingsApplied":
                self._settings_ok = True
                logger.info("✅ Settings applied")
            
            elif t == "ConversationText":
                role = data.get("role", "?")
                text = data.get("content", "")
                who = "Caller" if role == "user" else "Agent"
                logger.info(f"💬 {who}: {text}")
                if self.on_transcript:
                    self.on_transcript(role, text)
            
            elif t == "AgentStartedSpeaking":
                if self.on_speaking_start:
                    self.on_speaking_start()
            
            elif t == "AgentAudioDone":
                if self.on_speaking_stop:
                    self.on_speaking_stop()
            
            elif t == "UserStartedSpeaking":
                logger.debug("🎤 Caller speaking")
            
            elif t == "Error":
                logger.error(f"DG error: {data}")
            
            elif t not in ("Welcome", "History", "AgentThinking"):
                logger.debug(f"DG event: {t}")
                
        except json.JSONDecodeError:
            pass
    
    # ── Send Loop (drains thread-safe queue) ─────────────────────────
    
    async def _send_loop(self):
        """
        Dedicated async task that drains the send queue.
        Runs every ~20ms, batching any queued audio into a single send.
        This avoids run_coroutine_threadsafe overhead.
        """
        while self._active:
            try:
                # Drain queue (blocking get in executor to avoid busy-wait)
                data = await self._loop.run_in_executor(
                    self._executor, self._drain_queue
                )
                if data and self._ws:
                    await self._ws.send(data)
            except Exception as e:
                if self._active and "closed" not in str(e).lower():
                    logger.debug(f"Send error: {e}")
                if not self._active:
                    break
    
    def _drain_queue(self) -> Optional[bytes]:
        """Drain all available audio from queue into one buffer. Blocks max 25ms."""
        try:
            first = self._send_q.get(timeout=0.025)
        except queue.Empty:
            return None
        
        buf = bytearray(first)
        
        # Drain any remaining without blocking
        while not self._send_q.empty():
            try:
                buf.extend(self._send_q.get_nowait())
            except queue.Empty:
                break
        
        return bytes(buf)
    
    # ── Public API ───────────────────────────────────────────────────
    
    async def inject_message(self, text: str):
        """
        Inject a mid-call instruction or statement using InjectAgentMessage.
        This forces the agent to speak the content or react to the instruction immediately.
        """
        if not self._active or not self._ws:
            return
            
        msg = {
            "type": "InjectAgentMessage",
            "text": text
        }
        await self._ws.send(json.dumps(msg))
        logger.info(f"💉 Injected agent message: {text}")
    
    def send_audio(self, audio_bytes: bytes):
        """Thread-safe: enqueue audio for sending. Called from worker thread."""
        if not self._active or not self._settings_ok:
            return
        try:
            self._send_q.put_nowait(audio_bytes)
        except queue.Full:
            pass  # Drop if backed up
    
    async def _send_settings(self):
        settings = {
            "type": "Settings",
            "audio": {
                "input": {
                    "encoding": "linear16",
                    "sample_rate": self.RATE
                },
                "output": {
                    "encoding": "linear16",
                    "sample_rate": self.RATE,
                    "container": "none"
                }
            },
            "agent": {
                "language": "en",
                "listen": {
                    "provider": {
                        "type": "deepgram",
                        "model": config.DG_LISTEN_MODEL
                    }
                },
                "think": {
                    "provider": {
                        "type": config.DG_THINK_PROVIDER,
                        "model": config.DG_THINK_MODEL
                    },
                    "prompt": self.system_prompt or "You are a helpful phone assistant."
                },
                "speak": {
                    "provider": {
                        "type": "deepgram",
                        "model": config.DG_SPEAK_MODEL
                    }
                }
            }
        }
        if self.greeting:
            settings["agent"]["greeting"] = self.greeting
        
        await self._ws.send(json.dumps(settings))
        logger.debug("Settings sent")
    
    async def disconnect(self):
        self._active = False
        
        for task in [self._recv_task, self._send_task]:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._recv_task = None
        self._send_task = None
        
        # Drain remaining queue
        while not self._send_q.empty():
            try:
                self._send_q.get_nowait()
            except queue.Empty:
                break
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        logger.info("🔌 Deepgram disconnected")
    
    @property
    def is_active(self) -> bool:
        return self._active


def create_voice_agent() -> VoiceAgent:
    """Create a fresh VoiceAgent instance. Each worker/call gets its own isolated agent."""
    return VoiceAgent()
