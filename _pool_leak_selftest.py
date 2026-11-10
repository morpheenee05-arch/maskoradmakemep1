"""Self-test: worker-pool leak regressions (no real browser).

Covers the failure seen in production where acquire() timed out while the
snapshot still claimed ready=6/8:
  1. normal release returns the worker to the queue
  2. a release CANCELLED mid-cleanup requeues a healthy worker (no ghost)
  3. a release CANCELLED mid-cleanup of an unhealthy worker marks it failed
     and schedules a revive instead of orphaning it
  4. p1 worker loop requeues the number on acquire timeout (no false failed)
  5. double-release is still blocked

Run:  python _pool_leak_selftest.py
"""
import asyncio
import faulthandler
import sys

faulthandler.dump_traceback_later(20, exit=True)

import config  # noqa: F401  (env-backed defaults)

from telephony import p1_runner
from telephony.google_voice_pool import PhonePool


class FakeWorker:
    def __init__(self, worker_id=0, healthy=True):
        self.worker_id = worker_id
        self.is_authenticated = healthy
        self.driver = object() if healthy else None
        self.needs_restart = False
        self.is_banned = False
        self.is_spam_flagged = False
        self.login_state = "ready" if healthy else "needs_login"
        self.restart_reason = ""
        self.last_error = None
        self.phone_number = "+15550000000"
        self.calls_served = 0
        self.last_rss_mb = None
        self.last_heap_pct = None
        self.failed_restarts = 0
        self.restart_count = 0
        self.oom_events = 0
        self._revive_running = False
        self._active_calls = {}
        self.hang_dialer_ready = False

    def session_lost_reason(self):
        return getattr(self, "_lost_reason", None)

    async def refresh_memory(self, budget=8.0):
        return None

    def recycle_plan(self):
        return None, None

    async def recycle_page(self, reason=""):
        return True

    async def wait_until_dialer_ready(self, timeout=0.35):
        if self.hang_dialer_ready:
            await asyncio.Event().wait()  # hangs until cancelled
        return True

    async def recover_dialer_state(self, reason=""):
        return True

    async def hangup(self, cid):
        return True


def _make_pool(*workers, queued=True):
    pool = PhonePool()
    pool.workers = list(workers)
    pool._state = "ready"
    # Keep acquire() from triggering a real pool re-init (real Chrome) when a
    # test drains the queue to zero ready workers.
    import time as _t
    pool._last_recovery_attempt_at = _t.time()
    for w in pool.workers:
        pool._worker_states[w.worker_id] = {"state": "ready"}
        if queued:
            pool._in_queue.add(w.worker_id)
            pool._available_workers.put_nowait(w)
    return pool


async def test_normal_release():
    w = FakeWorker(0)
    pool = _make_pool(w)
    got = await pool.acquire(timeout=1)
    assert got is w, "acquire should hand out the queued worker"
    await pool.release(w)
    assert pool._available_workers.qsize() == 1, "worker must be back in queue"
    assert 0 in pool._in_queue
    assert pool._worker_states[0]["state"] == "ready"
    print("PASS normal release -> requeued")


async def test_cancelled_release_healthy():
    w = FakeWorker(0)
    w.hang_dialer_ready = True  # cleanup hangs like a dead-browser release
    pool = _make_pool(w)
    await pool.acquire(timeout=1)
    # Pretend the worker was actually handed out (acquire found it unhealthy
    # only if driver is None — ours is healthy, so it was checked out).
    assert 0 in pool._checked_out_worker_ids

    t = asyncio.create_task(pool.release(w))
    await asyncio.sleep(0.2)  # let it get stuck inside the cleanup
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    assert 0 not in pool._checked_out_worker_ids
    assert 0 in pool._in_queue, "healthy worker must be requeued, not orphaned"
    assert pool._available_workers.qsize() == 1
    again = await pool.acquire(timeout=1)
    assert again is w, "reclaimed worker must be acquirable again"
    print("PASS cancelled release (healthy) -> reclaimed into queue")


