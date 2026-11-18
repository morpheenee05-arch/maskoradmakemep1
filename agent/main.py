"""
Agent node entry point.

Runs the existing telephony PhonePool on this RDP server and publishes a
live capacity heartbeat to the Redis bus so the controller can see and
(Phase 2+) dispatch work to it. No Telegram here.

Run with NODE_ROLE=agent (see .env). Phase 1 = registry + heartbeat only;
job consumption is added in Phase 2.
"""
import asyncio
import base64
import os
import signal
import subprocess
import sys
import time

from loguru import logger

import config
from infra.redis_bus import RedisBus

_MAX_IMG_BYTES = 4 * 1024 * 1024  # skip screenshots larger than this


def _slim_result(r: dict) -> dict:
    """JSON-safe subset of a spam-probe result for transport to controller."""
    return {
        "caller_worker": r.get("caller_worker"),
        "callee_worker": r.get("callee_worker"),
        "caller_number": r.get("caller_number"),
        "callee_number": r.get("callee_number"),
        "caller_email": r.get("caller_email"),
        "verdict": r.get("verdict"),
        "error": r.get("error"),
    }


async def _handle_accounts_job(pool, bus, job: dict):
    """Report this agent's Google account inventory to the controller."""
    job_id = job.get("job_id")
    aid = config.AGENT_ID
    try:
        rows = pool.accounts_report()
    except Exception as e:
        logger.exception(f"[job {job_id}] accounts_report failed")
        rows, err = [], str(e)
    else:
        err = None

    snap = pool.get_status_snapshot()
    await bus.publish_result({
        "job_id": job_id, "agent_id": aid, "kind": "accounts",
        "accounts": rows,
        "error": err,
        "summary": {
            "state": snap["state"],
            "total": snap["total_workers"],
            "ready": snap["ready_workers"],
            "needs_login": snap.get("needs_login_workers", 0),
            "challenge": snap.get("challenge_workers", 0),
            "captcha": snap.get("captcha_workers", 0),
            "logging_in": snap.get("logging_in_workers", 0),
            "credentials_known": snap.get("credentials_known", 0),
        },
    })
    logger.info(f"[job {job_id}] accounts report sent ({len(rows)} worker(s))")


async def _publish_wipe_screenshot(bus, job_id: str, aid: str, path, caption: str):
    import base64
    import os
    from pathlib import Path

    shot = Path(path)
    if not shot.is_file():
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "photo",
            "error": "screenshot file missing",
            "caption": caption,
        })
        return
    if os.path.getsize(shot) > _MAX_IMG_BYTES:
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "photo",
            "error": "screenshot too large",
            "caption": caption,
        })
        return
    with open(shot, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    await bus.publish_result({
        "job_id": job_id, "agent_id": aid, "kind": "photo",
        "caption": caption,
        "image_b64": b64,
        "filename": shot.name,
    })


async def _handle_wipe_history_job(pool, bus, job: dict):
    """Send this agent's local wipe archive (text; optional named/latest photo)."""
    from telephony import wipe_archive

    job_id = job.get("job_id")
    aid = config.AGENT_ID
    params = job.get("params") or {}
    shot_name = str(params.get("screenshot") or "").strip()
    if shot_name:
        path = wipe_archive.screenshot_file(shot_name)
        try:
            if path is None:
                await bus.publish_result({
                    "job_id": job_id, "agent_id": aid, "kind": "photo",
                    "error": f"screenshot not on this agent: {shot_name}",
                    "caption": f"{aid} · {shot_name}",
                })
            else:
                await _publish_wipe_screenshot(
                    bus, job_id, aid, path, f"{aid} · {path.name}",
                )
        except Exception as e:
            logger.warning(f"[job {job_id}] wipe photo failed: {e}")
            await bus.publish_result({
                "job_id": job_id, "agent_id": aid, "kind": "photo",
                "error": str(e),
                "caption": f"{aid} · {shot_name}",
            })
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "done",
        })
        return

    limit = max(1, min(80, int(params.get("limit") or 40)))
    want_photo = bool(params.get("photo"))
    try:
        events = wipe_archive.list_events(limit=limit)
    except Exception as e:
        logger.exception(f"[job {job_id}] wipe history failed")
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "wipe_history",
            "events": [], "error": str(e),
        })
        return
    await bus.publish_result({
        "job_id": job_id, "agent_id": aid, "kind": "wipe_history",
        "events": events, "error": None,
        "dir": str(wipe_archive.archive_dir()),
        "kept": len(events),
    })
    if want_photo:
        for shot in wipe_archive.latest_screenshots(limit=1):
            try:
                await _publish_wipe_screenshot(
                    bus, job_id, aid, shot, f"{aid} · last wipe · {shot.name}",
                )
            except Exception as e:
                logger.warning(f"[job {job_id}] wipe photo failed: {e}")
    await bus.publish_result({
        "job_id": job_id, "agent_id": aid, "kind": "done",
    })
    logger.info(f"[job {job_id}] wipe history sent ({len(events)} event(s))")


