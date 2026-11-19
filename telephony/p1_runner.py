"""
Context-free P1 (scripted IVR) executor — used by AGENT nodes.

This mirrors the call execution in bot/commands/call.py but WITHOUT any
Telegram/`context` coupling, so an agent can run /p1 jobs on its local
PhonePool and stream progress back over Redis.

Standalone /p1 keeps using its own code path in call.py (unchanged).
"""
import asyncio
import contextvars
import os
import time
import uuid
from typing import Awaitable, Callable, List, Optional

from loguru import logger

import config


# ── Per-call diagnostics ("why did this call end like that") ─────────────────
# Stored in a ContextVar, so every worker task inside _run_p1_workers keeps its
# own isolated state (create_task copies the context; execute_one is awaited
# directly, so its writes are visible to the worker that called it).
# The agent's on_event publisher attaches this as 'detail' to
# cancelled/failed/timeout results → the controller log shows the exact reason.
_call_diag: contextvars.ContextVar = contextvars.ContextVar(
    "p1_call_diag", default=None
)
_last_call_recordings: contextvars.ContextVar = contextvars.ContextVar(
    "p1_call_recordings", default=None
)


def take_last_call_recordings():
    """Recording paths handed off by the worker task (same task as on_event).

    execute_one() runs under asyncio.wait_for, which copies the context into a
    child task. A ContextVar set inside the call is invisible here, so the body
    stashes paths on the PhoneClient and the worker copies them into this
    ContextVar before awaiting on_event.
    """
    paths = _last_call_recordings.get()
    _last_call_recordings.set(None)
    return paths


def _take_client_recordings(gv_client):
    rec = getattr(gv_client, "_last_p1_recordings", None)
    try:
        gv_client._last_p1_recordings = None
    except Exception:
        pass
    return rec


def reset_diag() -> None:
    """Clear per-call diag (called by the worker loop before each number)."""
    _call_diag.set(None)


def get_last_diag() -> Optional[dict]:
    """Snapshot of the current task's call diag (JSON-safe fields only)."""
    d = _call_diag.get()
    return dict(d) if d else None


def _diag_set(**fields) -> None:
    d = dict(_call_diag.get() or {})
    d.update(fields)
    _call_diag.set(d)


def _mask_phone(phone: str) -> str:
    p = str(phone or "")
    return (p[:5] + "…" + p[-2:]) if len(p) > 8 else p


def _cancel_diag(why: str, t0: float, phone: str, gv_client) -> None:
    """Log + record a should_stop()-driven cancellation with full context."""
    elapsed = round(time.monotonic() - t0, 1)
    _diag_set(why=why, elapsed_s=elapsed, stop_flag=True)
    logger.warning(
        f"p1 CANCELLED [{why}] phone={_mask_phone(phone)} "
        f"worker={getattr(gv_client, 'worker_id', '?')} elapsed={elapsed}s "
        f"— the agent-local stop flag was SET at this checkpoint. Look UP this "
        f"log for the source: 'P1 HARD STOP triggered: reason=…' or "
        f"'Got job … type=p1stop' (a stop was requested on THIS agent)."
    )


async def _ensure_ready(gv_client, reason: str) -> bool:
    budget = max(5.0, float(getattr(config, "GV_RECOVERY_TIMEOUT_SEC", 20.0)))
    try:
        return await asyncio.wait_for(_ensure_ready_inner(gv_client, reason), timeout=budget)
    except asyncio.TimeoutError:
        logger.warning(f"_ensure_ready timeout ({reason})")
        return False
    except Exception as e:
        logger.debug(f"_ensure_ready error ({reason}): {e}")
        return False


async def _ensure_ready_inner(gv_client, reason: str) -> bool:
    try:
        if await gv_client.wait_until_dialer_ready(timeout=1.25):
            return True
        if await gv_client.recover_dialer_state(reason):
            return await gv_client.wait_until_dialer_ready(timeout=2.0)
    except Exception as e:
        logger.debug(f"_ensure_ready inner error ({reason}): {e}")
    return False


async def _safe_hangup(gv_client, call_id: str):
    try:
        await gv_client.hangup(call_id)
    except Exception:
        pass
    try:
        getattr(gv_client, "_active_calls", {}).pop(call_id, None)
    except Exception:
        pass


