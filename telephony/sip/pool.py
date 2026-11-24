"""SipPool — drop-in pool for p1_runner that hands out SipPhoneClient
objects instead of Chrome-backed PhoneClients.

It wraps the existing Chrome pool: Chrome workers are still what logs the
accounts in and holds the session cookies (via CDP harvest), but the calls
themselves go over our own SIP/WebRTC stack — no dialer UI, no virtual
audio cables, no per-call Selenium round-trips.

Multi-stream: one GV account can carry several simultaneous calls (proven
by the dual-call experiment — two registrations on one credential set, both
answered, both with media). SipPool therefore builds `streams_per_account`
slots per authenticated Chrome worker; every slot is its own SipPhoneClient
(own WebSocket registration, media session, audio bridge and scripted
handler), so p1_runner sees N_accounts × K_streams independent lines.

Interface mirrors what p1_runner uses:
    acquire(timeout=None) -> SipPhoneClient | None
    release(client, recycle=True)
    workers  (slot list — p1_runner uses its length for the task count)
    _checked_out_worker_ids / _checked_out_at / _reclaim_orphaned
    (slot-level bookkeeping; _safe_release pokes them directly)
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from loguru import logger

import config
from .client import SipPhoneClient
from .cookies import harvest_google_cookies

# 2 streams are proven by the dual-call test; allow up to 4 for experiments,
# but never below 1 (0 would silently build an empty pool).
MAX_STREAMS_PER_ACCOUNT = 4


class SipPool:
    def __init__(self, chrome_pool, streams_per_account: int = 1):
        self._chrome = chrome_pool
        k = int(streams_per_account or 1)
        self._streams = max(1, min(MAX_STREAMS_PER_ACCOUNT, k))

        # Slot-level checkout bookkeeping (p1_runner._safe_release pokes these
        # on a timed-out release; keyed by slot id, NOT by chrome worker id).
        self._checked_out_worker_ids: set = set()
        self._checked_out_at: dict = {}

        self._slots: list = []
        self._free: asyncio.Queue = asyncio.Queue()

        workers = [
            w for w in chrome_pool.workers
            if getattr(w, "driver", None) is not None
            and getattr(w, "is_authenticated", False)
        ]
        if not workers:
            raise RuntimeError(
                "no authenticated Chrome workers — SIP needs live sessions "
                "to harvest cookies from"
            )

        for w in workers:
            for client in self._clients_for(w):
                client._slot_id = len(self._slots)
                self._slots.append(client)
                self._free.put_nowait(client)

        logger.info(
            f"SipPool ready: {len(self._slots)} SIP line(s) = "
            f"{len(workers)} account(s) × {self._streams} stream(s)"
        )

    # ── slot construction (cached on the chrome worker across campaigns) ──

    def _clients_for(self, worker) -> list:
        """Return `streams` SipPhoneClients for one account, reusing clients
        from the previous campaign so warm SIP registrations survive."""
        existing = getattr(worker, "_sip_clients", None) or []
        want = self._streams

        # Selenium is not thread-safe: two streams minting credentials at the
        # same moment would drive the same Chrome concurrently. One lock per
        # account serializes the (fast) CDP cookie harvest.
        lock = getattr(worker, "_sip_cookie_lock", None)
        if lock is None:
            lock = threading.Lock()
            worker._sip_cookie_lock = lock

        def _provider(w=worker, lk=lock):
            with lk:
                return harvest_google_cookies(w.driver)

        while len(existing) < want:
            client = SipPhoneClient(
                worker_id=worker.worker_id,
                cookies_provider=_provider,
                email=worker.account_email
                or getattr(worker, "credential_email", "") or "",
                number=worker.phone_number or "",
                stream_idx=len(existing),
            )
            client._chrome_worker = worker
            existing.append(client)
        # Streams setting lowered: close the surplus registrations instead of
        # leaving idle keepalive loops running forever.
        for extra in existing[want:]:
            try:
                asyncio.ensure_future(extra.close())
            except Exception:
                pass
        existing = existing[:want]
        worker._sip_clients = existing
        return existing

    # ── p1_runner interface ──

    @property
    def workers(self):
        """p1_runner uses len(pool.workers) as the campaign task count."""
        return self._slots

    def _reclaim_orphaned(self, client, worker_id: int, why: str):
        sid = getattr(client, "_slot_id", None)
        if sid is None:
            return
        if sid not in self._checked_out_worker_ids:
            return
        self._checked_out_worker_ids.discard(sid)
        self._checked_out_at.pop(sid, None)
        logger.warning(
            f"sip {getattr(client, '_tag', '?')}: release interrupted "
            f"({why}) — slot requeued"
        )
        self._free.put_nowait(client)

    async def acquire(self, timeout: Optional[int] = None) -> Optional[SipPhoneClient]:
        if timeout is None:
            timeout = int(getattr(config, "GV_WORKER_ACQUIRE_TIMEOUT_SEC", 30))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                logger.error(
                    f"sip pool: acquire timeout ({timeout}s) — "
                    f"{self._free.qsize()}/{len(self._slots)} slot(s) free"
                )
                return None
            try:
                client = await asyncio.wait_for(
                    self._free.get(), timeout=min(remaining, 2.0)
                )
            except asyncio.TimeoutError:
                continue
            w = getattr(client, "_chrome_worker", None)
            if w is not None and (
                w.driver is None or not getattr(w, "is_authenticated", False)
            ):
                # The account's Chrome died (OOM/restart) — its slots can't
                # mint fresh SIP credentials, so parking the slot beats
                # burning numbers as 'failed'. The slot rejoins on the next
                # campaign once the watchdog has revived the browser.
                logger.warning(
                    f"sip {getattr(client, '_tag', '?')}: account's Chrome "
                    f"is not live — parking this slot for the rest of the run"
                )
                continue
            sid = client._slot_id
            self._checked_out_worker_ids.add(sid)
            self._checked_out_at[sid] = time.time()
            # Warm the SIP registration in the background so the first INVITE
            # doesn't pay the mint+REGISTER cost. make_call() awaits the same
            # lock, so this never races a real call.
            if client._phone is None or not client._phone.registered:
                asyncio.ensure_future(self._warm(client))
            return client

    async def prewarm(self, concurrency: int = 4):
        """Register every slot before the campaign starts dialing.

        Without this the first N calls each pay mint+REGISTER inline, and a
        burst of simultaneous registrations regularly times out — burning the
        first numbers as 'failed'. Returns (ready, total).
        """
        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(client):
            async with sem:
                try:
                    return await client.wait_until_dialer_ready(timeout=20)
                except Exception:
                    return False

        results = await asyncio.gather(*(_one(c) for c in self._slots))
        ready = sum(1 for r in results if r)
        logger.info(
            f"sip pool prewarm: {ready}/{len(self._slots)} line(s) registered"
        )
        return ready, len(self._slots)

    async def _warm(self, client: SipPhoneClient):
        try:
            await client.wait_until_dialer_ready(timeout=15)
        except Exception as e:
            logger.debug(f"sip {client._tag}: warm register failed: {e}")

    async def release(self, client, recycle: bool = True):
        """Return a slot to the free queue.

        `recycle` is accepted for interface compatibility and ignored: a SIP
        line needs no page reload / memory recycle, and the account's Chrome
        is just a cookie jar — it is never checked out or recycled here.
        """
        sid = getattr(client, "_slot_id", None)
        if sid is None:
            return
        if sid not in self._checked_out_worker_ids:
            logger.warning(
                f"sip {getattr(client, '_tag', '?')} double-release blocked"
            )
            return
        self._checked_out_worker_ids.discard(sid)
        self._checked_out_at.pop(sid, None)
        self._free.put_nowait(client)

    async def close(self):
        for client in self._slots:
            try:
                await client.close()
            except Exception:
                pass