async def _handle_sip_test_job(pool, bus, job: dict):
    """SIP backend test: cookies from a live worker Chrome → mint SIP creds →
    REGISTER (+ optional test call), streaming progress to the controller."""
    import json as _json

    job_id = job.get("job_id")
    aid = config.AGENT_ID
    params = job.get("params") or {}
    call_number = str(params.get("call") or "").strip()
    hold = max(5, min(120, int(params.get("hold") or 25)))
    want_worker = params.get("worker")

    async def progress(stage: str, line: str):
        try:
            await bus.publish_result({
                "job_id": job_id, "agent_id": aid, "kind": "sip_progress",
                "stage": stage, "line": line,
            })
        except Exception:
            pass

    stages: dict = {}
    events: list = []
    error = None
    phone = None
    try:
        # ── 1. pick a worker with a live, signed-in Chrome ──
        worker = None
        if want_worker is not None:
            worker = pool._worker_by_selector(worker=int(want_worker))
        else:
            for w in pool.workers:
                if w.driver is not None and w.is_authenticated:
                    worker = w
                    break
        if worker is None or worker.driver is None:
            raise RuntimeError(
                "no signed-in worker with a live Chrome on this agent")
        stages["worker"] = {
            "worker_id": worker.worker_id,
            "email": worker.account_email or worker.credential_email or "",
            "number": worker.phone_number or "",
        }
        await progress(
            "cookies",
            f"using W{worker.worker_id} "
            f"({stages['worker']['email'] or 'email unknown'})",
        )

        # ── 2. harvest SAPISID-family cookies from the live Chrome ──
        # CDP Storage.getCookies sees HttpOnly cookies too (SID/HSID) —
        # document.cookie does not, and the endpoint wants the full set.
        def _harvest():
            drv = worker.driver
            if "voice.google.com" not in (drv.current_url or ""):
                drv.get("https://voice.google.com/u/0/calls")
            deadline = time.time() + 40
            while time.time() < deadline:
                cookies = {}
                try:
                    res = drv.execute_cdp_cmd("Storage.getCookies", {})
                    for c in res.get("cookies", []):
                        if "google.com" in (c.get("domain") or ""):
                            cookies[c["name"]] = {
                                "value": c.get("value", ""),
                                "domain": c.get("domain", ""),
                            }
                except Exception:
                    cookies = {}
                if not cookies:
                    raw = drv.execute_script("return document.cookie") or ""
                    cookies = {
                        p.strip().split("=", 1)[0]: p.strip().split("=", 1)[1]
                        for p in raw.split(";") if "=" in p
                    }
                if any(k in cookies for k in (
                        "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")):
                    return cookies
                time.sleep(2)
            raise RuntimeError(
                "no SAPISID-family cookies on voice.google.com — "
                "session expired? (re-login this worker)")

        cookies = await asyncio.to_thread(_harvest)
        found = [k for k in ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")
                 if k in cookies]
        stages["cookies"] = {"found": found}
        await progress("cookies", f"cookies ok: {', '.join(found)}")

        # ── 3. mint per-session SIP credentials ──
        # Primary path: let the browser make the call itself — we hook XHR
        # via CDP addScriptToEvaluateOnNewDocument (fires in the gapi proxy
        # iframe too), reload the page and capture request headers + response
        # body of the real sipregisterinfo exchange. This also tells us the
        # exact SAPISIDHASH origin so the pure-Python path can be fixed.
        from telephony.sip.gv_auth import (
            VoiceAPIClient, parse_sip_credentials,
        )
        from telephony.sip.sniff import SIP_HOOK_JS, diagnose_auth_origin

        def _sniff():
            drv = worker.driver
            drv.execute_cdp_cmd("Page.enable", {})
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                {"source": SIP_HOOK_JS})
            drv.get("https://voice.google.com/u/0/calls")
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    cap = drv.execute_script(
                        "return window.__sipCredCapture || null")
                except Exception:
                    cap = None
                if cap and cap.get("body"):
                    return cap
                time.sleep(2)
            return None

        creds = None
        captured = await asyncio.to_thread(_sniff)
        if captured:
            await progress("mint", "sniffed live sipregisterinfo exchange")
            match = diagnose_auth_origin(
                captured.get("reqHeaders", {}).get("authorization") or
                captured.get("reqHeaders", {}).get("Authorization") or "",
                cookies,
            )
            if match:
                await progress("mint", f"SAPISIDHASH origin proven: {match}")
                stages["auth_origin"] = match
            try:
                creds = parse_sip_credentials(_json.loads(captured["body"]))
                stages["mint"] = {
                    "ok": True, "source": "in-page sniff",
                    "username": creds.username,
                    "password_len": len(creds.password),
                    "raw": captured["body"][:900],
                    "req_body": captured.get("reqBody", "")[:200],
                }
            except Exception as e:
                stages["mint"] = {"ok": False, "source": "in-page sniff",
                                  "error": str(e),
                                  "raw": captured.get("body", "")[:900]}
                raise RuntimeError(f"sniffed response parse failed: {e}")
        else:
            await progress(
                "mint", "in-page sniff timed out — falling back to direct API")
            api = VoiceAPIClient(cookies)
            try:
                creds = await asyncio.to_thread(api.get_sip_register_info)
            except Exception as e:
                stages["mint"] = {"ok": False, "source": "direct API",
                                  "error": str(e)}
                raise
            stages["mint"] = {
                "ok": True, "source": "direct API",
                "username": creds.username,
                "password_len": len(creds.password),
                "raw": _json.dumps(creds.raw)[:900],
            }
        await progress(
            "mint",
            f"SIP creds: {creds.username} (password len {len(creds.password)})",
        )

        # ── 4. REGISTER on the SIP WebSocket ──
        from telephony.sip.sip_ws import GVWebPhone

        def on_event(kind: str, info: dict):
            slim = {k: str(v)[:160] for k, v in info.items() if k != "sdp"}
            events.append({"kind": kind, **slim})
            asyncio.ensure_future(progress("sip", f"event {kind} {slim}"))

        phone = GVWebPhone(creds, on_event=on_event)
        await asyncio.wait_for(phone.connect(), timeout=20)
        try:
            await asyncio.wait_for(phone.register(), timeout=30)
        except Exception as e:
            stages["register"] = {"ok": False, "error": str(e)}
            raise RuntimeError(f"REGISTER failed: {e}")
        stages["register"] = {"ok": True, "expires": phone._reg_expires}
        await progress("register", "REGISTER 200 OK — signaling works")

        # ── 5. optional test call (signaling only, template SDP) ──
        if call_number:
            from telephony.sip.templates import TEMPLATE_SDP

            # ── 5a. mint X-GV-PlaceCallContext via the live page ──
            # The page's own JS (encryptPlaceCallContext) builds this blob;
            # without it the callee sees "Unknown". We arm a WS send hook,
            # let the page build its INVITE for this number, capture the
            # header and swallow the page's frame (a synthetic 603 settles
            # the page stack, no real call is placed by the browser).
            from telephony.sip.sniff import PCC_DIAL_JS, PCC_HOOK_JS

            def _mint_pcc():
                drv = worker.driver
                drv.execute_script(PCC_HOOK_JS)
                drv.execute_script(
                    "window.__pccArm = true; window.__pccCapture = null;")
                try:
                    res = drv.execute_async_script(PCC_DIAL_JS, call_number)
                finally:
                    drv.execute_script("window.__pccArm = false;")
                return res

            pcc = None
            try:
                res = await asyncio.to_thread(_mint_pcc)
                if res and res.get("ok"):
                    cap = await asyncio.to_thread(
                        lambda: worker.driver.execute_script(
                            "return window.__pccCapture || null"))
                    if cap and cap.get("header"):
                        pcc = cap["header"]
                if pcc:
                    stages["pcc"] = {"ok": True, "len": len(pcc)}
                    await progress(
                        "call", f"PlaceCallContext minted ({len(pcc)} chars)")
                else:
                    detail = "; ".join((res or {}).get("log") or [])[:300]
                    stages["pcc"] = {"ok": False, "log": detail}
                    await progress(
                        "call", f"PlaceCallContext mint failed: {detail}")
            except Exception as e:
                stages["pcc"] = {"ok": False, "error": str(e)[:300]}
                await progress("call", f"PlaceCallContext mint error: {e}")
            if pcc:
                phone.place_call_context = pcc

            finished = asyncio.Event()

            def on_call_event(kind: str, info: dict):
                if kind in ("bye", "failed", "transport_lost"):
                    finished.set()

            phone.on_event = lambda k, i: (on_event(k, i), on_call_event(k, i))
            await phone.start_call(call_number, TEMPLATE_SDP)
            await progress("call", f"INVITE sent to {call_number}")
            try:
                await asyncio.wait_for(finished.wait(), timeout=hold)
            except asyncio.TimeoutError:
                pass
            try:
                await phone.hangup()
            except Exception:
                pass
            await asyncio.sleep(1.5)
            verdict = next(
                (e["kind"] for e in reversed(events)
                 if e["kind"] in ("answered", "failed", "bye")),
                "no-answer",
            )
            stages["call"] = {
                "number": call_number,
                "verdict": verdict,
                "flow": [e["kind"] for e in events
                         if e["kind"] in ("calling", "ringing", "early_media",
                                          "answered", "failed", "bye")],
            }
            await progress("call", f"call verdict: {verdict}")

    except Exception as e:
        error = str(e)
        logger.exception(f"[job {job_id}] sip_test failed")
    finally:
        if phone is not None:
            try:
                await phone.close()
            except Exception:
                pass
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "sip_done",
            "stages": stages, "events": events[-40:], "error": error,
        })
        logger.info(f"[job {job_id}] sip_test done (error={error})")