async def _safe_release(pool, gv_client):
    # Must exceed release()'s own worst case — a recycle reload plus dialer
    # recovery takes GV_RELEASE_TIMEOUT_SEC + GV_RECOVERY_TIMEOUT_SEC + ~15s,
    # and the memory probe before it up to ~8s. A shorter outer timeout used
    # to cancel healthy releases mid-flight, orphaning the worker (not checked
    # out, not queued, still shown as ready) until the pool bled dry.
    base = max(5.0, float(getattr(config, "GV_RELEASE_TIMEOUT_SEC", 25.0)))
    rec = max(10.0, float(getattr(config, "GV_RECOVERY_TIMEOUT_SEC", 20.0)))
    budget = base + rec + 30.0
    try:
        await asyncio.wait_for(pool.release(gv_client), timeout=budget)
    except Exception as e:
        logger.warning(f"p1 release timed out/failed: {e}")
        try:
            wid = getattr(gv_client, "worker_id", -1)
            getattr(pool, "_checked_out_worker_ids", set()).discard(wid)
            getattr(pool, "_checked_out_at", {}).pop(wid, None)
            # Backstop: release() reclaims internally on cancellation, but if
            # it never got that far, make sure the worker is not lost here.
            reclaim = getattr(pool, "_reclaim_orphaned", None)
            if reclaim:
                reclaim(gv_client, wid, f"release did not finish: {e}")
        except Exception:
            pass


async def execute_one(
    gv_client,
    phone: str,
    script_profile: str,
    connect_timeout: int = 30,
    ivr_timeout: int = 120,
    should_stop: Optional[Callable[[], bool]] = None,
    script_bundle: Optional[dict] = None,
) -> str:
    """One scripted call on an already-acquired worker.

    Reuses the worker's warm handler (STT/audio stay hot between calls);
    a cold init happens only on the first call, after failures, or when the
    script changes. Set GV_P1_WARM_HANDLER=0 to force cold-per-call.

    Returns: '1' | '2' | 'voicemail' | 'not_answered' | 'hangup' |
             'timeout' | 'failed' | 'cancelled'.
    """
    hard = max(
        60,
        int(getattr(config, "P1_CALL_HARD_TIMEOUT_SEC", 0) or 0)
        or (int(connect_timeout) + int(ivr_timeout) + 60),
    )
    _call_diag.set({
        "phone": _mask_phone(phone),
        "worker_id": getattr(gv_client, "worker_id", None),
        "stage": "start",
        "t0": time.monotonic(),
    })
    try:
        return await asyncio.wait_for(
            _execute_one_body(
                gv_client, phone, script_profile,
                connect_timeout, ivr_timeout, should_stop,
                script_bundle,
            ),
            timeout=hard,
        )
    except asyncio.TimeoutError:
        stage = (_call_diag.get() or {}).get("stage")
        _diag_set(why=f"hard_timeout({hard}s)")
        logger.warning(
            f"execute_one hard-timeout ({hard}s) for {_mask_phone(phone)} "
            f"at stage={stage} — forcing cleanup"
        )
        return "timeout"
    except asyncio.CancelledError:
        # A CancelledError reached us DESPITE no stop checkpoint firing.
        # This is the "phantom cancelled" case — log everything needed to
        # find who cancelled the call task.
        d = _call_diag.get() or {}
        t0 = d.get("t0")
        elapsed = round(time.monotonic() - t0, 1) if t0 else None
        cancelling = None
        try:
            t = asyncio.current_task()
            cancelling = t.cancelling() if t else None
        except Exception:
            pass
        stop_state = None
        try:
            stop_state = bool(should_stop()) if should_stop else None
        except Exception:
            pass
        _diag_set(
            why="cancelled_error", elapsed_s=elapsed,
            task_cancelling=cancelling, stop_flag=stop_state,
        )
        # opt(exception=True) attaches the full traceback of the CancelledError
        # — the stack's deepest frame is the exact await that was cancelled.
        logger.opt(exception=True).warning(
            f"p1 CANCELLED [cancelled_error] phone={_mask_phone(phone)} "
            f"worker={getattr(gv_client, 'worker_id', '?')} "
            f"stage={d.get('stage')} elapsed={elapsed}s "
            f"task_cancelling={cancelling} stop_flag={stop_state} — "
            f"task_cancelling>0 means an EXTERNAL task cancel (agent "
            f"hard-stop/watchdog — see 'P1 HARD STOP triggered' above); "
            f"task_cancelling=0 means a CancelledError bubbled up from "
            f"inside the call machinery itself."
        )
        return "cancelled"