async def test_cancelled_release_unhealthy():
    w = FakeWorker(1, healthy=False)
    w.driver = None
    w.is_authenticated = False
    w.login_state = "ready"  # was signed in; the browser died
    w.hang_dialer_ready = True
    w.failed_restarts = 99  # park: _schedule_revive must not spawn a task
    pool = _make_pool(w)
    got = await pool.acquire(timeout=1)
    assert got is None, "unhealthy worker must not be handed out"
    # acquire's unhealthy path marked it failed; reset to ready to simulate
    # the pre-death state, then check the release-cancel path directly.
    pool._worker_states[1]["state"] = "ready"
    pool._checked_out_worker_ids.add(1)
    pool._checked_out_at[1] = 0.0

    t = asyncio.create_task(pool.release(w))
    await asyncio.sleep(0.2)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    assert 1 not in pool._in_queue, "unhealthy worker must NOT be requeued"
    assert pool._worker_states[1]["state"] == "failed"
    assert pool._available_workers.qsize() == 0
    print("PASS cancelled release (unhealthy) -> failed + revive path")


async def test_p1_requeue_on_acquire_timeout():
    w = FakeWorker(0)

    class StarvedPool:
        def __init__(self):
            self.workers = [w]
            self.calls = 0

        async def acquire(self, timeout=None):
            self.calls += 1
            if self.calls <= 2:
                return None  # simulate degraded pool (spam check / deaths)
            return w

        async def release(self, worker, recycle=True):
            return None

    pool = StarvedPool()
    claimed = ["+15550100"]
    requeued = []

    async def claim_next():
        await asyncio.sleep(0)
        return claimed.pop(0) if claimed else None

    async def requeue(phone):
        requeued.append(phone)
        claimed.append(phone)

    events = []

    async def on_event(phone, res):
        events.append((phone, res))

    async def fake_execute_one(*a, **kw):
        return "hangup"

    orig = p1_runner.execute_one
    p1_runner.execute_one = fake_execute_one
    try:
        report = await asyncio.wait_for(
            p1_runner._run_p1_workers(
                pool, claim_next, "suspicious", on_event,
                lambda: False, 5, 5, requeue=requeue,
            ),
            timeout=10,
        )
    finally:
        p1_runner.execute_one = orig

    assert requeued == ["+15550100", "+15550100"], requeued
    assert report["counts"] == {"hangup": 1}, report
    assert report["done"] == 1
    assert events == [("+15550100", "hangup")]
    print("PASS p1 acquire-timeout -> number requeued, never false-failed")


async def test_double_release_blocked():
    w = FakeWorker(0)
    pool = _make_pool(w)
    await pool.acquire(timeout=1)
    await pool.release(w)
    await pool.release(w)  # must warn + return, not double-queue
    assert pool._available_workers.qsize() == 1
    print("PASS double release blocked")


class _Profile:
    def __init__(self, name):
        self.name = name


async def test_spam_wipe_becomes_login_slot():
    """After spamcheck wipes cookies, the worker is a vacant /login slot."""
    w = FakeWorker(2, healthy=True)
    w.profile_dir = _Profile(".chrome_profile_3")
    w.phone_number = "+15551212"
    w.account_email = "spam@gmail.com"
    w.credentials = {"email": "spam@gmail.com", "password": "secret"}
    w.login_state = "ready"

    def wipe():
        w.driver = None
        w.is_authenticated = False
        w.is_spam_flagged = False
        w.phone_number = None
        w.account_email = None
        w.credentials = None
        w.login_state = "needs_login"
        w.last_error = "previous number +15551212 flagged as spam — cookies cleared"

    w.wipe_cookies = wipe

    pool = _make_pool(w)
    pool._credentials.put(".chrome_profile_3", "spam@gmail.com", "secret")
    got = await pool.acquire(timeout=1)
    assert got is w

    await pool.disable_worker_spam(w)
    assert pool._worker_states[2]["state"] == "needs_login"
    assert pool._credentials.get(".chrome_profile_3") is None
    assert w in pool.workers_needing_login()
    assert w.login_state == "needs_login"
    assert not w.is_spam_flagged

    await pool.release(w)
    assert 2 not in pool._in_queue, "vacant login slot must not take calls"
    assert pool._worker_states[2]["state"] == "needs_login"
    assert pool._available_workers.qsize() == 0
    print("PASS spam wipe -> needs_login slot for /login, not call queue")