async def _handle_login_job(pool, bus, job: dict):
    """Apply operator credentials and sign the matching profiles in.

    The job payload carries plaintext passwords; it is acked (and deleted from
    the stream) by the job loop as soon as this returns, and nothing here logs
    a password.
    """
    job_id = job.get("job_id")
    aid = config.AGENT_ID
    p = job.get("params", {})
    creds = p.get("credentials") or []
    concurrency = p.get("concurrency")
    worker_ids = p.get("worker_ids")

    logger.info(
        f"[job {job_id}] login: {len(creds)} credential(s) received, "
        f"concurrency={concurrency or 'default'}"
    )

    try:
        applied = pool.apply_credentials(creds)
    except Exception as e:
        logger.exception(f"[job {job_id}] applying credentials failed")
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "login_done",
            "results": [], "unassigned": [], "error": str(e),
        })
        return

    targets = worker_ids or [row["worker_id"] for row in applied["assigned"]]
    if not targets:
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "login_done",
            "results": [], "unassigned": applied["unassigned"],
            "error": "no profile on this agent matched the credentials",
        })
        return

    try:
        async def _progress(rows):
            await bus.publish_result({
                "job_id": job_id, "agent_id": aid, "kind": "login_progress",
                "results": rows,
            })

        rows = await pool.auto_login_workers(
            targets, concurrency=concurrency, on_progress=_progress,
        )
        err = None
    except Exception as e:
        logger.exception(f"[job {job_id}] auto-login crashed")
        rows, err = [], str(e)

    await bus.publish_result({
        "job_id": job_id, "agent_id": aid, "kind": "login_done",
        "results": rows, "unassigned": applied["unassigned"], "error": err,
    })
    ok = sum(1 for r in rows if r.get("ok"))
    logger.info(f"[job {job_id}] login finished — {ok}/{len(rows)} signed in")


