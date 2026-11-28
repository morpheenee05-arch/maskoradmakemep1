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
from .gv_auth import VoiceAPIClient, random_client_id, sip_register_info_body
from .media import SipMediaSession, AiortcMissingError
from .proxy_config import mask_proxy, proxy_for_worker
from .sip_ws import GVWebPhone, SipError

REREGISTER_MARGIN = 300

# One sipDeviceId per Google account, persisted across restarts. The real web
# dialer mints a single id (voice_module_set DI token) and reuses it for every
# sipregisterinfo/get; a fresh random id on each REGISTER looks like dozens of
# devices on one account and is a ban signal.
import json as _json
import threading as _threading
from pathlib import Path as _Path

_device_id_lock = _threading.Lock()


def _device_id_store_path() -> "_Path":
    try:
        import config
        return config.BASE_DIR / ".sip_device_ids.json"
    except Exception:
        return _Path(".sip_device_ids.json")


def persistent_client_id(identity: str) -> str:
    """Return the stable sipDeviceId for one account, creating it once."""
    identity = (identity or "").strip() or "unknown"
    with _device_id_lock:
        path = _device_id_store_path()
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        cid = data.get(identity)
        if not cid:
            cid = random_client_id()
            data[identity] = cid
            try:
                path.write_text(
                    _json.dumps(data, indent=1, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return cid


class SipPhoneClient:
    def __init__(self, worker_id: int, cookies_provider: Callable[[], dict],
                 email: str = "", number: str = "", stream_idx: int = 0):
        self.worker_id = worker_id
        self.stream_idx = stream_idx
        # Log tag: W4 for a single stream, W4s1/W4s2 when several streams
        # share one account.
        self._tag = f"W{worker_id}" if not stream_idx else f"W{worker_id}s{stream_idx}"
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

        # Per-call signaling trace — explains WHY a call never reached 200 OK
        # (rings seen, early media present, final SIP failure code).
        self._sip_ring_count = 0
        self._sip_early_media = False
        self._sip_fail_code = None
        self._sip_fail_reason = None
        # Timestamps for BYE diagnostics (how long the call lived before the
        # far end hung up — organic hangup vs systematic network teardown).
        self._call_start_ts: Optional[float] = None
        self._answered_at: Optional[float] = None

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
        proxy = getattr(self, "_proxy_url", None)
        if proxy:
            # The mint POST is authenticated with the account's cookies, so it
            # must exit through the same IP as the signaling WebSocket —
            # otherwise Google sees one account from two IPs at once.
            api.session.proxies.update({"http": proxy, "https": proxy})
        # Stable per-account sipDeviceId — the web dialer reuses ONE id for
        # the account's whole life; a new random id per mint is what burns it.
        cid = persistent_client_id(self.account_email or f"worker{self.worker_id}")
        return api.get_sip_register_info(body=sip_register_info_body(cid))

    async def _ensure_registered(self):
        async with self._reg_lock:
            if self._phone is not None and self._phone.registered:
                return
            # Resolved per registration, so /proxy changes apply without a
            # pool rebuild; a dead proxy fails loudly here, never falls back
            # to the agent IP silently.
            proxy = proxy_for_worker(self.worker_id)
            self._proxy_url = proxy
            creds = await asyncio.to_thread(self._mint_creds)
            phone = GVWebPhone(creds, on_event=self._on_sip_event, proxy=proxy)
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
            via = f" via proxy {mask_proxy(proxy)}" if proxy else " (direct)"
            logger.info(f"sip {self._tag}: registered{via}")

    async def _drop_registration(self, why: str):
        logger.warning(f"sip {self._tag}: dropping registration ({why})")
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
            self._answered_at = time.monotonic()
            if self._answered_ev is not None:
                self._answered_ev.set()
            sdp = info.get("sdp")
            if sdp and self._media is not None:
                asyncio.ensure_future(self._apply_answer(sdp))
        elif kind == "ringing":
            self._sip_ring_count += 1
        elif kind == "ack_resent":
            # The far end retransmitted its 200 OK — it has NOT accepted our
            # ACK. We re-sent it; if these pile up, the dialog lives on
            # borrowed time (timer H, BYE at ~32s after answer).
            self._sip_ack_resent = getattr(self, "_sip_ack_resent", 0) + 1
            logger.warning(
                f"sip {self._tag}: far end retransmitted 200 OK — ACK re-sent "
                f"(#{self._sip_ack_resent}); repeated lines here = our ACK is "
                f"not being accepted (timer-H BYE at ~32s)")
        elif kind == "early_media":
            self._sip_early_media = True
            sdp = info.get("sdp")
            if sdp and self._media is not None:
                asyncio.ensure_future(self._apply_answer(sdp))
        elif kind in ("failed", "bye"):
            was_active = self._state == "active"
            self._state = "ended"
            if kind == "failed":
                self._sip_fail_code = info.get("code")
                self._sip_fail_reason = info.get("reason")
                logger.warning(
                    f"sip {self._tag}: call FAILED by network: "
                    f"{info.get('code')} {info.get('reason')}")
            elif not was_active and self._sip_fail_code is None:
                # Pre-answer BYE — the far end gave up while we were ringing.
                self._sip_fail_code = "bye"
                self._sip_fail_reason = info.get("reason") or "remote ended during setup"
                logger.info(
                    f"sip {self._tag}: pre-answer BYE (reason={info.get('reason') or '—'})")
            else:
                talk = (f"{round(time.monotonic() - self._answered_at, 1)}s"
                        if self._answered_at else "unknown")
                logger.info(
                    f"sip {self._tag}: remote BYE after {talk} of talk "
                    f"(reason={info.get('reason') or '—'})")
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
            logger.warning(f"sip {self._tag}: media failed — marking call ended")
            self._state = "ended"
            self._media_failed = True
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
            logger.warning(f"sip {self._tag}: accept_answer failed: {e}")

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
            if self._phone is not None:
                self._phone.rtp_stat_provider = media.rtp_stat
            self._answered_ev = asyncio.Event()
            self._end_ev = asyncio.Event()
            self._media_failed = False
            self._sip_ring_count = 0
            self._sip_early_media = False
            self._sip_fail_code = None
            self._sip_fail_reason = None
            self._sip_ack_resent = 0
            self._state = "calling"
            self._call_start_ts = time.monotonic()
            self._answered_at = None
            offer = await media.create_offer()
            await self._phone.start_call(phone, offer)
            self._active_calls[call_id] = phone
            self._fail_streak = 0
            return True
        except Exception as e:
            # Full type + traceback: a bare '{e}' is empty for asyncio
            # timeouts, which used to make this line diagnostically useless.
            logger.opt(exception=True).warning(
                f"sip {self._tag}: make_call failed: {type(e).__name__}: {e}")
            await self._teardown_media()
            # Back off on consecutive failures — a fast-failing client must
            # not burn the whole number queue in seconds.
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
            if self._fail_streak >= 3:
                backoff = min(2 * self._fail_streak, 15)
                logger.warning(
                    f"sip {self._tag}: {self._fail_streak} consecutive "
                    f"make_call failures — backing off {backoff}s")
                await asyncio.sleep(backoff)
            return False

    async def wait_for_connect(self, call_id: str, timeout: int = 45) -> bool:
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
        if getattr(self, "_media_failed", False):
            return "media_failed"
        return "ended"

    def sip_call_diag(self) -> dict:
        """Signaling trace of the current/last call — why no 200 OK happened."""
        d: dict = {}
        if self._sip_ring_count:
            d["sip_rings"] = self._sip_ring_count
        if self._sip_early_media:
            d["sip_early_media"] = True
        if getattr(self, "_sip_ack_resent", 0):
            d["ack_resent"] = self._sip_ack_resent
        if getattr(self, "_media_failed", False):
            d["media_failed"] = True
        if self._sip_fail_code is not None:
            d["sip_fail_code"] = self._sip_fail_code
            d["sip_fail_reason"] = self._sip_fail_reason or ""
        return d

    def check_ban_status(self):
        return False, ""

    async def hangup(self, call_id: str = ""):
        phone = self._phone
        try:
            if phone is not None:
                await phone.hangup()
        except Exception as e:
            logger.debug(f"sip {self._tag}: hangup: {e}")
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
            logger.warning(f"sip {self._tag}: recover failed: {e}")
            return False

    # ── lifecycle ──

    async def close(self):
        await self._teardown_media()
        await self._drop_registration("client close")