async def test_logged_out_recovery_keeps_account():
    """Logout / auth page is not a Google burn — keep cookies and creds."""
    w = FakeWorker(3, healthy=True)
    w.profile_dir = _Profile(".chrome_profile_4")
    w.account_email = "dead@gmail.com"
    w._lost_reason = "logged out (auth page)"
    wiped = []

    def wipe():
        wiped.append(1)

    w.wipe_cookies = wipe

    async def not_ready(timeout=0.35):
        return False

    async def recover(reason=""):
        w.is_authenticated = False
        w.login_state = "needs_login"
        return False

    w.wait_until_dialer_ready = not_ready
    w.recover_dialer_state = recover

    pool = _make_pool(w)
    pool._credentials.put(".chrome_profile_4", "dead@gmail.com", "secret")
    got = await pool.acquire(timeout=1)
    assert got is w
    await pool.release(w)
    assert not wiped
    assert pool._credentials.get(".chrome_profile_4") is not None
    assert 3 not in pool._in_queue
    assert w in pool.workers_needing_login()
    print("PASS logged-out recovery -> account kept, no cookie wipe")


async def test_recovery_timeout_does_not_wipe():
    """Missing dialer after a timeout is a Chrome problem, not a ban."""
    w = FakeWorker(2, healthy=True)
    w.profile_dir = _Profile(".chrome_profile_3")
    w.account_email = "broken@gmail.com"
    wiped = []

    def wipe(*_a, **_k):
        wiped.append(1)

    w.wipe_cookies = wipe

    async def not_ready(timeout=0.35):
        return False

    async def recover(reason=""):
        w.is_authenticated = False
        w.login_state = "needs_login"
        w.last_error = "dialer recovery timeout"
        return False

    w.wait_until_dialer_ready = not_ready
    w.recover_dialer_state = recover

    pool = _make_pool(w)
    pool._credentials.put(".chrome_profile_3", "broken@gmail.com", "secret")
    got = await pool.acquire(timeout=1)
    assert got is w
    await pool.release(w)
    assert not wiped
    assert pool._credentials.get(".chrome_profile_3") is not None
    print("PASS recovery timeout -> no wipe, credentials kept")


async def test_banned_account_vacates_slot():
    """A Voice product-ban wipes cookies and frees the slot for /login."""
    w = FakeWorker(5, healthy=True)
    w.is_banned = True
    w.login_state = "banned"
    w.account_email = "bingingexercise689@gmail.com"
    w.credential_email = "bingingexercise689@gmail.com"
    w.profile_dir = _Profile(".chrome_profile_6")

    def wipe(*_a, **_k):
        w.driver = None
        w.is_banned = False
        w.is_authenticated = False
        w.phone_number = None
        w.account_email = None
        w.login_state = "needs_login"
        w.last_error = "account banned by Google"

    w.wipe_cookies = wipe
    w.clear_credentials = lambda: None

    pool = _make_pool(w)
    pool._credentials.put(
        ".chrome_profile_6", "bingingexercise689@gmail.com", "secret"
    )
    got = await pool.acquire(timeout=1)
    assert got is w
    await pool.release(w)
    assert pool._worker_states[5]["state"] == "needs_login"
    assert 5 not in pool._in_queue
    assert not w.is_banned
    assert w in pool.workers_needing_login()
    assert pool._credentials.get(".chrome_profile_6") is None
    print("PASS banned account -> cookies wiped, slot waits for /login")