async def _execute_one_body(
    gv_client,
    phone: str,
    script_profile: str,
    connect_timeout: int,
    ivr_timeout: int,
    should_stop: Optional[Callable[[], bool]],
    script_bundle: Optional[dict] = None,
) -> str:
    from audio.scripted_handler import ScriptedCallHandler
    from audio.script_profiles import profile_from_bundle

    call_id = str(uuid.uuid4())[:8]
    t0 = time.monotonic()
    handler = None
    monitor_task = None
    keep_warm = False
    try:
        if should_stop and should_stop():
            _cancel_diag("should_stop@pre_call", t0, phone, gv_client)
            return "cancelled"

        _diag_set(stage="audio_init")
        scripts = profile_from_bundle(script_bundle) if script_bundle else None
        script_sig = (script_profile, str((scripts or {}).get("intro"))[:64])

        # Reuse a warm per-worker handler when possible (STT/audio stay hot),
        # same as the standalone call.py path — a cold init + full stop per
        # call costs several seconds of every cycle.
        warm_ok = bool(int(os.getenv("GV_P1_WARM_HANDLER", "1")))
        handler = getattr(gv_client, "_p1_handler", None)
        if handler is not None and (
            not warm_ok
            or getattr(gv_client, "_p1_handler_profile", None) != script_sig
        ):
            try:
                await handler.stop()
            except Exception:
                pass
            handler = None
            try:
                gv_client._p1_handler = None
                gv_client._p1_handler_profile = None
                gv_client._p1_handler_warm = False
            except Exception:
                pass

        if handler is None:
            handler = ScriptedCallHandler(
                profile=script_profile,
                capture_device=gv_client.capture_device,
                playback_device=gv_client.playback_device,
                worker_id=gv_client.worker_id,
                scripts=scripts,
                audio_manager=getattr(gv_client, "sip_audio", None),
            )
            if not await handler.initialize():
                _diag_set(why="audio_init_failed", elapsed_s=round(time.monotonic() - t0, 1))
                return "failed"
            try:
                gv_client._p1_handler = handler
                gv_client._p1_handler_profile = script_sig
                gv_client._p1_handler_warm = False
            except Exception:
                pass
        handler.retain_recording = True

        if should_stop and should_stop():
            _cancel_diag("should_stop@after_audio_init", t0, phone, gv_client)
            return "cancelled"

        _diag_set(stage="make_call")
        prepare_task = asyncio.create_task(handler.prepare())

        success = await gv_client.make_call(phone, call_id)
        if not success:
            prepare_task.cancel()
            _diag_set(why="make_call_failed", elapsed_s=round(time.monotonic() - t0, 1))
            await _ensure_ready(gv_client, "p1 make_call failed")
            return "failed"

        if should_stop and should_stop():
            await _safe_hangup(gv_client, call_id)
            prepare_task.cancel()
            _cancel_diag("should_stop@after_make_call", t0, phone, gv_client)
            return "cancelled"

        if prepare_task.done() and prepare_task.exception() is not None:
            await _safe_hangup(gv_client, call_id)
            _diag_set(
                why=f"prepare_failed:{type(prepare_task.exception()).__name__}",
                elapsed_s=round(time.monotonic() - t0, 1),
            )
            await _ensure_ready(gv_client, "p1 prepare failed")
            return "failed"

        _diag_set(stage="wait_connect")
        connected = await gv_client.wait_for_connect(
            call_id, timeout=connect_timeout
        )
        if not connected:
            await _safe_hangup(gv_client, call_id)
            prepare_task.cancel()
            _diag_set(why="not_answered", elapsed_s=round(time.monotonic() - t0, 1))
            await _ensure_ready(gv_client, "p1 no-answer")
            # The handler never went live — it stays healthy for the next call.
            keep_warm = True
            return "not_answered"

        _diag_set(stage="prepare_wait")
        try:
            await asyncio.wait_for(prepare_task, timeout=20)
        except (asyncio.TimeoutError, Exception):
            try:
                prepare_task.cancel()
            except Exception:
                pass
            await _safe_hangup(gv_client, call_id)
            _diag_set(why="prepare_failed_or_timeout(20s)", elapsed_s=round(time.monotonic() - t0, 1))
            await _ensure_ready(gv_client, "p1 prepare failed")
            return "failed"

        if should_stop and should_stop():
            await _safe_hangup(gv_client, call_id)
            _cancel_diag("should_stop@after_connect", t0, phone, gv_client)
            return "cancelled"

        async def _hangup_monitor():
            loop = asyncio.get_running_loop()
            consec = 0
            last = None
            tick = 0
            await asyncio.sleep(1.5)
            while True:
                if should_stop and should_stop():
                    handler.force_result("hangup")
                    return
                try:
                    st = await loop.run_in_executor(
                        None, gv_client.check_call_status
                    )
                    if st == "media_failed":
                        # SIP media (ICE/DTLS) died — not a callee hangup;
                        # keep campaign stats honest.
                        handler.force_result("failed")
                        return
                    if st in ("ended", "disconnected", "force_ended"):
                        consec = consec + 1 if st == last else 1
                        last = st
                        if consec >= 2:
                            handler.force_result("hangup")
                            return
                    else:
                        consec, last = 0, None
                    # Ban probe every other tick — it is a second Selenium
                    # round-trip and changes far slower than call state.
                    tick += 1
                    if tick % 2 == 0 and hasattr(gv_client, "check_ban_status"):
                        banned, _ = await loop.run_in_executor(
                            None, gv_client.check_ban_status
                        )
                        if banned:
                            handler.force_result("hangup")
                            return
                except Exception:
                    pass
                await asyncio.sleep(1.0)

        monitor_task = asyncio.create_task(_hangup_monitor())

        _diag_set(stage="ivr_run")
        try:
            result = await asyncio.wait_for(handler.run(), timeout=ivr_timeout)
        except asyncio.TimeoutError:
            result = "timeout"
        except asyncio.CancelledError:
            result = "cancelled"
            _diag_set(
                why="cancelled_error@ivr_run",
                elapsed_s=round(time.monotonic() - t0, 1),
            )
            raise

        monitor_task.cancel()
        try:
            await monitor_task
        except (asyncio.CancelledError, Exception):
            pass

        await _safe_hangup(gv_client, call_id)
        await _ensure_ready(gv_client, "p1 cleanup")
        keep_warm = True
        return result or "timeout"

    except asyncio.CancelledError:
        if monitor_task:
            monitor_task.cancel()
        await _safe_hangup(gv_client, call_id)
        raise
    except Exception as e:
        logger.error(f"execute_one crashed for {_mask_phone(phone)}: {e}")
        _diag_set(
            why=f"exception:{type(e).__name__}",
            elapsed_s=round(time.monotonic() - t0, 1),
        )
        if monitor_task:
            monitor_task.cancel()
        await _safe_hangup(gv_client, call_id)
        await _ensure_ready(gv_client, "p1 exception")
        return "failed"
    finally:
        try:
            rec = handler.pop_call_recordings() if handler is not None else None
            gv_client._last_p1_recordings = rec
        except Exception:
            pass
        if handler is not None and keep_warm:
            # Leave STT/audio hot on the worker for the next call.
            try:
                gv_client._p1_handler_warm = True
            except Exception:
                pass
        else:
            try:
                if getattr(gv_client, "_p1_handler", None) is handler:
                    gv_client._p1_handler = None
            except Exception:
                pass
            if handler is not None:
                try:
                    await asyncio.wait_for(handler.stop(), timeout=8)
                except Exception:
                    try:
                        handler._listening = False
                        handler.audio.stop()
                    except Exception:
                        pass


