"""
Cross-profile spam probe.

Каждый профиль звонит на GV-номер другого профиля. Трубку НИКТО не берёт —
после короткого гудка звонящий сбрасывает. Затем у профиля-получателя
проверяется история звонков на пометку 'Suspected spam'.

Пары формируются из готовых воркеров пула: (W0,W1), (W2,W3), ...
Режимы: parallel (все пары одновременно) | sequential (по очереди).

При debug=True пишется подробный файл spam_debug_<время>.log в корень
проекта и снимаются скриншоты+HTML звонящего на каждом ключевом шаге.
"""
import asyncio
import time
import uuid
from pathlib import Path

from loguru import logger

import config


def _make_step_logger(debug: bool):
    """Return (log_fn, path). log_fn(msg) → loguru + (debug) file append."""
    path = None
    if debug:
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = config.BASE_DIR / f"spam_debug_{ts}.log"

    def log_fn(msg: str):
        logger.info(msg)
        if path is not None:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
            except Exception:
                pass

    return log_fn, path


async def _probe_pair(caller, callee, ring_sec: float, debug: bool, log) -> dict:
    """Один прозвон: caller → callee, затем проверка истории у callee."""
    cid = str(uuid.uuid4())[:8]
    caller_num = caller.phone_number
    callee_num = callee.phone_number
    tag = f"W{caller.worker_id}->W{callee.worker_id}"

    steps: list[str] = []

    def step(msg: str):
        line = f"[{tag}] {msg}"
        steps.append(line)
        log(line)

    base = {
        "caller_worker": caller.worker_id,
        "callee_worker": callee.worker_id,
        "caller_number": caller_num,
        "callee_number": callee_num,
        "caller_email": None,
        "verdict": "unknown",
        "error": None,
        "debug": None,
        "receiver_screenshot": None,
        "steps": steps,
    }

    step(
        f"PAIR caller=W{caller.worker_id}({caller_num}) "
        f"callee=W{callee.worker_id}({callee_num}) cid={cid}"
    )

    if not caller_num or not callee_num:
        base["error"] = "missing GV number"
        step("ABORT: missing GV number")
        return base
    if caller_num == callee_num:
        base["error"] = "caller and callee share the same GV number"
        step("ABORT: caller==callee number")
        return base

    try:
        from console_ui import dashboard
        dashboard.update_worker(
            caller.worker_id,
            status=f"Calling W{callee.worker_id}",
            stt=f"{caller_num} -> {callee_num}",
        )
    except Exception:
        pass

    async def _caller_debug(dtag: str):
        if not debug:
            return None
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, lambda: caller._dump_debug_sync(callee_num, f"caller_{dtag}")
            )
            step(f"caller dump [{dtag}]: {res}")
            return res
        except Exception as e:
            step(f"caller dump [{dtag}] FAILED: {e}")
            return None

    try:
        step("calling make_call()...")
        dialed = await caller.make_call(callee_num, cid)
        step(f"make_call returned {dialed}")
        # Always capture what the caller's screen looks like right after dial.
        after = await _caller_debug("afterdial")
        base["debug"] = after

        if not dialed:
            base["error"] = "dial failed (make_call returned False)"
            return base

        # make_call возвращает True оптимистично — проверяем реально.
        loop = asyncio.get_running_loop()
        call_went_out = False
        last_status = None
        verify_deadline = loop.time() + 12.0
        while loop.time() < verify_deadline:
            status = await loop.run_in_executor(None, caller.check_call_status)
            if status != last_status:
                step(f"caller status={status}")
                last_status = status
            if status in ("ringing", "connected", "active"):
                call_went_out = True
                step(f"CALL CONFIRMED (status={status})")
                break
            await asyncio.sleep(0.5)

        if not call_went_out:
            base["debug"] = await _caller_debug("notplaced") or after
            try:
                await caller.hangup(cid)
            except Exception:
                pass
            try:
                from console_ui import dashboard
                dashboard.update_worker(
                    caller.worker_id, status="GV Ready ✅",
                    stt="spam-probe: call not placed",
                )
            except Exception:
                pass
            base["error"] = (
                f"call not placed (caller never reached ringing; "
                f"last status={last_status})"
            )
            step(f"RESULT: {base['error']}")
            return base

        step(f"ringing for {ring_sec:.0f}s (no answer)...")
        await asyncio.sleep(ring_sec)
        try:
            await caller.hangup(cid)
        except Exception:
            pass
        step("hung up; checking callee history for spam...")

        # Inspect the RECEIVER's (callee) Google Voice for the spam mark
        # on the CALLER's number.
        verdict = await callee.check_incoming_spam(caller_num, debug=debug)
        email = await caller.fetch_account_email()

        base["verdict"] = verdict
        base["caller_email"] = email
        # Screenshot is of the RECEIVER's GV page (always captured).
        callee_dump = getattr(callee, "_last_spam_debug", None)
        if callee_dump:
            base["debug"] = callee_dump
            base["receiver_screenshot"] = callee_dump.get("screenshot")
        if verdict == "absent":
            base["error"] = (
                f"call did not appear in receiver history "
                f"W{callee.worker_id}({callee_num})"
            )
        step(
            f"RESULT verdict={verdict} email={email} "
            f"receiver=W{callee.worker_id}({callee_num})"
        )
        return base

    except Exception as e:
        step(f"EXCEPTION: {e}")
        logger.exception(f"spam probe pair {tag} failed")
        try:
            await caller.hangup(cid)
        except Exception:
            pass
        base["error"] = str(e)
        return base