async def test_no_form_does_not_wipe_session():
    """A chooser/stale-element failure is not a burned Google session."""
    w = FakeWorker(6, healthy=True)
    w.login_state = "needs_login"
    w.login_outcome = "no_form"
    w.last_error = "auto-login no_form: account chooser offered no usable option"
    w.account_email = "live@gmail.com"
    pool = _make_pool(w)
    assert pool._dead_account_reason(w) is None
    print("PASS no_form does not vacate / wipe cookies")


def test_offline_and_spawn_do_not_wipe():
    """DNS interstitial / Chrome spawn errors must not clear cookies."""
    from telephony.google_voice import PhoneClient
    from telephony.google_voice_pool import PhonePool

    assert PhoneClient._looks_like_offline_blob(
        "https://voice.google.com/u/0/calls",
        "",
        "DNS_PROBE_FINISHED_NO_INTERNET checking the network cables",
    )
    assert PhoneClient._looks_like_offline_blob("chrome://new-tab-page/", "", "")
    assert not PhoneClient._looks_like_offline_blob(
        "https://voice.google.com/u/0/calls", "", "make a call"
    )

    pool = PhonePool()
    w = FakeWorker(4, healthy=True)
    w._lost_reason = "dialer gone (session dead)"
    w._page_looks_offline = lambda: True
    assert pool._dead_account_reason(w) is None

    w2 = FakeWorker(3, healthy=False)
    w2.login_state = "needs_login"
    w2.last_error = "restart: browser spawn timeout"
    w2.account_email = "keep@gmail.com"
    w2.phone_number = "+14155550100"
    w2.has_credentials = lambda: True
    assert pool._dead_account_reason(w2) is None

    vacant = FakeWorker(1, healthy=False)
    vacant.login_state = "needs_login"
    vacant.last_error = "not ready (page=unknown)"
    vacant.phone_number = None
    vacant.account_email = None
    vacant.has_credentials = lambda: False
    assert pool._dead_account_reason(vacant) is None

    missing_num = FakeWorker(0, healthy=True)
    missing_num.phone_number = None
    missing_num.login_state = "ready"
    missing_num._lost_reason = "dialer gone (session dead)"
    assert pool._dead_account_reason(missing_num) is None

    banned = FakeWorker(5, healthy=True)
    banned.is_banned = True
    banned.account_email = "burned@gmail.com"
    assert "banned" in (pool._dead_account_reason(banned) or "")

    disabled = FakeWorker(6, healthy=False)
    disabled.login_state = "needs_login"
    disabled.login_outcome = "disabled"
    disabled.account_email = "off@gmail.com"
    disabled.has_credentials = lambda: True
    assert pool._dead_account_reason(disabled) == "google account disabled"
    print("PASS only Google ban/disabled wipe; logout/offline/missing dialer do not")


def test_login_stage_label():
    import inspect
    from telephony.google_voice_pool import PhonePool
    w = FakeWorker(0, healthy=False)
    w.is_authenticated = False
    w.login_state = "logging_in"
    w.login_outcome = None
    w.login_detail = "sign-in entering password"
    w.phone_number = None
    assert "password" in PhonePool.login_stage_label(w, "logging_in")
    w.login_outcome = "captcha"
    w.login_state = "captcha"
    assert "captcha" in PhonePool.login_stage_label(w)
    recycle_src = inspect.getsource(PhonePool.recycle_captcha_account)
    assert "wait_spawn" in recycle_src
    assert "reset_to_login_page" in recycle_src
    print("PASS login stage label follows the form and captcha")


async def main():
    await test_normal_release()
    await test_cancelled_release_healthy()
    await test_cancelled_release_unhealthy()
    await test_p1_requeue_on_acquire_timeout()
    await test_double_release_blocked()
    await test_spam_wipe_becomes_login_slot()
    await test_logged_out_recovery_keeps_account()
    await test_recovery_timeout_does_not_wipe()
    await test_banned_account_vacates_slot()
    await test_no_form_does_not_wipe_session()
    test_offline_and_spawn_do_not_wipe()
    test_login_stage_label()
    print("\nALL POOL LEAK TESTS PASSED")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