ClaimNext = Callable[[], Awaitable[Optional[str]]]


async def _run_p1_workers(
    pool,
    claim_next: ClaimNext,
    script_profile: str,
    on_event: Callable,
    should_stop: Callable[[], bool],
    connect_timeout: int,
    ivr_timeout: int,
    requeue: Optional[Callable[[str], Awaitable[None]]] = None,
    script_bundle: Optional[dict] = None,
) -> dict:
    """Drive N pool workers from a shared claim_next() source."""
    counts: dict = {}
    done = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal done
        while not should_stop():
            phone = await claim_next()
            if phone is None:
                return
            if should_stop():
                return
            reset_diag()
            _last_call_recordings.set(None)
            try:
                gv_client = await pool.acquire()
            except asyncio.CancelledError:
                # Hard-stop while waiting for a worker — the claimed number
                # was never dialed; put it back before propagating.
                if requeue is not None:
                    try:
                        await requeue(phone)
                    except Exception:
                        pass
                raise
            if not gv_client:
                _diag_set(
                    stage="acquire",
                    why="no_ready_worker(pool.acquire timeout)",
                )
                logger.warning(
                    f"p1 worker got NO free worker for {_mask_phone(phone)} "
                    f"(pool.acquire timeout — workers busy/unhealthy)"
                )
                if requeue is not None:
                    # The number was never dialed — putting it back instead of
                    # burning it as a false 'failed'. A degraded pool (spam
                    # check running, accounts died) then just slows the run
                    # down instead of destroying it.
                    try:
                        await requeue(phone)
                        continue
                    except Exception as e:
                        logger.warning(
                            f"p1 requeue failed for {_mask_phone(phone)}: {e}"
                        )
                res = "failed"
            else:
                drop_recordings = False
                try:
                    res = await execute_one(
                        gv_client, phone, script_profile,
                        connect_timeout, ivr_timeout,
                        should_stop=should_stop,
                        script_bundle=script_bundle,
                    )
                except asyncio.CancelledError:
                    drop_recordings = True
                    res = "cancelled"
                    _diag_set(why="worker_task_cancelled_outer")
                    logger.warning(
                        f"p1 worker task CANCELLED mid-call: "
                        f"phone={_mask_phone(phone)} "
                        f"(outer cancel — agent hard-stop/shutdown)"
                    )
                    try:
                        await _safe_release(pool, gv_client)
                    except Exception:
                        pass
                    raise
                except Exception as e:
                    logger.error(f"p1 worker {_mask_phone(phone)} failed: {e}")
                    _diag_set(why=f"worker_exception:{type(e).__name__}")
                    res = "failed"
                finally:
                    rec = _take_client_recordings(gv_client)
                    if drop_recordings and rec:
                        from audio.call_recorder import CallRecorder
                        CallRecorder.delete_paths(rec)
                        rec = None
                    _last_call_recordings.set(rec)
                    await _safe_release(pool, gv_client)
            async with lock:
                counts[res] = counts.get(res, 0) + 1
                done += 1
            try:
                await on_event(phone, res)
            except Exception:
                pass
            finally:
                leftover = take_last_call_recordings()
                if leftover:
                    from audio.call_recorder import CallRecorder
                    CallRecorder.delete_paths(leftover)

    n_workers = max(1, len(getattr(pool, "workers", []) or [1]))
    tasks = [asyncio.create_task(worker()) for _ in range(n_workers)]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return {"counts": counts, "done": done}


