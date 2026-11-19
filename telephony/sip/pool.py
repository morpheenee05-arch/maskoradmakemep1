"""SipPool — drop-in pool for p1_runner that hands out SipPhoneClient
objects instead of Chrome-backed PhoneClients.

It wraps the existing Chrome pool: Chrome workers are still what logs the
accounts in and holds the session cookies (via CDP harvest), but the calls
themselves go over our own SIP/WebRTC stack — no dialer UI, no virtual
audio cables, no per-call Selenium round-trips.

Interface mirrors what p1_runner uses:
    acquire(timeout=None) -> SipPhoneClient | None
    release(client, recycle=True)
    workers  (list, used only for the worker count)
    _checked_out_worker_ids / _checked_out_at / _reclaim_orphaned
    (delegated to the wrapped pool — _safe_release pokes them directly)
"""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from .client import SipPhoneClient
from .cookies import harvest_google_cookies


class SipPool:
    def __init__(self, chrome_pool):
        self._chrome = chrome_pool

    # ── delegated bookkeeping (p1_runner._safe_release touches these) ──

    @property
    def workers(self):
        return self._chrome.workers

    @property
    def _checked_out_worker_ids(self):
        return self._chrome._checked_out_worker_ids

    @property
    def _checked_out_at(self):
        return self._chrome._checked_out_at

    def _reclaim_orphaned(self, client, worker_id: int, why: str):
        worker = getattr(client, "_chrome_worker", None)
        if worker is None:
            return
        self._chrome._reclaim_orphaned(worker, worker_id, why)

    # ── acquire / release ──

    def _client_for(self, worker) -> SipPhoneClient:
        client = getattr(worker, "_sip_client", None)
        if client is None:
            client = SipPhoneClient(
                worker_id=worker.worker_id,
                cookies_provider=lambda w=worker: harvest_google_cookies(w.driver),
                email=worker.account_email or getattr(worker, "credential_email", "") or "",
                number=worker.phone_number or "",
            )
            client._chrome_worker = worker
            worker._sip_client = client
        return client

    async def acquire(self, timeout: Optional[int] = None) -> Optional[SipPhoneClient]:
        worker = await self._chrome.acquire(timeout=timeout)
        if not worker:
            return None
        client = self._client_for(worker)
        # Warm the SIP registration in the background so the first INVITE
        # doesn't pay the mint+REGISTER cost. make_call() awaits the same
        # lock, so this never races a real call.
        if client._phone is None or not client._phone.registered:
            asyncio.ensure_future(self._warm(client))
        return client

    async def _warm(self, client: SipPhoneClient):
        try:
            await client.wait_until_dialer_ready(timeout=15)
        except Exception as e:
            logger.debug(f"sip W{client.worker_id}: warm register failed: {e}")

    async def release(self, client, recycle: bool = True):
        worker = getattr(client, "_chrome_worker", None)
        if worker is None:
            return
        # recycle=False: the SIP client needs no page reload / memory
        # recycle — skipping it keeps the Chrome pool's counters untouched,
        # and Chrome stays logged in for the next cookie harvest.
        await self._chrome.release(worker, recycle=False)

    async def close(self):
        for worker in self._chrome.workers:
            client = getattr(worker, "_sip_client", None)
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
