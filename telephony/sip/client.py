"""
SipPhoneClient — one Google Voice account reachable over our own SIP/WebRTC
stack, presenting the same call interface the p1 machinery expects from a
Chrome-backed gv_client:

    make_call(phone, call_id) -> bool        (INVITE sent)
    wait_for_connect(call_id, timeout) -> bool   (200 OK = callee answered)
    hangup(call_id)                          (BYE / CANCEL)
    check_call_status() -> "active" | "ended" | "disconnected"   (sync!)
    check_ban_status() -> (False, "")
    wait_until_dialer_ready(timeout) / recover_dialer_state(reason)

plus worker bookkeeping attrs (worker_id, capture_device, playback_device,
_p1_handler*, _last_p1_recordings, _active_calls).

Audio goes through telephony.sip.audio_bridge.SipAudioManager — the
ScriptedCallHandler is given that instead of OS devices, so DTMF (Goertzel),
voicemail STT and TTS playback all work unchanged on the RTP stream.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from loguru import logger

from .audio_bridge import SipAudioManager
from .gv_auth import VoiceAPIClient
from .media import SipMediaSession, AiortcMissingError
from .sip_ws import GVWebPhone, SipError

REREGISTER_MARGIN = 300


class SipPhoneClient:
    def __init__(self, worker_id: int, cookies_provider: Callable[[], dict],
                 email: str = "", number: str = ""):
        self.worker_id = worker_id
        self.account_email = email
        self.phone_number = number
        self._cookies_provider = cookies_provider  # () -> cookie dict (CDP)

        # p1_runner bookkeeping attrs (mirroring the Chrome client)
        self.capture_device = "sip"
        self.playback_device = "sip"
        self._p1_handler = None
        self._p1_handler_profile = None
        self._p1_handler_warm = False
        self._last_p1_recordings = None
        self._active_calls: dict = {}

        self.sip_audio = SipAudioManager(client=self)

        self._phone: Optional[GVWebPhone] = None
        self._reg_lock = asyncio.Lock()
        self._cred_ts = 0.0

        # call state (updated from the event loop; read sync from executor)
        self._state = "idle"          # idle | calling | active | ended | failed
        self._answered_ev: Optional[asyncio.Event] = None
        self._end_ev: Optional[asyncio.Event] = None
        self._media: Optional[SipMediaSession] = None

    # ── registration ──

    def _mint_creds(self):
        cookies = self._cookies_provider() or {}
        if not any(k in cookies for k in (
                "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")):
            raise SipError(
                "no SAPISID-family cookies — worker session expired?")
        flat = {k: (v.get("value", "") if isinstance(v, dict) else v)
                for k, v in cookies.items()}
        api = VoiceAPIClient(flat)
        return api.get_sip_register_info()

    async def _ensure_registered(self):
        async with self._reg_lock:
            if self._phone is not None and self._phone.registered:
                return
            creds = await asyncio.to_thread(self._mint_creds)
            phone = GVWebPhone(creds, on_event=self._on_sip_event)
            await asyncio.wait_for(phone.connect(), timeout=20)
            try:
                await asyncio.wait_for(phone.register(), timeout=30)
            except Exception:
                await phone.close()
                raise
            if self._phone is not None:
                try:
                    await self._phone.close()
                except Exception:
                    pass
            self._phone = phone
            self._cred_ts = time.time()
            logger.info(f"sip W{self.worker_id}: registered")

    async def _drop_registration(self, why: str):
        logger.warning(f"sip W{self.worker_id}: dropping registration ({why})")
        phone, self._phone = self._phone, None
        if phone is not None:
            try:
                await phone.close()
            except Exception:
                pass

    # ── SIP events ──

    def _on_sip_event(self, kind: str, info: dict):
        if kind == "answered":
            self._state = "active"
            if self._answered_ev is not None:
                self._answered_ev.set()
            sdp = info.get("sdp")
            if sdp and self._media is not None:
                asyncio.ensure_future(self._apply_answer(sdp))
        elif kind == "early_media":
            sdp = info.get("sdp")
            if sdp and self._media is not None:
                asyncio.ensure_future(self._apply_answer(sdp))
        elif kind in ("failed", "bye"):
            self._state = "ended"
            for ev in (self._answered_ev, self._end_ev):
                if ev is not None:
                    ev.set()
        elif kind == "transport_lost":
            self._state = "ended"
            asyncio.ensure_future(self._drop_registration("transport_lost"))
            for ev in (self._answered_ev, self._end_ev):
                if ev is not None:
                    ev.set()

    def _on_media_state(self, state: str):
        # ICE/DTLS failure mid-call = dead audio — treat as ended so the
        # hangup monitor stops the IVR script instead of playing the whole
        # script into dead air. ("closed" is NOT an error — it fires from our
        # own teardown in _teardown_media.)
        if state == "failed" and self._state in ("calling", "active"):
            logger.warning(f"sip W{self.worker_id}: media failed — marking call ended")
            self._state = "ended"
            for ev in (self._answered_ev, self._end_ev):
                if ev is not None:
                    ev.set()

    async def _apply_answer(self, sdp: str):
        media = self._media
        if media is None:
            return
        try:
            await media.accept_answer(sdp)
        except Exception as e:
            logger.warning(f"sip W{self.worker_id}: accept_answer failed: {e}")

    # ── p1 call interface ──

    async def make_call(self, phone: str, call_id: str) -> bool:
        try:
            await self._ensure_registered()
            # Defensive: a stale dialog from a previous call must never block
            # this one ("call already in progress" storm).
            if self._phone is not None and getattr(self._phone, "dialog", None) is not None:
                try:
                    await self._phone.hangup()
                except Exception:
                    pass
            try:
                media = SipMediaSession(on_state=self._on_media_state)
            except AiortcMissingError as e:
                logger.error(str(e))
                return False
            self._media = media
            self.sip_audio.bind_session(media)
            self._answered_ev = asyncio.Event()
            self._end_ev = asyncio.Event()
            self._state = "calling"
            offer = await media.create_offer()
            await self._phone.start_call(phone, offer)
            self._active_calls[call_id] = phone
            self._fail_streak = 0
            return True
        except Exception as e:
            logger.warning(f"sip W{self.worker_id}: make_call failed: {e}")
            await self._teardown_media()
            # Back off on consecutive failures — a fast-failing client must
            # not burn the whole number queue in seconds.
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
            if self._fail_streak >= 3:
                backoff = min(2 * self._fail_streak, 15)
                logger.warning(
                    f"sip W{self.worker_id}: {self._fail_streak} consecutive "
                    f"make_call failures — backing off {backoff}s")
                await asyncio.sleep(backoff)
            return False

    async def wait_for_connect(self, call_id: str, timeout: int = 30) -> bool:
        ev = self._answered_ev
        if ev is None:
            return False
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._state == "active"

    def check_call_status(self) -> str:
        """Sync, called from an executor thread by the hangup monitor."""
        if self._state == "active":
            return "active"
        return "ended"

    def check_ban_status(self):
        return False, ""

    async def hangup(self, call_id: str = ""):
        phone = self._phone
        try:
            if phone is not None:
                await phone.hangup()
        except Exception as e:
            logger.debug(f"sip W{self.worker_id}: hangup: {e}")
        self._active_calls.pop(call_id, None)
        await self._teardown_media()
        self._state = "idle"

    async def _teardown_media(self):
        media, self._media = self._media, None
        self.sip_audio.detach_session()
        if media is not None:
            try:
                await media.close()
            except Exception:
                pass

    # ── readiness (p1_runner._ensure_ready) ──

    async def wait_until_dialer_ready(self, timeout: float = 1.25) -> bool:
        if self._phone is not None and self._phone.registered:
            return True
        try:
            await asyncio.wait_for(self._ensure_registered(), timeout=max(5.0, timeout))
            return True
        except Exception:
            return False

    async def recover_dialer_state(self, reason: str) -> bool:
        await self._drop_registration(f"recover: {reason}")
        try:
            await self._ensure_registered()
            return True
        except Exception as e:
            logger.warning(f"sip W{self.worker_id}: recover failed: {e}")
            return False

    # ── lifecycle ──

    async def close(self):
        await self._teardown_media()
        await self._drop_registration("client close")