async def run_p1_batch(
    pool,
    numbers: List[str],
    script_profile: str,
    on_event: Callable,
    should_stop: Callable[[], bool],
    connect_timeout: int = 30,
    ivr_timeout: int = 120,
    script_bundle: Optional[dict] = None,
) -> dict:
    """Run a bulk P1 queue on one agent (local list, standalone-style).

    on_event(phone, result) is awaited after each call (for live progress).
    should_stop() is checked before taking each number.
    Returns {"counts": {result: n}, "done": n}.
    """
    queue: asyncio.Queue = asyncio.Queue()
    for ph in numbers:
        queue.put_nowait(ph)

    async def claim_next() -> Optional[str]:
        if should_stop():
            return None
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def requeue(phone: str) -> None:
        queue.put_nowait(phone)

    report = await _run_p1_workers(
        pool, claim_next, script_profile, on_event,
        should_stop, connect_timeout, ivr_timeout,
        requeue=requeue,
        script_bundle=script_bundle,
    )

    if should_stop():
        remaining = queue.qsize()
        if remaining:
            report["counts"]["cancelled"] = (
                report["counts"].get("cancelled", 0) + remaining
            )
            report["done"] += remaining

    return report


async def run_p1_cluster(
    pool,
    bus,
    run_id: str,
    script_profile: str,
    on_event: Callable,
    should_stop: Callable[[], bool],
    connect_timeout: int = 30,
    ivr_timeout: int = 120,
    claim_block_ms: int = 3000,
    script_bundle: Optional[dict] = None,
) -> dict:
    """Pull phones from a Redis shared queue until empty or stopped.

    All pool workers compete for the next number cluster-wide (via BRPOP).
    Fast agents naturally process more calls than slow ones.
    """

    # Only give up on the run after the queue is *confirmed* empty this many
    # consecutive times. A single transient Redis error (len == -1) never counts,
    # so a network blip can't make workers quit a run with numbers still queued.
    empty_confirmations = 0
    required_empty = 3

    async def claim_next() -> Optional[str]:
        nonlocal empty_confirmations
        while not should_stop():
            if await bus.is_p1_stopped(run_id):
                return None
            phone = await bus.claim_p1_number(run_id, block_ms=claim_block_ms)
            if phone:
                empty_confirmations = 0
                return phone

            qlen = await bus.p1_queue_length(run_id)
            if qlen == 0:
                # Confirmed empty — require a few in a row to tolerate a race
                # between BRPOP timeout and a concurrent RPUSH/agent.
                empty_confirmations += 1
                if empty_confirmations >= required_empty:
                    return None
                await asyncio.sleep(0.5)
                continue
            # qlen < 0 → transport error (unknown). qlen > 0 → work remains.
            # Either way: keep trying, never exit. Back off on error.
            empty_confirmations = 0
            if qlen < 0:
                await asyncio.sleep(1.5)
        return None

    async def requeue(phone: str) -> None:
        await bus.requeue_p1_number(run_id, phone)

    return await _run_p1_workers(
        pool, claim_next, script_profile, on_event,
        should_stop, connect_timeout, ivr_timeout,
        requeue=requeue,
        script_bundle=script_bundle,
    )