async def _handle_spamcheck_job(pool, bus, job: dict):
    """Run the local spam-probe and stream the result back to the controller."""
    from telephony.spam_probe import run_spam_probe

    job_id = job.get("job_id")
    aid = config.AGENT_ID
    params = job.get("params", {})
    mode = params.get("mode", "parallel")
    ring_sec = float(params.get("ring_sec", 8.0))
    max_pairs = params.get("max_pairs")
    debug = bool(params.get("debug", False))

    logger.info(f"[job {job_id}] spamcheck start (mode={mode} ring={ring_sec})")
    try:
        report = await run_spam_probe(
            pool, mode=mode, ring_sec=ring_sec,
            max_pairs=max_pairs, debug=debug,
        )
    except Exception as e:
        logger.exception(f"[job {job_id}] run_spam_probe crashed")
        await bus.publish_result({
            "job_id": job_id, "agent_id": aid, "kind": "report",
            "summary": {"error": f"agent crashed: {e}", "results": []},
        })
        await bus.publish_result(
            {"job_id": job_id, "agent_id": aid, "kind": "done"}
        )
        return

    results = report.get("results", [])
    await bus.publish_result({
        "job_id": job_id, "agent_id": aid, "kind": "report",
        "summary": {
            "pairs": report.get("pairs"),
            "checked": report.get("checked", len(results)),
            "error": report.get("error"),
            "results": [_slim_result(r) for r in results],
            "disabled_workers": report.get("disabled_workers") or [],
        },
    })

    # Screenshots: only for FLAGGED numbers by default (all of them in
    # debug) — one photo per account was flooding the chat.
    for r in results:
        if not debug and r.get("verdict") != "spam":
            continue
        shot = r.get("receiver_screenshot")
        if not shot or not os.path.exists(shot):
            continue
        try:
            if os.path.getsize(shot) > _MAX_IMG_BYTES:
                logger.warning(f"[job {job_id}] screenshot too big, skipped: {shot}")
                continue
            with open(shot, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            await bus.publish_result({
                "job_id": job_id, "agent_id": aid, "kind": "photo",
                "caption": (
                    f"{aid} · receiver W{r.get('callee_worker')}"
                    f"({r.get('callee_number')}) · "
                    f"{r.get('caller_number')}: {r.get('verdict')}"
                ),
                "image_b64": b64,
            })
        except Exception as e:
            logger.warning(f"[job {job_id}] screenshot send failed: {e}")

    await bus.publish_result(
        {"job_id": job_id, "agent_id": aid, "kind": "done"}
    )
    logger.info(f"[job {job_id}] spamcheck done ({len(results)} results)")


_INJECT = {
    "op_success": "The security code just worked. Michael's account is "
                  "secured. Finish the call professionally and warmly.",
    "op_failed": "The system rejected that code. It might be expired. Calmly "
                 "ask the caller to check for the LATEST code and read it back.",
    "op_stall": "I'm just syncing the security tokens on my end Michael, "
                "stay with me—this only takes a second.",
}


async def _handle_call_job(pool, bus, job: dict):
    """Run one interactive AI call locally; bridge events/control via Redis."""
    p = job.get("params", {})
    call_id = p.get("call_id")
    phone = p.get("phone")
    system_prompt = p.get("system_prompt") or "You are a helpful AI assistant."
    greeting = p.get("greeting", "")

    async def ev(d):
        try:
            await bus.publish_call_event(call_id, d)
        except Exception as e:
            logger.debug(f"publish_call_event failed: {e}")

    gv = await pool.acquire()
    if not gv:
        await ev({"t": "error", "error": "no free worker"})
        return

    handler = None
    ps = None
    ctl_task = None
    mon_task = None
    ended = asyncio.Event()
    try:
        from audio.call_handler import CallHandler
        await ev({"t": "status", "status": "🔄 connecting"})
        handler = CallHandler(
            capture_device=gv.capture_device,
            playback_device=gv.playback_device,
        )
        if not await handler.initialize():
            await ev({"t": "error", "error": "audio init failed"})
            return
        if not await handler.prepare(system_prompt, greeting):
            await ev({"t": "error", "error": "audio/Deepgram prepare failed"})
            return

        if not await gv.make_call(phone, call_id):
            await ev({"t": "status", "status": "❌ failed to connect"})
            await ev({"t": "ended", "reason": "failed"})
            return
        await ev({"t": "status", "status": "🔔 ringing"})

        if not await gv.wait_for_connect(call_id, timeout=60):
            await gv.hangup(call_id)
            await ev({"t": "status", "status": "📵 not answered"})
            await ev({"t": "ended", "reason": "not_answered"})
            return

        await ev({"t": "status", "status": "✅ connected"})

        async def on_transcript(speaker, text):
            await bus.publish_call_event(
                call_id, {"t": "transcript", "speaker": speaker, "text": text}
            )

        handler.set_callbacks(on_transcript=on_transcript)
        await handler.start()

        ps = await bus.subscribe(f"gv:call:{call_id}:control")

        async def control_loop():
            while not ended.is_set():
                m = await bus.next_message(ps, timeout=1.0)
                if not m:
                    continue
                act = m.get("action")
                if act == "hangup":
                    ended.set()
                    return
                txt = _INJECT.get(act)
                if txt and getattr(handler, "agent", None):
                    try:
                        await handler.agent.inject_message(txt)
                    except Exception as e:
                        logger.warning(f"inject failed: {e}")

        async def on_end(_cid):
            ended.set()

        ctl_task = asyncio.create_task(control_loop())
        mon_task = asyncio.create_task(gv.monitor_call(call_id, on_end))
        await ended.wait()
    except Exception as e:
        logger.exception(f"call job {call_id} crashed")
        await ev({"t": "error", "error": str(e)})
    finally:
        for _t in (ctl_task, mon_task):
            if _t:
                _t.cancel()
        if handler is not None:
            try:
                await handler.stop()
            except Exception:
                pass
        try:
            await gv.hangup(call_id)
        except Exception:
            pass
        try:
            await pool.release(gv)
        except Exception:
            pass
        if ps is not None:
            try:
                await ps.unsubscribe()
                await ps.aclose()
            except Exception:
                pass
        await ev({"t": "ended", "reason": "cleanup"})
        logger.info(f"call job {call_id} finished")


async def _publish_p1_recordings(bus, job_id, run_id, aid, phone, result, recordings: dict):
    """Stream the mixed call WAV to the controller, then delete the local file."""
    from pathlib import Path
    from audio.call_recorder import CallRecorder, read_recording_b64

    caption = "🎧 Звонок · левый канал абонент, правый наш звук"
    try:
        path = recordings.get("call") or recordings.get("remote") or recordings.get("local")
        if not path:
            return
        b64 = read_recording_b64(Path(path))
        if not b64:
            await bus.publish_result({
                "job_id": job_id, "run_id": run_id, "agent_id": aid,
                "kind": "p1_audio", "phone": phone, "result": result,
                "error": "recording missing or too large",
                "caption": caption,
            })
            return
        await bus.publish_result({
            "job_id": job_id, "run_id": run_id, "agent_id": aid,
            "kind": "p1_audio", "phone": phone, "result": result,
            "audio_b64": b64,
            "filename": Path(path).name, "caption": caption,
        })
    finally:
        CallRecorder.delete_paths(recordings)


async def _handle_p1_job(pool, bus, job: dict, stop_event: asyncio.Event, on_progress=None):
    """Run a distributed /p1 run on this agent's pool, stream progress."""
    job_id = job.get("job_id")
    aid = config.AGENT_ID
    p = job.get("params", {})
    run_id = p.get("run_id")
    numbers = list(p.get("numbers") or [])
    profile = p.get("script_profile", "suspicious")
    script_bundle = p.get("script_bundle")
    connect_timeout = int(p.get("connect_timeout", 30))
    ivr_timeout = int(p.get("ivr_timeout", 120))

    try:
        if run_id:
            from telephony.p1_runner import run_p1_cluster
        else:
            from telephony.p1_runner import run_p1_batch
    except Exception as e:
        logger.exception(f"[job {job_id}] p1: import failed")
        try:
            await bus.publish_result({
                "job_id": job_id, "run_id": run_id, "agent_id": aid,
                "kind": "p1_done", "counts": {}, "done": 0,
                "error": f"agent import failed: {e}",
            })
        except Exception:
            pass
        return

    if run_id:
        logger.info(f"[job {job_id}] p1 cluster join run={run_id}")
    else:
        logger.info(f"[job {job_id}] p1 batch start ({len(numbers)} numbers)")

    async def on_event(phone, result):
        if on_progress is not None:
            try:
                on_progress()
            except Exception:
                pass
        msg = {
            "job_id": job_id, "run_id": run_id, "agent_id": aid,
            "kind": "p1_progress", "phone": phone, "result": result,
        }
        # Attach per-call diagnostics (stage/why/elapsed/stop flag) so the
        # controller log shows exactly WHY a call ended cancelled/failed.
        if result in ("cancelled", "failed", "timeout"):
            try:
                from telephony.p1_runner import get_last_diag
                d = get_last_diag()
                if d:
                    d.pop("t0", None)  # monotonic clock — useless off-host
                    msg["detail"] = d
            except Exception:
                pass
        await bus.publish_result(msg)
        try:
            from telephony.p1_runner import take_last_call_recordings
            from audio.call_recorder import CallRecorder
            rec = take_last_call_recordings()
            if rec:
                import config as _cfg
                send_results = tuple(
                    getattr(_cfg, "P1_SEND_RECORDING_RESULTS", ("1", "2"))
                )
                send = (
                    result in send_results
                    and getattr(_cfg, "P1_SEND_RECORDINGS", True)
                )
                if send:
                    await _publish_p1_recordings(
                        bus, job_id, run_id, aid, phone, result, rec,
                    )
                else:
                    CallRecorder.delete_paths(rec)
        except Exception as e:
            logger.warning(f"[job {job_id}] p1 recording handling failed: {e}")

    try:
        if run_id:
            report = await run_p1_cluster(
                pool, bus, run_id, profile, on_event,
                should_stop=stop_event.is_set,
                connect_timeout=connect_timeout, ivr_timeout=ivr_timeout,
                script_bundle=script_bundle,
            )
        else:
            report = await run_p1_batch(
                pool, numbers, profile, on_event,
                should_stop=stop_event.is_set,
                connect_timeout=connect_timeout, ivr_timeout=ivr_timeout,
                script_bundle=script_bundle,
            )
        await bus.publish_result({
            "job_id": job_id, "run_id": run_id, "agent_id": aid,
            "kind": "p1_done",
            "counts": report.get("counts", {}),
            "done": report.get("done", 0),
            "stopped": stop_event.is_set(),
        })
        logger.info(f"[job {job_id}] p1 done {report.get('counts')}")
    except Exception as e:
        logger.exception(f"[job {job_id}] p1 crashed")
        await bus.publish_result({
            "job_id": job_id, "run_id": run_id, "agent_id": aid,
            "kind": "p1_done", "counts": {}, "done": 0, "error": str(e),
        })


def _kill_chrome_processes():
    """Force-kill leftover chromedriver / headless chrome on shutdown."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/IM", "chromedriver.exe"],
                capture_output=True, check=False,
            )
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/FI", "WINDOWTITLE eq "],
                capture_output=True, check=False,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "chromedriver"],
                capture_output=True, check=False,
            )
            subprocess.run(
                ["pkill", "-f", "chrome.*--headless"],
                capture_output=True, check=False,
            )
    except Exception as e:
        logger.debug(f"Process cleanup error (non-fatal): {e}")


_agent_version_cache = None


def _agent_version() -> str:
    """Git commit this agent runs on (shown in the controller's agent list)."""
    global _agent_version_cache
    if _agent_version_cache is None:
        try:
            from infra.updater import current_version
            _agent_version_cache = current_version()
        except Exception:
            _agent_version_cache = "nogit"
    return _agent_version_cache


def _capacity_payload(pool) -> dict:
    """Compact snapshot the controller needs for routing/aggregation."""
    snap = pool.get_status_snapshot()
    return {
        "version": _agent_version(),
        "state": snap["state"],
        "total_workers": snap["total_workers"],
        "ready_workers": snap["ready_workers"],
        "checked_out_workers": snap["checked_out_workers"],
        "numbers_fetched_workers": snap["numbers_fetched_workers"],
        "restarting_workers": snap.get("restarting_workers", 0),
        "needs_login_workers": snap.get("needs_login_workers", 0),
        "challenge_workers": snap.get("challenge_workers", 0),
        "captcha_workers": snap.get("captcha_workers", 0),
        "logging_in_workers": snap.get("logging_in_workers", 0),
        "oom_events": snap.get("oom_events", 0),
        "browser_restarts": snap.get("browser_restarts", 0),
    }


_last_mem_log_at = 0.0


def _log_memory_report(pool, interval_sec: int = 300):
    """Periodic per-worker memory line, so OOM pressure is visible in the log."""
    global _last_mem_log_at

    rows = [r for r in pool.memory_report() if r.get("rss_mb")]
    if not rows:
        return

    soft = int(getattr(config, "GV_WORKER_RSS_SOFT_MB", 900))
    for r in rows:
        if r["rss_mb"] >= soft:
            logger.warning(
                f"Worker {r['worker_id']}: memory pressure — "
                f"rss={r['rss_mb']}MB heap={r['heap_pct']}% "
                f"calls={r['calls_served']} (soft limit {soft}MB; the tab is "
                f"reloaded between calls once this is hit)"
            )

    now = time.time()
    if now - _last_mem_log_at < interval_sec:
        return
    _last_mem_log_at = now

    from telephony.google_voice import host_free_mb

    total = sum(r["rss_mb"] for r in rows)
    free = host_free_mb()
    free_txt = f"{free:.0f}MB" if free is not None else "unknown"
    detail = " ".join(
        f"w{r['worker_id']}={r['rss_mb']:.0f}MB/{r['calls_served']}c" for r in rows
    )
    logger.info(
        f"Chrome memory: total={total:.0f}MB host_free={free_txt} "
        f"restarts={sum(r['restarts'] for r in rows)} "
        f"oom={sum(r['oom_events'] for r in rows)} | {detail}"
    )


async def _init_pool(pool) -> bool:
    attempts = max(1, config.GV_INIT_RETRY_COUNT)
    delay = max(5, config.GV_INIT_RETRY_DELAY_SEC)
    for attempt in range(1, attempts + 1):
        if await pool.initialize():
            logger.info(f"Agent pool ready (attempt {attempt}/{attempts})")
            return True
        if attempt < attempts:
            logger.warning(
                f"Agent pool init {attempt}/{attempts} failed; retry in {delay}s"
            )
            await asyncio.sleep(delay)
    logger.error("Agent pool init fully failed")
    return False


async def _poll_remote_settings(bus, state: dict):
    """Fetch gv:settings and apply diffs live (incl. the Deepgram key pool).

    Only setting NAMES are logged — values may contain API keys.
    """
    try:
        settings = await bus.get_settings()
    except Exception:
        return
    if settings == state.get("applied"):
        return
    from infra.remote_settings import apply_settings

    old = state.get("applied") or {}
    applied = apply_settings(config, settings)
    changed = sorted(k for k in settings if old.get(k) != settings.get(k))
    removed = sorted(k for k in old if k not in settings)
    if changed or removed:
        line = f"Remote settings applied: {sorted(applied)}"
        if changed:
            line += f" | changed: {changed}"
        if removed:
            line += f" | removed on controller: {removed}"
        logger.info(line)
    state["applied"] = settings


# ── Fleet self-update (git pull + graceful restart on controller signal) ──────

def _update_marker_path():
    return config.BASE_DIR / "storage" / ".update_applied"


def _read_update_marker() -> float:
    try:
        return float(_update_marker_path().read_text().strip())
    except Exception:
        return 0.0


def _write_update_marker(ts: float):
    try:
        _update_marker_path().parent.mkdir(parents=True, exist_ok=True)
        _update_marker_path().write_text(str(ts))
    except Exception as e:
        logger.debug(f"update marker write failed: {e}")


async def _poll_update_signal(bus, update_state: dict, stop_event: asyncio.Event):
    """Apply the controller's fleet-update signal once, then restart."""
    try:
        signal = await bus.get_update_signal()
    except Exception:
        return
    if not signal or not signal.get("ts"):
        return
    if update_state.get("applied_ts") is None:
        update_state["applied_ts"] = _read_update_marker()
    ts = float(signal["ts"])
    if ts <= float(update_state.get("applied_ts") or 0):
        return  # already applied (or attempted) this signal

    from infra.updater import apply_update

    logger.warning(f"Fleet update signal received — pulling latest code…")
    ok, detail = apply_update(str(signal.get("ref") or ""))
    _write_update_marker(ts)  # one attempt per signal, success or not
    update_state["applied_ts"] = ts
    if not ok:
        logger.error(
            f"Fleet update FAILED on this agent: {detail} — staying on old code"
        )
        return
    logger.warning(f"Fleet update applied ({detail}) — restarting agent…")
    update_state["pending"] = True
    stop_event.set()


async def run_agent():
    config.validate_config()

    from telephony.google_voice_pool import PhonePool
    from console_ui import dashboard

    logger.info("=" * 50)
    logger.info(f"AI Caller AGENT starting — id={config.AGENT_ID}")
    logger.info("=" * 50)

    dashboard.start()

    bus = RedisBus()
    if not await bus.ping():
        logger.error("Cannot reach Redis — check REDIS_URL / network / TLS / password")
        dashboard.stop()
        return 1

    # Remote settings from the controller bot (gv:settings). Applied once
    # here, then re-polled by the watchdog so changes land within seconds.
    settings_state: dict = {}
    await _poll_remote_settings(bus, settings_state)

    # Fleet update state: "pending" means the new code is pulled and the
    # finally-block re-execs the agent after graceful shutdown.
    update_state: dict = {"pending": False, "applied_ts": None}

    pool = PhonePool()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    def _request_stop(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _request_stop)
        loop.add_signal_handler(signal.SIGTERM, _request_stop)
    except NotImplementedError:
        # Windows ProactorEventLoop: fall back to signal.signal
        signal.signal(signal.SIGINT, lambda *_: _request_stop())

    # Liveness marker: job_loop bumps this every iteration. If it goes stale the
    # watchdog knows the loop is wedged (not just idle) and restarts it, and the
    # heartbeat advertises the staleness so the controller can route elsewhere.
    liveness = {"job_loop_ts": time.time()}

    # P1 batch runs as a background task so the loop stays responsive to a
    # /p1stop control job (which must interrupt a running batch).
    p1_state = {
        "task": None,
        "stop": asyncio.Event(),
        "run_id": None,
        "started_at": None,
        "progress_at": None,  # last finished call — watchdog liveness signal
    }

    # Auto-login also runs detached: signing N browsers in costs minutes.
    login_state = {"task": None}
    # SIP backend test (signaling) also runs detached — it can hold a test
    # call for up to ~2 minutes and the loop must stay responsive.
    sip_state = {"task": None}
    # Pool init runs detached too, so a /login that arrives while Chrome is
    # still coming up can cancel the pending re-init instead of racing it —
    # that overlap was spawning a second Chrome on the same profile.
    init_state = {"task": None}

    # Heartbeat starts immediately (state=initializing) so the controller
    # sees the box come online before the slow Selenium init finishes.
    async def heartbeat():
        while not stop_event.is_set():
            try:
                payload = _capacity_payload(pool)
                p1_task = p1_state.get("task")
                payload["p1_running"] = bool(p1_task and not p1_task.done())
                payload["job_loop_age"] = round(time.time() - liveness["job_loop_ts"], 1)
                await bus.register_or_heartbeat(payload)
            except Exception as e:
                logger.warning(f"Heartbeat failed (will retry): {e}")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=max(3, config.AGENT_HEARTBEAT_SEC)
                )
            except asyncio.TimeoutError:
                pass

    hb_task = asyncio.create_task(heartbeat(), name="agent_heartbeat")
    call_tasks = {}  # call_id -> task (interactive /call jobs)

    def _log_task_exception(task: asyncio.Task):
        """Surface silent task crashes to ai-caller.log (Rich Live hides stderr)."""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.opt(exception=exc).error(
                f"Background task {task.get_name()} crashed: {exc}"
            )

    async def _hangup_all_active():
        for w in getattr(pool, "workers", []):
            for cid in list(getattr(w, "_active_calls", {})):
                try:
                    await w.hangup(cid)
                except Exception:
                    pass

    async def _force_stop_all_handlers():
        """Kill zombie scripted handlers left after hangup/p1stop."""
        for w in getattr(pool, "workers", []):
            h = getattr(w, "_p1_handler", None)
            if h is None:
                continue
            try:
                await asyncio.wait_for(h.stop(), timeout=5)
            except Exception:
                try:
                    h._listening = False
                    h.audio.stop()
                except Exception:
                    pass
            try:
                w._p1_handler = None
            except Exception:
                pass

    async def _hard_p1_stop(reason: str = "p1stop"):
        """Fully unwind a stuck/running P1 batch so the agent can take new work."""
        _t = p1_state.get("task")
        _started = p1_state.get("started_at") or 0
        logger.warning(
            f"P1 HARD STOP triggered: reason={reason} "
            f"run_id={p1_state.get('run_id')} "
            f"task_alive={bool(_t and not _t.done())} "
            f"task_age={time.time() - _started:.0f}s — "
            f"every in-flight call will now report 'cancelled'"
        )
        p1_state["stop"].set()
        await _hangup_all_active()
        await _force_stop_all_handlers()
        t = p1_state.get("task")
        if t is not None and not t.done():
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        p1_state["task"] = None
        p1_state["run_id"] = None
        p1_state["started_at"] = None
        try:
            await pool.force_release_checked_out(reason)
        except Exception as e:
            logger.warning(f"force_release after {reason}: {e}")
        logger.info(f"{reason}: P1 hard-stop complete — agent ready for new jobs")

    async def job_loop():
        logger.info(f"Agent job loop started (stream {config.AGENT_ID})")
        while not stop_event.is_set():
            liveness["job_loop_ts"] = time.time()
            try:
                jobs = await bus.consume_jobs(config.AGENT_ID, block_ms=5000)
            except Exception as e:
                logger.warning(f"consume_jobs failed (retry): {e}")
                await asyncio.sleep(3)
                continue
            for entry_id, job in jobs:
                if stop_event.is_set():
                    break
                jtype = job.get("type")
                jid = job.get("job_id")

                # ── Drop STALE jobs replayed from the stream backlog ──────────
                # The job consumer group is created at id=0 (so a freshly-online
                # agent never loses a job dispatched just before its group
                # existed). The side effect: after a crash/restart, undelivered
                # or reclaimed (xautoclaim) entries from a DEAD run get replayed.
                # A p1stop carries no run_id and hard-stops whatever p1 is live,
                # so a replayed old p1/p1stop would cancel the CURRENT run the
                # instant the agent comes back — exactly the "restart → all
                # cancelled" symptom. Anything older than the age budget belongs
                # to a run that is long over: ack-drop it, never execute it.
                job_ts = 0.0
                try:
                    job_ts = float(job.get("ts") or 0)
                except (TypeError, ValueError):
                    job_ts = 0.0
                max_age = max(30, int(getattr(config, "GV_JOB_MAX_AGE_SEC", 90)))
                age = time.time() - job_ts if job_ts else 0.0
                if job_ts and age > max_age:
                    logger.warning(
                        f"Dropping STALE job {jid} type={jtype} "
                        f"(age {age:.0f}s > {max_age}s) — belongs to a dead run, "
                        f"not executing (prevents phantom cancel on restart)"
                    )
                    try:
                        await bus.ack_job(config.AGENT_ID, entry_id)
                    except Exception:
                        pass
                    continue

                logger.info(f"Got job {jid} type={jtype} age={age:.0f}s")
                try:
                    if jtype == "spamcheck":
                        await _handle_spamcheck_job(pool, bus, job)
                    elif jtype == "accounts":
                        await _handle_accounts_job(pool, bus, job)
                    elif jtype == "wipe_history":
                        await _handle_wipe_history_job(pool, bus, job)
                    elif jtype == "login":
                        # A pending pool re-init would respawn Chrome right on
                        # top of the sign-in we are about to run — cancel it.
                        it = init_state.get("task")
                        if it and not it.done():
                            it.cancel()
                            logger.info(
                                f"[job {jid}] cancelled pending pool re-init "
                                f"to make room for sign-in"
                            )
                        # Signing several browsers in takes minutes, so it runs
                        # detached: the loop must stay free for /p1stop.
                        lt = login_state.get("task")
                        if lt and not lt.done():
                            await bus.publish_result({
                                "job_id": jid, "agent_id": config.AGENT_ID,
                                "kind": "login_done", "results": [],
                                "unassigned": [],
                                "error": "agent is already signing profiles in",
                            })
                        else:
                            login_state["task"] = asyncio.create_task(
                                _handle_login_job(pool, bus, job),
                                name=f"login_{jid}",
                            )
                            login_state["task"].add_done_callback(
                                _log_task_exception
                            )
                    elif jtype == "p1":
                        t = p1_state["task"]
                        run_id = job.get("params", {}).get("run_id")
                        if t and not t.done():
                            # If previous p1 is zombie-stuck, self-heal then accept.
                            # Judge by the last finished call, not the job
                            # start — a healthy big run easily outlives the
                            # budget and must NOT be hard-stopped.
                            started = (
                                p1_state.get("progress_at")
                                or p1_state.get("started_at")
                                or 0
                            )
                            stuck_lim = max(
                                120,
                                int(getattr(config, "GV_WORKER_STUCK_SEC", 240)) * 2,
                            )
                            if started and (time.time() - started) > stuck_lim:
                                logger.warning(
                                    f"P1 task stuck >{stuck_lim}s — hard-stopping before new job"
                                )
                                await _hard_p1_stop("p1_stuck_before_new")
                            elif run_id and run_id == p1_state.get("run_id"):
                                logger.info(
                                    f"Already on p1 run {run_id}, ignoring duplicate job"
                                )
                                continue
                            else:
                                await bus.publish_result({
                                    "job_id": jid, "run_id": run_id,
                                    "agent_id": config.AGENT_ID,
                                    "kind": "p1_done", "counts": {}, "done": 0,
                                    "error": "agent busy with another p1 run",
                                })
                                continue
                        p1_state["run_id"] = run_id
                        p1_state["stop"].clear()
                        p1_state["started_at"] = time.time()
                        p1_state["progress_at"] = time.time()
                        logger.info(
                            f"p1 run {run_id} accepted — stop flag cleared, "
                            f"starting batch task"
                        )
                        p1_state["task"] = asyncio.create_task(
                            _handle_p1_job(
                                pool, bus, job, p1_state["stop"],
                                on_progress=lambda: p1_state.__setitem__(
                                    "progress_at", time.time()
                                ),
                            ),
                            name=f"p1_{jid}",
                        )
                        p1_state["task"].add_done_callback(
                            _log_task_exception
                        )

                        def _clear_p1(_t, st=p1_state):
                            st["run_id"] = None
                            st["started_at"] = None
                            st["progress_at"] = None
                            if st.get("task") is _t:
                                st["task"] = None

                        p1_state["task"].add_done_callback(_clear_p1)
                    elif jtype == "sip_test":
                        # Runs up to ~2 min (call hold) — detach like login so
                        # the loop stays free for p1stop & co.
                        st = sip_state.get("task")
                        if st and not st.done():
                            await bus.publish_result({
                                "job_id": jid, "agent_id": config.AGENT_ID,
                                "kind": "sip_done", "stages": {}, "events": [],
                                "error": "a SIP test is already running on this agent",
                            })
                        else:
                            sip_state["task"] = asyncio.create_task(
                                _handle_sip_test_job(pool, bus, job),
                                name=f"siptest_{jid}",
                            )
                            sip_state["task"].add_done_callback(
                                _log_task_exception
                            )
                    elif jtype == "p1stop":
                        await _hard_p1_stop("p1stop")
                    elif jtype == "call":
                        cid = job.get("params", {}).get("call_id", jid)
                        for k, t in list(call_tasks.items()):
                            if t.done():
                                call_tasks.pop(k, None)
                        call_tasks[cid] = asyncio.create_task(
                            _handle_call_job(pool, bus, job),
                            name=f"call_{cid}",
                        )
                        call_tasks[cid].add_done_callback(_log_task_exception)
                    else:
                        logger.warning(f"Unknown job type: {jtype}")
                except Exception:
                    logger.exception(f"Job {jid} handler crashed")
                finally:
                    await bus.ack_job(config.AGENT_ID, entry_id)

    async def watchdog_loop():
        """Periodic self-heal: reclaim stuck workers, kill zombie p1 tasks,
        and resurrect the job/heartbeat loops if they ever die or wedge."""
        nonlocal job_task, hb_task
        interval = max(10, int(getattr(config, "AGENT_WATCHDOG_SEC", 20)))
        logger.info(f"Agent watchdog started (every {interval}s)")
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

            # ── Resurrect dead background loops ──────────────────────────────
            # A silent crash here is exactly how an agent ends up "online per
            # heartbeat" yet never pulling jobs. Restart whatever died.
            if not stop_event.is_set():
                if job_task is None or job_task.done():
                    logger.error("Watchdog: job loop is dead — restarting it")
                    job_task = asyncio.create_task(job_loop(), name="agent_job_loop")
                    job_task.add_done_callback(_log_task_exception)
                    liveness["job_loop_ts"] = time.time()
                if hb_task is None or hb_task.done():
                    logger.error("Watchdog: heartbeat loop is dead — restarting it")
                    hb_task = asyncio.create_task(heartbeat(), name="agent_heartbeat")
                    hb_task.add_done_callback(_log_task_exception)

            try:
                n = await pool.reclaim_stuck_workers()
                if n:
                    logger.warning(f"Watchdog reclaimed {n} stuck worker(s)")
            except Exception as e:
                logger.debug(f"Watchdog reclaim error: {e}")

            # Bring back workers whose Chrome died (OOM-killed renderer, crashed
            # tab). Without this a run slowly bleeds capacity until a restart.
            try:
                revived = pool.revive_failed_workers()
                if revived:
                    logger.warning(
                        f"Watchdog: scheduled browser restart for {revived} "
                        f"dead worker(s)"
                    )
            except Exception as e:
                logger.debug(f"Watchdog revive error: {e}")

            # A Google session that expired mid-run is signed back in, provided
            # the operator already gave us that account's password.
            try:
                lt = login_state.get("task")
                if not (lt and not lt.done()):
                    signed = await pool.retry_pending_logins()
                    if signed:
                        logger.warning(
                            f"Watchdog: signed {signed} worker(s) back in"
                        )
            except Exception as e:
                logger.debug(f"Watchdog login retry error: {e}")

            try:
                _log_memory_report(pool)
            except Exception as e:
                logger.debug(f"Watchdog memory report error: {e}")

            # Controller-pushed settings (bot ⚙️ Settings screen).
            try:
                await _poll_remote_settings(bus, settings_state)
            except Exception as e:
                logger.debug(f"Watchdog settings poll error: {e}")

            # Controller-pushed fleet update (bot 📦 Fleet update button).
            try:
                await _poll_update_signal(bus, update_state, stop_event)
            except Exception as e:
                logger.debug(f"Watchdog update poll error: {e}")
            try:
                t = p1_state.get("task")
                # Hard-stop only when the run shows NO progress: a healthy
                # cluster run can legitimately work for tens of minutes, so
                # wall-clock since job acceptance is not a stuck signal.
                started = (
                    p1_state.get("progress_at")
                    or p1_state.get("started_at")
                )
                if t is not None and not t.done() and started:
                    age = time.time() - started
                    # Extreme wall-clock: far beyond a healthy multi-worker batch pause.
                    hard_p1 = max(
                        600,
                        int(getattr(config, "GV_WORKER_STUCK_SEC", 240)) * 4,
                    )
                    if age > hard_p1:
                        logger.warning(
                            f"Watchdog: p1 task no progress for {age:.0f}s "
                            f"(>{hard_p1}s) — hard stop"
                        )
                        await _hard_p1_stop("watchdog_p1_timeout")
            except Exception as e:
                logger.debug(f"Watchdog p1 check error: {e}")

    job_task = asyncio.create_task(job_loop(), name="agent_job_loop")
    wd_task = asyncio.create_task(watchdog_loop(), name="agent_watchdog")
    init_state["task"] = asyncio.create_task(_init_pool(pool), name="agent_init_pool")
    init_state["task"].add_done_callback(_log_task_exception)

    try:
        await stop_event.wait()
    finally:
        logger.info("Agent shutting down…")
        p1_state["stop"].set()
        hb_task.cancel()
        job_task.cancel()
        wd_task.cancel()
        _it = init_state.get("task")
        if _it:
            _it.cancel()
        _p1t = p1_state.get("task")
        if _p1t:
            _p1t.cancel()
        _lgt = login_state.get("task")
        if _lgt:
            _lgt.cancel()
        for _ct in list(call_tasks.values()):
            _ct.cancel()
        try:
            await _force_stop_all_handlers()
        except Exception:
            pass
        for _t in [hb_task, job_task, wd_task, _it, _p1t, _lgt, *call_tasks.values()]:
            if _t is None:
                continue
            try:
                await _t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await bus.unregister()
        except Exception:
            pass
        await bus.close()
        try:
            pool.shutdown()
        except Exception as e:
            logger.error(f"Pool shutdown error: {e}")
        _kill_chrome_processes()
        dashboard.stop()
        logger.info("Agent shutdown complete")
        if update_state.get("pending"):
            from infra.updater import restart_self
            restart_self()  # never returns
    return 0


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        return asyncio.run(run_agent())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        _kill_chrome_processes()
        return 0


if __name__ == "__main__":
    sys.exit(main())