async def run_spam_probe(
    pool,
    mode: str = "parallel",
    ring_sec: float = 8.0,
    max_pairs=None,
    debug: bool = False,
) -> dict:
    """Берёт готовые воркеры из пула, разбивает на пары, прозванивает.

    Возвращает {"pairs", "results", "error", "debug_log"}.
    """
    log, log_path = _make_step_logger(debug)
    log(f"=== run_spam_probe mode={mode} ring={ring_sec} max_pairs={max_pairs} ===")

    workers = []
    snap = pool.get_status_snapshot()
    ready_with_num = sum(
        1
        for w in snap["workers"]
        if w["state"] == "ready" and w.get("phone_number")
    )
    log(f"pool snapshot: ready_with_number={ready_with_num} "
        f"total={snap.get('total_workers')}")

    for _ in range(max(0, ready_with_num)):
        w = await pool.acquire(timeout=5)
        if not w:
            log("acquire() returned None — stopping acquisition")
            break
        if w.phone_number:
            workers.append(w)
            log(f"acquired W{w.worker_id} number={w.phone_number}")
        else:
            await pool.release(w)
            log(f"released W{getattr(w,'worker_id','?')} (no number)")

    if len(workers) < 2:
        for w in workers:
            await pool.release(w)
        return {
            "pairs": 0,
            "results": [],
            "error": "at least 2 ready workers with a GV number are required",
            "debug_log": str(log_path) if log_path else None,
        }

    # Split into pairs; the last worker is a "leftover" if the count is odd.
    pairs = [
        (workers[i], workers[i + 1])
        for i in range(0, len(workers) - 1, 2)
    ]
    leftover = workers[-1] if len(workers) % 2 == 1 else None
    if max_pairs:
        pairs = pairs[:max_pairs]
        leftover = None  # limiting pairs disables the odd-helper logic

    log("pairs: " + ", ".join(
        f"(W{a.worker_id}<->W{b.worker_id})" for a, b in pairs
    ) + (f"  leftover: W{leftover.worker_id}" if leftover else ""))

    results: list = []
    results_lock = asyncio.Lock()
    # Workers freed by a finished pair — used to help the odd leftover.
    freed: asyncio.Queue = asyncio.Queue()

    async def _record(r: dict):
        async with results_lock:
            results.append(r)

    async def run_pair(a, b):
        """Both directions: a calls b, then b calls a (checks BOTH numbers)."""
        log(f"PAIR START W{a.worker_id}<->W{b.worker_id}")
        await _record(await _probe_pair(a, b, ring_sec, debug, log))
        await _record(await _probe_pair(b, a, ring_sec, debug, log))
        log(f"PAIR DONE W{a.worker_id}<->W{b.worker_id} — workers freed")
        # These workers are now idle — offer them to help the leftover.
        freed.put_nowait(a)
        freed.put_nowait(b)

    async def run_leftover(L):
        """Odd worker: wait for any freed worker, then L calls it (checks L)."""
        log(f"LEFTOVER W{L.worker_id} waiting for a freed helper...")
        helper = await freed.get()
        log(f"LEFTOVER W{L.worker_id} got helper W{helper.worker_id}")
        await _record(await _probe_pair(L, helper, ring_sec, debug, log))
        log(f"LEFTOVER W{L.worker_id} checked")

    try:
        tasks = [run_pair(a, b) for a, b in pairs]
        if leftover is not None:
            tasks.append(run_leftover(leftover))

        if mode == "parallel":
            await asyncio.gather(*tasks)
        else:
            # Sequential: run pairs one by one. Leftover still needs a freed
            # worker, so run it concurrently with the (sequential) pairs.
            lo_task = None
            if leftover is not None:
                lo_task = asyncio.create_task(run_leftover(leftover))
            for a, b in pairs:
                await run_pair(a, b)
            if lo_task is not None:
                await lo_task

        # ── Auto-disable spam-flagged workers ─────────────────────────
        # Identify CALLER workers whose number was flagged as spam. Their
        # cookies are cleared (the profile itself is kept) and they leave the
        # pool, so a burned number is never used again.
        spam_workers: dict = {}  # worker_id -> result dict
        for r in results:
            if r.get("verdict") != "spam":
                continue
            cw = r.get("caller_worker")
            if cw is not None and cw not in spam_workers:
                spam_workers[cw] = r

        disabled_workers: list = []
        if spam_workers:
            for w in workers:
                if w.worker_id in spam_workers:
                    sr = spam_workers[w.worker_id]
                    log(
                        f"AUTO-DISABLE W{w.worker_id} "
                        f"({sr.get('caller_number')}) — flagged as spam, "
                        f"clearing cookies"
                    )
                    try:
                        await pool.disable_worker_spam(w)
                        disabled_workers.append({
                            "worker_id": w.worker_id,
                            "number": sr.get("caller_number"),
                            "email": sr.get("caller_email"),
                        })
                    except Exception as e:
                        log(f"WARN: failed to disable W{w.worker_id}: {e}")

        log(f"=== run_spam_probe finished — {len(results)} numbers checked, "
            f"{len(disabled_workers)} workers disabled ===")
        return {
            "pairs": len(pairs),
            "checked": len(results),
            "results": list(results),
            "disabled_workers": disabled_workers,
            "error": None,
            "debug_log": str(log_path) if log_path else None,
        }
    finally:
        # КРИТИЧНО: вернуть все воркеры в пул, иначе они утекут.
        # Spam-wiped workers are needs_login with driver=None — release()
        # parks them for /login instead of putting them back on calls.
        for w in workers:
            await pool.release(w)
