"""
🧪 SIP backend test — run signaling tests on a chosen agent and watch live.

Flow (Telegram inline UI):
  🧪 SIP test → pick an online agent →
      🔑 Creds + REGISTER   — harvest cookies from the agent's live Chrome,
                              mint SIP creds, REGISTER on the SIP WebSocket
      📞 Test call          — same + INVITE with a template SDP to a number
                              you type in chat (signaling only, no media yet)

While the agent works it streams sip_progress results over the Redis bus;
this module renders them as a live-updating checklist + log tail, then a
final verdict card. Routed from menu.py (camp:sip…).
"""
import asyncio
import html
import os
import time

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop


def _esc(v) -> str:
    return html.escape(str(v if v is not None else "—"))


def _back(to: str):
    return [InlineKeyboardButton("🔙 Back", callback_data=to)]


# ── screens ──────────────────────────────────────────────────────────────────

async def sip_home_payload(context) -> tuple:
    bus = context.bot_data.get("bus")
    agents = []
    if bus is not None:
        try:
            agents = [a for a in await bus.list_agents()
                      if a.get("role") == "agent"]
        except Exception as e:
            logger.warning(f"sip: list_agents failed: {e}")

    lines = [
        "🧪 <b>SIP backend test</b>",
        "",
        "Runs the new SIP signaling path on an agent: cookies → SIP "
        "credentials → REGISTER (+ optional test call). No media yet — this "
        "validates that Google accepts our SIP client.",
        "",
    ]
    rows = []
    if not agents:
        lines.append("⚠️ No agents online.")
    else:
        lines.append("Pick an agent:")
        for a in agents:
            aid = a.get("agent_id", "?")
            ready = a.get("ready_workers", "?")
            total = a.get("total_workers", "?")
            state = a.get("state", "?")
            icon = "🟢" if state == "ready" else "🟡"
            rows.append([InlineKeyboardButton(
                f"{icon} {aid} — {ready}/{total} ready",
                callback_data=f"camp:sip:ag:{aid}",
            )])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="camp:sip")])
    rows.append(_back("camp:home"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def sip_agent_payload(context, aid: str) -> tuple:
    lines = [
        f"🧪 <b>SIP test — {_esc(aid)}</b>",
        "",
        "The test uses the first signed-in worker's live Chrome — make sure "
        "the agent is idle (no campaign running).",
        "",
        "Choose what to run:",
    ]
    rows = [
        [InlineKeyboardButton(
            "🔑 Creds + REGISTER (no call)",
            callback_data=f"camp:sip:run:{aid}",
        )],
        [InlineKeyboardButton(
            "📞 Test call — type a number",
            callback_data=f"camp:sip:num:{aid}",
        )],
        _back("camp:sip"),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def sip_number_prompt_payload(aid: str) -> tuple:
    lines = [
        f"📞 <b>Test call from {_esc(aid)}</b>",
        "",
        "Send the phone number to call (E.164, e.g. <code>+14036139786</code>).",
        "The call uses a static SDP template — signaling only, no audio. "
        "It is hung up automatically after ~25s.",
    ]
    rows = [[InlineKeyboardButton("❌ Cancel", callback_data=f"camp:sip:cancel")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── rendering ────────────────────────────────────────────────────────────────

_STAGE_ORDER = ("worker", "cookies", "mint", "register", "pcc", "call")
_STAGE_TITLE = {
    "worker": "Worker",
    "cookies": "Cookies",
    "mint": "SIP credentials",
    "register": "REGISTER",
    "pcc": "Caller-ID context",
    "call": "Test call",
}


def _stage_line(name: str, data) -> str:
    title = _STAGE_TITLE.get(name, name)
    if data is None:
        return f"⏳ {title}"
    if name == "worker":
        num = _esc(data.get("number") or "")
        return (f"✅ Worker: W{data.get('worker_id')} "
                f"({_esc(data.get('email') or '?')})"
                + (f" · <code>{num}</code>" if num else ""))
    if name == "cookies":
        return f"✅ Cookies: {_esc(', '.join(data.get('found') or []))}"
    if name == "mint":
        if not data.get("ok"):
            return f"❌ SIP credentials: {_esc(data.get('error', 'failed'))}"
        return (f"✅ SIP credentials: <code>{_esc(data.get('username'))}</code> "
                f"(password len {data.get('password_len')})")
    if name == "register":
        if not data.get("ok"):
            return f"❌ REGISTER: {_esc(data.get('error', 'failed'))}"
        return f"✅ REGISTER 200 OK (expires {data.get('expires', '?')}s)"
    if name == "pcc":
        if not data.get("ok"):
            detail = data.get("error") or data.get("log") or "failed"
            return f"⚠️ Caller-ID context: {_esc(detail[:120])}"
        return f"✅ Caller-ID context: {data.get('len', '?')} chars"
    if name == "call":
        verdict = data.get("verdict", "?")
        icon = {"answered": "✅", "failed": "❌", "bye": "📴"}.get(verdict, "⚠️")
        flow = " → ".join(data.get("flow") or []) or "—"
        return (f"{icon} Test call <code>{_esc(data.get('number'))}</code>: "
                f"{verdict}\n   └ {_esc(flow)}")
    return f"• {title}: {_esc(data)}"


def _render_live(aid: str, stages: dict, log_lines: list,
                 done: dict = None, error: str = "") -> str:
    lines = [f"🧪 <b>SIP test — {_esc(aid)}</b>", ""]
    for name in _STAGE_ORDER:
        if name in stages or name in ("worker", "cookies", "mint",
                                      "register", "pcc"):
            lines.append(_stage_line(name, stages.get(name)))
    if error:
        lines += ["", f"❌ <b>Error:</b> {_esc(error)}"]
    if done is not None and done.get("error"):
        lines += ["", f"❌ <b>Agent error:</b> {_esc(done['error'])}"]
    if done is None:
        last = _esc(log_lines[-1]) if log_lines else "waiting for the agent…"
        lines += ["", f"<i>Running… {last}</i>"]
    else:
        lines += ["", f"📄 Full log: {len(log_lines)} lines — attached as .log"]
    return "\n".join(lines)


def _build_log_file(aid: str, jid: str, call: str, stages: dict,
                    log_lines: list, done, error: str) -> str:
    """Assemble the complete run log shipped to chat as a .log document."""
    import datetime
    head = [
        f"SIP backend test log",
        f"agent:   {aid}",
        f"job:     {jid}",
        f"time:    {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"call:    {call or '(no call — register only)'}",
        "",
        "── stages ──",
    ]
    import re as _re
    ordered = [n for n in _STAGE_ORDER if n in stages]
    ordered += [n for n in stages if n not in _STAGE_ORDER]
    for name in ordered:
        data = stages[name]
        if name in _STAGE_TITLE or name == "worker":
            head.append(_re.sub(r"<[^>]+>", "", _stage_line(name, data)))
        else:
            head.append(f"{name}: {data}")
    body = ["", "── live progress ──"] + (log_lines or ["(none)"])
    events = ["", "── events ──"]
    if done:
        for e in done.get("events") or []:
            slim = {k: v for k, v in e.items() if k != "kind"}
            events.append(f"event {e.get('kind')} {slim if slim else ''}")
        if done.get("error"):
            events.append(f"AGENT ERROR: {done['error']}")
    else:
        events.append(f"NO RESULT: {error or 'timeout'}")
    raw = (stages.get("mint") or {}).get("raw")
    tail = ["", "── raw sipregisterinfo ──", raw] if raw else []
    return "\n".join(str(x) for x in head + body + events + tail)


# ── run & collect ────────────────────────────────────────────────────────────

async def run_sip_test(message, context, aid: str, call: str = ""):
    """Dispatch the sip_test job and live-render progress into `message`."""
    bus = context.bot_data.get("bus")
    if bus is None:
        await message.reply_text("⚠️ Redis bus is not available.")
        return

    params = {"hold": 20}
    if call:
        params["call"] = call
    try:
        jid = await bus.dispatch_job(aid, {"type": "sip_test", "params": params})
    except Exception as e:
        await message.reply_text(f"❌ Dispatch failed: {_esc(e)}", parse_mode="HTML")
        return

    timeout = int(os.getenv("SIP_TEST_TIMEOUT_SEC", "90" if call else "60"))
    started = time.time()
    status = await message.reply_text(
        _render_live(aid, {}, [f"job dispatched → {aid}"]),
        parse_mode="HTML",
    )

    group = await bus.new_results_group()
    stages: dict = {}
    log_lines: list = []
    done = None
    deadline = started + timeout
    last_edit = time.time()
    try:
        while time.time() < deadline:
            msgs = await bus.consume_results(group, block_ms=2000)
            for entry_id, msg in msgs:
                try:
                    await bus.ack_result(group, entry_id)
                    if msg.get("job_id") != jid:
                        continue
                    kind = msg.get("kind")
                    if kind == "sip_progress":
                        log_lines.append(msg.get("line", ""))
                    elif kind == "sip_done":
                        done = msg
                except Exception as e:
                    logger.warning(f"sip result handling: {e}")
            if done is not None:
                break
            now = time.time()
            # Heartbeat edit: on fresh logs, or every ~6s of silence so the
            # chat never looks frozen.
            if (msgs and now - last_edit > 1.0) or (now - last_edit > 6.0):
                last_edit = now
                elapsed = int(now - started)
                try:
                    await status.edit_text(
                        _render_live(aid, stages,
                                     log_lines + [f"… {elapsed}s / {timeout}s"]),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    finally:
        await bus.destroy_results_group(group)

    if done is None:
        try:
            await status.edit_text(
                _render_live(aid, stages, log_lines,
                             error=f"no reply within {timeout}s — agent busy "
                                   "or offline?"),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([_back("camp:sip")]),
            )
        except Exception:
            pass
        await _send_log(status, aid, jid, call, stages, log_lines, None,
                        f"timeout after {timeout}s")
        return

    stages = done.get("stages") or {}
    for e in done.get("events") or []:
        slim = {k: v for k, v in e.items() if k != "kind"}
        log_lines.append(f"event {e.get('kind')} {slim if slim else ''}")
    try:
        await status.edit_text(
            _render_live(aid, stages, log_lines, done=done),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Run again",
                                      callback_data=f"camp:sip:ag:{aid}")],
                _back("camp:sip"),
            ]),
        )
    except Exception:
        pass

    await _send_log(status, aid, jid, call, stages, log_lines, done, "")


async def _send_log(status, aid: str, jid: str, call: str, stages: dict,
                    log_lines: list, done, error: str):
    """Ship the full run log as a .log document — chat stays compact."""
    import io
    text = _build_log_file(aid, jid, call, stages, log_lines, done, error)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"sip_test_{aid}_{ts}.log"
    try:
        await status.reply_document(
            document=io.BytesIO(text.encode("utf-8")),
            filename=filename,
            caption=f"📄 Full SIP test log — {aid}",
        )
    except Exception as e:
        logger.warning(f"sip log document failed: {e}")


# ── callback routing (from menu.py, camp:sip…) ───────────────────────────────

async def handle_sip_callback(update, context, query, parts: list):
    sub = parts[2] if len(parts) > 2 else ""
    if sub == "ag" and len(parts) > 3:
        text, markup = await sip_agent_payload(context, parts[3])
        await _edit(query, text, markup)
    elif sub == "run" and len(parts) > 3:
        await query.message.reply_text("🚀 Launching…")
        await run_sip_test(query.message, context, parts[3])
    elif sub == "num" and len(parts) > 3:
        context.bot_data["sip_pending"] = {"agent": parts[3]}
        text, markup = sip_number_prompt_payload(parts[3])
        await _edit(query, text, markup)
    elif sub == "cancel":
        context.bot_data.pop("sip_pending", None)
        text, markup = await sip_home_payload(context)
        await _edit(query, text, markup)
    else:
        text, markup = await sip_home_payload(context)
        await _edit(query, text, markup)


async def _edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.debug(f"sip edit failed: {e}")


async def sip_text_input(update, context):
    """Capture the test-call number after the 📞 button (group -1 handler)."""
    pending = context.bot_data.get("sip_pending")
    if not pending or not update.message:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    context.bot_data.pop("sip_pending", None)

    import re as _re
    number = _re.sub(r"[^\d+]", "", text)
    if not number.startswith("+"):
        number = "+" + number if number else ""
    if len(number) < 8:
        await update.message.reply_text(
            "⚠️ That does not look like a phone number "
            "(e.g. <code>+14036139786</code>) — cancelled.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    await update.message.reply_text(
        f"📞 Calling <code>{_esc(number)}</code> via "
        f"<b>{_esc(pending['agent'])}</b> — live log below 👇",
        parse_mode="HTML",
    )
    await run_sip_test(update.message, context, pending["agent"], call=number)
    raise ApplicationHandlerStop
