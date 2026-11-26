"""
Controller-side distributed /p1 and /p1stop.

All numbers go into one Redis work queue; every agent pulls the next phone
when a local GV worker is free (BRPOP). Fast agents naturally take more calls.

Session stats live in Redis so /p1live works for any user, not just the initiator.

Standalone /p1 is untouched (call.py keeps its own local path).
"""
import asyncio
import os
import time
import uuid

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.commands.p1_dashboard import (
    _LABEL,
    _esc,
    completion_keyboard,
    format_completion_notice,
    format_dashboard,
    format_final_summary,
    format_copy_line,
    new_session,
    record_agent_done,
    record_progress,
)


async def _sync_session(bus, state: dict) -> None:
    session = state.get("session")
    if not session or not bus:
        return
    try:
        session["queue_remaining"] = max(
            0, session.get("total", 0) - session.get("done", 0)
        )
        await bus.save_p1_session(session)
    except Exception as e:
        logger.debug(f"p1 session sync failed: {e}")


async def _p1_result_loop(bot, bus, state: dict):
    """Background task: consume results from agents, update Telegram status."""
    group = state["group"]
    run_id = state["run_id"]
    pending = state["pending"]
    chat_id = state["chat_id"]
    status_message_id = state["status_message_id"]
    total = state["total"]
    session = state["session"]
    timeout = state["timeout"]
    profile = session.get("script_profile", "suspicious")
    deadline = time.time() + timeout
    last_edit = 0.0
    last_sync = 0.0

    # ── Stall / orphan-number protection ────────────────────────────────────
    # A run must never hang for the full `timeout` (default 1h) just because an
    # agent quit early (network blip) or died mid-call leaving numbers unclaimed.
    STALL_SEC = int(os.getenv("GV_P1_STALL_SEC", "180"))
    REDISPATCH_COOLDOWN = int(os.getenv("GV_P1_REDISPATCH_COOLDOWN_SEC", "45"))
    MAX_REDISPATCH_ROUNDS = int(os.getenv("GV_P1_MAX_REDISPATCH_ROUNDS", "20"))
    now0 = time.time()
    started_at = session.get("started_at", now0)
    last_progress_at = now0
    last_qcheck = 0.0
    last_redispatch_at = now0
    redispatch_rounds = 0
    empty_confirms = 0
    end_reason = "unknown"
    last_msg_at = now0
    # After the last p1_progress the agent still needs seconds to publish the
    # recording (b64-encoding a capped ~6MB wav is not instant). Breaking the
    # moment done>=total stranded the FINAL call's p1_audio in the Redis
    # group — progress arrived first, the loop exited, and the audio message
    # sat unacked forever (why timeout recordings never reached the chat while
    # small voicemail ones, published ~0.2s after progress, did).
    TRAILING_QUIET_SEC = 10   # exit once the stream has been this quiet
    TRAILING_HARD_SEC = 60    # absolute cap on the trailing drain
    # Diagnostics: when each agent emitted its FIRST cancelled/failed result.
    # This is the key signal for "why cancelled" — it tells us whether an agent
    # started cancelling on its own (agent bug) or only after /p1stop (expected).
    first_bad_at: dict = {}

    def _mask(ph: str) -> str:
        ph = str(ph or "")
        return (ph[:5] + "…" + ph[-2:]) if len(ph) > 8 else ph

    async def _online_agent_ids() -> set:
        try:
            return {
                a["agent_id"] for a in await bus.list_agents()
                if a.get("role") == "agent"
            }
        except Exception as e:
            logger.debug(f"p1 online-agent lookup failed: {e}")
            return set()

    all_done_at: float | None = None
    try:
        while state["running"] and time.time() < deadline:
            if state.get("done", 0) >= total:
                # All calls reported — drain trailing p1_audio uploads (see
                # the comment at TRAILING_QUIET_SEC) before exiting.
                if all_done_at is None:
                    all_done_at = time.time()
                if (time.time() - last_msg_at > TRAILING_QUIET_SEC
                        or time.time() - all_done_at > TRAILING_HARD_SEC):
                    end_reason = "all_done"
                    break
            msgs = await bus.consume_results(group, block_ms=2000)
            if msgs:
                last_msg_at = time.time()
            changed = False
            for entry_id, r in msgs:
                try:
                    await bus.ack_result(group, entry_id)
                    if r.get("run_id") != run_id:
                        continue
                    aid = r.get("agent_id", "?")
                    kind = r.get("kind")
                    if kind == "p1_progress":
                        res = r.get("result")
                        phone = r.get("phone", "")
                        record_progress(session, aid, res, phone)
                        state["done"] = session["done"]
                        last_progress_at = time.time()
                        changed = True
                        # ── Diagnose cancelled/failed/timeout as they arrive ──
                        if res in ("cancelled", "failed", "timeout"):
                            since = time.time() - started_at
                            stop_state = (
                                "AFTER /p1stop" if state.get("was_stopped")
                                else "run active (NO stop sent)"
                            )
                            detail = r.get("detail")
                            dtxt = f" detail={detail}" if detail else ""
                            if aid not in first_bad_at:
                                first_bad_at[aid] = time.time()
                                logger.warning(
                                    f"p1 {run_id}: FIRST '{res}' from agent {aid} "
                                    f"at +{since:.0f}s [{stop_state}] phone={_mask(phone)}{dtxt}"
                                )
                            else:
                                logger.info(
                                    f"p1 {run_id}: '{res}' from {aid} "
                                    f"at +{since:.0f}s [{stop_state}] phone={_mask(phone)}{dtxt}"
                                )
                        if res in ("1", "2"):
                            try:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        f"🚨 {_LABEL[res]}\n"
                                        f"{format_copy_line(session, phone)}\n"
                                        f"server <code>{aid}</code>"
                                    ),
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                        if res == "account_banned":
                            # The agent heard Google's account-restricted
                            # intercept mid-call and is already wiping the
                            # profile — surface WHICH account burned.
                            try:
                                email = r.get("email") or "?"
                                gvnum = r.get("gv_number") or "?"
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        "🚫 <b>GV account BANNED</b>\n"
                                        f"account <code>{_esc(email)}</code>"
                                        f" ({_esc(gvnum)})\n"
                                        f"server <code>{aid}</code> · "
                                        f"номер {_esc(phone)}\n"
                                        "Услышали «unable to complete your "
                                        "call» — cookies wiped, слот ждёт "
                                        "/login"
                                    ),
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                    elif kind == "p1_audio":
                        try:
                            from bot.p1_audio import send_p1_audio_from_bus
                            await send_p1_audio_from_bus(bot, chat_id, r)
                        except Exception as e:
                            logger.debug(f"p1_audio send failed: {e}")
                    elif kind == "p1_backend":
                        warn = r.get("warning")
                        if warn:
                            try:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=f"⚠️ {aid}: {warn}",
                                )
                            except Exception:
                                pass
                    elif kind == "p1_done":
                        pending.discard(aid)
                        record_agent_done(session, aid)
                        # Backend honesty check: if we asked for SIP but the
                        # agent didn't confirm it (old agent code ignores the
                        # param silently), say so once per agent.
                        if session.get("backend") == "sip" \
                                and r.get("backend") != "sip":
                            warned = state.setdefault("_backend_warned", set())
                            if aid not in warned:
                                warned.add(aid)
                                try:
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text=(
                                            f"⚠️ {aid}: запрошен 🌐 SIP, но звонки "
                                            "пошли через 🖥 Chrome — агент не "
                                            "подтвердил SIP (старый код на агенте?)"
                                        ),
                                    )
                                except Exception:
                                    pass
                        # Per-agent completion breakdown — shows exactly what one
                        # server produced (incl. its own cancelled count) so we
                        # can tell a misbehaving agent from an expected stop.
                        acounts = session.get("agents", {}).get(aid, {}).get("counts", {})
                        logger.info(
                            f"p1 {run_id}: agent {aid} reported DONE "
                            f"counts={acounts} stopped={r.get('stopped')} "
                            f"error={r.get('error')!r} (pending now: {sorted(pending)})"
                        )
                        if r.get("error"):
                            try:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=f"⚠️ {aid}: {r['error']}",
                                )
                            except Exception:
                                pass
                        changed = True
                except Exception as e:
                    logger.warning(f"p1 result handling error: {e}")

            now = time.time()
            if changed and now - last_sync > 1.0:
                last_sync = now
                await _sync_session(bus, state)

            if changed and (now - last_edit) > 4 and status_message_id:
                last_edit = now
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_message_id,
                        text=format_dashboard(session, "overview"),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # ── Queue-drain completion + orphan re-dispatch ──────────────────
            # Only runs when not manually stopped. Checked at most every ~2s.
            if not state.get("was_stopped") and (now - last_qcheck) >= 2.0:
                last_qcheck = now
                qlen = await bus.p1_queue_length(run_id)  # -1 on transient error

                if qlen == 0:
                    # Queue drained. Finish once agents have also reported done,
                    # or after a short grace so a dead-mid-call agent can't hang us.
                    empty_confirms += 1
                    drained = (not pending) or (now - last_progress_at) > STALL_SEC
                    if empty_confirms >= 2 and drained:
                        logger.info(
                            f"p1 run {run_id}: queue drained "
                            f"(done={state.get('done', 0)}/{total}, pending={len(pending)}) — finishing"
                        )
                        end_reason = "queue_drained"
                        break
                else:
                    empty_confirms = 0

                if qlen > 0 and (now - last_redispatch_at) >= REDISPATCH_COOLDOWN:
                    # Numbers remain but progress stalled or an agent went idle.
                    # Re-arm every online agent so a blip-quitter resumes pulling.
                    stalled = (not pending) or (now - last_progress_at) > STALL_SEC
                    if stalled and redispatch_rounds < MAX_REDISPATCH_ROUNDS:
                        online = await _online_agent_ids()
                        if online:
                            redispatch_rounds += 1
                            last_redispatch_at = now
                            for aid in online:
                                try:
                                    await bus.dispatch_job(aid, {
                                        "type": "p1",
                                        "params": {
                                            "run_id": run_id,
                                            "script_profile": profile,
                                            "script_bundle": session.get("script_bundle"),
                                            "backend": session.get("backend"),
                                        },
                                    })
                                    pending.add(aid)
                                except Exception as e:
                                    logger.debug(f"re-dispatch to {aid} failed: {e}")
                            logger.warning(
                                f"p1 run {run_id}: {qlen} number(s) unclaimed, "
                                f"progress stalled {now - last_progress_at:.0f}s — "
                                f"re-dispatched to {len(online)} agent(s) "
                                f"(round {redispatch_rounds}/{MAX_REDISPATCH_ROUNDS})"
                            )
                        elif not pending:
                            # No agents online and none pending → nobody can finish.
                            logger.warning(
                                f"p1 run {run_id}: {qlen} number(s) left but no agents online — finishing"
                            )
                            end_reason = "no_agents_left"
                            break
                    elif redispatch_rounds >= MAX_REDISPATCH_ROUNDS and not pending:
                        logger.warning(
                            f"p1 run {run_id}: giving up after {redispatch_rounds} "
                            f"re-dispatch rounds with {qlen} number(s) unclaimed"
                        )
                        end_reason = "gave_up_redispatch"
                        break
    except asyncio.CancelledError:
        logger.info("p1 result loop cancelled")
    except Exception:
        logger.exception("p1 result loop crashed")
    finally:
        try:
            await bus.cleanup_p1_run(run_id)
        except Exception:
            pass
        try:
            await bus.destroy_results_group(group)
        except Exception:
            pass

    # Resolve why the loop ended (for the diagnostic summary below).
    if end_reason == "unknown":
        if not state.get("running"):
            end_reason = "stopped" if state.get("was_stopped") else "loop_halted"
        elif time.time() >= deadline:
            end_reason = "timeout"

    session["running"] = False
    done_count = session.get("done", 0)

    # ── Cancelled breakdown (the answer to "why cancelled") ──────────────────
    # agent_cancelled : calls an agent actively returned as 'cancelled'
    #                   (should_stop hit mid-call → almost always after /p1stop)
    # leftover        : numbers never dialed at all (queue not drained) — these
    #                   are ALSO shown as 'cancelled' in the report but are NOT
    #                   an agent fault; they're just work that never started.
    per_agent_counts = {
        aid: st.get("counts", {})
        for aid, st in session.get("agents", {}).items()
    }
    agent_cancelled = {
        aid: c.get("cancelled", 0)
        for aid, c in per_agent_counts.items()
        if c.get("cancelled", 0)
    }
    leftover = max(0, total - done_count)
    logger.warning(
        f"p1 {run_id} FINISHED: reason={end_reason} was_stopped={state.get('was_stopped')} "
        f"done={done_count}/{total} leftover(never-dialed)={leftover} "
        f"agent_reported_cancelled={agent_cancelled or 'none'} "
        f"stopped_at={state.get('stopped_at')} started_at={started_at:.0f} "
        f"global_counts={session.get('counts', {})}"
    )
    logger.info(f"p1 {run_id} per-agent counts: {per_agent_counts}")

    if done_count < total:
        if leftover:
            session.setdefault("counts", {})["cancelled"] = (
                session["counts"].get("cancelled", 0) + leftover
            )
        session["was_stopped"] = state.get("was_stopped", False)

    state["running"] = False
    await bus.finish_p1_session(session)

    final = format_final_summary(session)
    keyboard = completion_keyboard(session)
    try:
        if status_message_id:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=final,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=chat_id, text=final, parse_mode="HTML",
            )
    except Exception:
        try:
            await bot.send_message(
                chat_id=chat_id, text=final, parse_mode="HTML",
            )
        except Exception:
            pass

    notice = format_completion_notice(session)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=notice,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logger.debug("p1 completion notice send failed")


BACKEND_LABELS = {"sip": "🌐 SIP", "manual": "🖥 Manual (Chrome)"}


class _UpdateShim:
    """query.message exposed as effective_message/effective_chat."""
    __slots__ = ("_update", "message")

    def __init__(self, update, message):
        object.__setattr__(self, "_update", update)
        object.__setattr__(self, "message", message)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_update"), name)

    @property
    def effective_message(self):
        return object.__getattribute__(self, "message")

    @property
    def effective_chat(self):
        return object.__getattribute__(self, "message").chat


async def p1_backend_pick_handler(update, context):
    """Inline 🌐 SIP / 🖥 Manual choice made — launch the pending run."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    backend = query.data.split(":", 1)[1]
    pending = context.bot_data.pop("p1_backend_pending", None)
    if not pending:
        await query.edit_message_text(
            "⚠️ Launch request expired — start the campaign again.")
        return
    bus = context.bot_data.get("bus")
    if bus is None:
        await query.edit_message_text("⚠️ Controller bus unavailable.")
        return
    label = BACKEND_LABELS.get(backend, backend)
    numbers = pending["numbers"]
    await query.edit_message_text(
        f"{label} — dispatching {len(numbers)} number(s) to agents…")
    shim = _UpdateShim(update, query.message)
    await p1_distributed(
        shim, context, bus, numbers, pending["script_profile"],
        line_map=pending.get("line_map"),
        campaign_name=pending.get("campaign_name"),
        backend=backend,
    )


async def p1_distributed(
    update, context, bus, numbers, script_profile,
    line_map=None, campaign_name=None, backend=None,
):
    """Seed a shared queue and dispatch all agents as pull workers."""
    msg = update.effective_message

    if backend is None:
        # Temporary while SIP is new: ask which call backend to drive.
        context.bot_data["p1_backend_pending"] = {
            "numbers": numbers,
            "script_profile": script_profile,
            "line_map": line_map,
            "campaign_name": campaign_name,
        }
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🌐 SIP", callback_data="p1backend:sip"),
            InlineKeyboardButton("🖥 Manual", callback_data="p1backend:manual"),
        ]])
        name = campaign_name or "Campaign"
        await msg.reply_text(
            f"📞 <b>{name}</b> — <b>{len(numbers)}</b> number(s).\n"
            "Choose the call backend:",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    try:
        agents = [a for a in await bus.list_agents()
                  if a.get("role") == "agent"]
    except Exception as e:
        await msg.reply_text(f"❌ Redis error: {e}")
        return
    if not agents:
        await msg.reply_text("⚠️ No agents online.")
        return

    existing = context.bot_data.get("p1_dist")
    if existing and existing.get("running"):
        await msg.reply_text(
            "⚠️ A P1 batch is already running. Use /p1stop first.",
        )
        return

    active = await bus.get_p1_active_session()
    if active and active.get("running"):
        await msg.reply_text(
            "⚠️ A P1 session is already active. Use /p1stop first.",
        )
        return

    run_id = uuid.uuid4().hex[:12]
    try:
        await bus.create_p1_run(run_id, numbers)
    except Exception as e:
        await msg.reply_text(f"❌ Failed to create work queue: {e}")
        return

    from storage.script_templates import bundle_for
    script_bundle = context.bot_data.get("script_bundle") or bundle_for(script_profile)

    aids = [a["agent_id"] for a in agents]
    timeout = int(os.getenv("GV_P1_TIMEOUT_SEC",
                            os.getenv("GV_JOB_TIMEOUT_SEC", "3600")))
    group = await bus.new_results_group()
    pending = set()
    job_params = {
        "run_id": run_id,
        "script_profile": script_profile,
        "script_bundle": script_bundle,
        "backend": backend,
    }
    for aid in aids:
        await bus.dispatch_job(aid, {
            "type": "p1",
            "params": job_params,
        })
        pending.add(aid)

    total = len(numbers)
    session = new_session(
        run_id, total, script_profile, aids,
        line_map=line_map, campaign_name=campaign_name,
    )
    session["script_bundle"] = script_bundle
    session["script_label"] = (script_bundle or {}).get("label") or script_profile
    session["backend"] = backend

    status_msg = await msg.reply_text(
        format_dashboard(session, "overview"),
        parse_mode="HTML",
    )

    from bot.commands.p1_live import live_keyboard
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            reply_markup=live_keyboard("overview", auto=False),
        )
    except Exception:
        pass

    state = {
        "run_id": run_id,
        "group": group,
        "pending": pending,
        "session": session,
        "chat_id": update.effective_chat.id,
        "status_message_id": status_msg.message_id if status_msg else None,
        "total": total,
        "done": 0,
        "running": True,
        "timeout": timeout,
    }
    await bus.save_p1_session(session)

    task = asyncio.create_task(
        _p1_result_loop(context.bot, bus, state),
        name="p1_dist_results",
    )
    state["task"] = task
    context.bot_data["p1_dist"] = state


async def p1stop_distributed(update, context, bus):
    """Stop the shared run, broadcast p1stop, and break the result loop."""
    msg = update.effective_message

    # Who/what triggered the stop — critical for explaining 'cancelled'.
    user = getattr(update, "effective_user", None)
    who = "unknown"
    if user is not None:
        who = f"{user.id}/@{user.username}" if user.username else str(user.id)

    st = context.bot_data.get("p1_dist")
    run_id = (st or {}).get("run_id")
    if not run_id:
        active = await bus.get_p1_active_session()
        run_id = (active or {}).get("run_id")

    logger.warning(
        f"/p1stop TRIGGERED by user={who} chat={getattr(update.effective_chat, 'id', '?')} "
        f"run_id={run_id} (this is the cause of any 'cancelled' that follows)"
    )
    if st is not None:
        st["stopped_at"] = time.time()

    if run_id:
        try:
            await bus.set_p1_stop(run_id)
        except Exception as e:
            logger.warning(f"set_p1_stop failed: {e}")

    try:
        agents = [a for a in await bus.list_agents()
                  if a.get("role") == "agent"]
    except Exception as e:
        await msg.reply_text(f"❌ Redis error: {e}")
        return
    if not agents:
        await msg.reply_text("⚠️ No agents online.")
        return
    for a in agents:
        await bus.dispatch_job(a["agent_id"], {"type": "p1stop"})
    logger.warning(
        f"/p1stop broadcast to {len(agents)} agent(s): "
        f"{[a.get('agent_id') for a in agents]}"
    )

    if st and st.get("running"):
        st["was_stopped"] = True
        st["running"] = False
        if st.get("session"):
            st["session"]["was_stopped"] = True
        task = st.get("task")
        if task and not task.done():
            await asyncio.sleep(3)
            if not task.done():
                task.cancel()

    await msg.reply_text(
        f"🛑 <b>P1 stop</b> sent to {len(agents)} agent(s).\n"
        f"Workers will finish active calls and halt.",
        parse_mode="HTML",
    )


async def flushall_command(update, context):
    """/flushall — nuclear Redis reset on the controller.

    Wipes EVERYTHING in the Redis DB: stuck p1 sessions/queues, the agent
    registry (agents re-register on their next heartbeat) and the remote
    settings hash (Deepgram keys, P1_SEND_ALL_RECORDINGS — must be re-set).
    Also drops the controller's local p1 state, so a 'session already
    active' ghost can never survive this command.
    """
    msg = update.effective_message
    bus = context.bot_data.get("bus")
    if bus is None:
        await msg.reply_text("⚠️ Standalone mode — no Redis to flush.")
        return

    st = context.bot_data.pop("p1_dist", None)
    if st is not None:
        st["running"] = False
        task = st.get("task")
        if task and not task.done():
            task.cancel()

    try:
        r = await bus.connect()
        await r.flushall()
    except Exception as e:
        await msg.reply_text(f"❌ FLUSHALL failed: {e}")
        return

    logger.warning("/flushall executed — Redis DB wiped (settings hash included)")
    await msg.reply_text(
        "🧨 <b>Redis FLUSHALL done.</b>\n\n"
        "Wiped: stuck p1 sessions, work queues, agent registry, remote settings.\n"
        "• Agents come back online on their heartbeat (~30s).\n"
        "• ⚙️ Settings (Deepgram keys, 📡 P1_SEND_ALL_RECORDINGS) were wiped too "
        "— re-apply them in the bot.",
        parse_mode="HTML",
    )
