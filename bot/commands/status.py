"""
/status, /help, /ask, /settings commands - Show system status, help, settings.
Telegram command handlers.
"""
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from loguru import logger

import config

# Module-level start time
_start_time = datetime.now()


async def _status_distributed(update, bus):
    """Controller mode: aggregate capacity across all live agents."""
    try:
        agents = await bus.list_agents()
    except Exception as e:
        await update.message.reply_text(
            f"📊 *AI Caller (controller)*\n\n❌ Redis недоступен: `{e}`",
            parse_mode="Markdown",
        )
        return

    if not agents:
        await update.message.reply_text(
            "📊 *AI Caller (controller)*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ Нет активных агентов.\n"
            "Запусти агента на сервере: `NODE_ROLE=agent python main.py`",
            parse_mode="Markdown",
        )
        return

    total = sum(a.get("total_workers", 0) for a in agents)
    ready = sum(a.get("ready_workers", 0) for a in agents)
    busy = sum(a.get("checked_out_workers", 0) for a in agents)
    needs_login = sum(a.get("needs_login_workers", 0) for a in agents)
    challenge = sum(a.get("challenge_workers", 0) for a in agents)
    captcha = sum(a.get("captcha_workers", 0) for a in agents)

    lines = [
        "📊 *AI Caller — Cluster*",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"🖥️ Агентов онлайн: *{len(agents)}*",
        f"✅ Готовых воркеров: *{ready}/{total}*  •  🔴 занято: {busy}",
    ]
    if needs_login or challenge or captcha:
        note = []
        if needs_login:
            note.append(f"🔑 ждут вход: *{needs_login}* (/accounts, затем /login)")
        if challenge:
            note.append(f"🧩 нужен 2FA вручную: *{challenge}*")
        if captcha:
            note.append(f"🤖 капча: *{captcha}*")
        lines.append("  •  ".join(note))
    lines += ["", "*Агенты:*"]
    for a in agents:
        st = str(a.get("state", "?")).upper()
        extra = ""
        if a.get("needs_login_workers"):
            extra += f" · 🔑{a['needs_login_workers']}"
        if a.get("challenge_workers"):
            extra += f" · 🧩{a['challenge_workers']}"
        if a.get("captcha_workers"):
            extra += f" · 🤖{a['captcha_workers']}"
        lines.append(
            f"  • `{a.get('agent_id','?')}` — {st} "
            f"{a.get('ready_workers',0)}/{a.get('total_workers',0)} ready"
            f" · v{a.get('version','?')}{extra}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current system status — real pool data."""
    bus = context.bot_data.get("bus")
    if bus is not None:
        await _status_distributed(update, bus)
        return

    gv_pool = context.bot_data.get("gv_pool")

    if not gv_pool:
        await update.message.reply_text(
            "📊 *AI Caller Status*\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ Telephony pool not yet initialized.\n\n"
            "💡 Use `/call` or `/p1` once workers are ready.",
            parse_mode="Markdown",
        )
        return

    snap = gv_pool.get_status_snapshot()
    state = snap["state"].upper()
    ready = snap["ready_workers"]
    total = snap["total_workers"]
    init_s = snap["init_seconds"]
    numbered = snap["numbers_fetched_workers"]

    # ── State emoji ──
    if snap.get("all_workers_ready"):
        state_line = f"✅ ALL {ready}/{total} WORKERS READY"
    elif ready > 0:
        state_line = f"⚠️ DEGRADED — {ready}/{total} workers ready"
    elif state == "INITIALIZING":
        state_line = f"⏳ INITIALIZING — {ready}/{total} ready so far"
    else:
        state_line = f"❌ FAILED — 0/{total} workers ready"

    timing = f"  _(init took {init_s}s)_" if init_s else ""
    lines = [
        "📊 *AI Caller Status*",
        "━━━━━━━━━━━━━━━━━━",
        "",
        state_line + timing,
        "",
        "*Workers:*",
    ]

    for ws in snap["workers"]:
        wi = ws["worker_id"]
        wstate = ws["state"]
        num = ws.get("phone_number") or "—"
        err = ws.get("last_error") or ""
        if wstate == "ready":
            b = ws.get("browser_start_seconds", 0)
            l_ = ws.get("login_seconds", 0)
            icon = "✅"
            detail = f"`{num}` _(b={b:.1f}s l={l_:.1f}s)_"
        elif wstate == "initializing":
            icon = "⏳"
            detail = "initializing…"
        else:
            icon = "❌"
            detail = f"`{err[:60]}`" if err else "failed"
        lines.append(f"  {icon} Worker {wi}: {detail}")

    lines += [
        "",
        f"💡 Use `/call` to make a call • `/prompt_list` to manage prompts",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help information."""
    text = (
        "❓ *AI Caller Help*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "*📞 Calling:*\n"
        "  `/call <phone>` — Make an outbound AI call\n"
        "  `/p1 <phone> [phone2] [phone3]` — Scripted IVR (bulk supported)\n"
        "  `/p1live` — Live bulk session stats (inline dashboard)\n"
        "  `/p1stop` — Stop bulk queue\n"
        "  📄 Send a `.txt` file to extract numbers\n"
        "  `/hangup [call_id]` — End an active call\n"
        "  `/settings` or `/script` — Script templates (create / pick default)\n"
        "  `/status` — Show system status\n"
        "  `/wipes` — GV wipe archive (servers / accounts / cause, screenshots)\n"
        "*📝 Prompts:*\n"
        "  `/prompt_list` — List all prompts\n"
        "  `/prompt_add` — Create a new prompt\n"
        "  `/prompt_edit <name>` — Edit a prompt\n"
        "  `/prompt_delete <name>` — Delete a prompt\n"
        "  `/prompt_view <name>` — View full prompt\n\n"
        "*🤖 Other:*\n"
        "  `/ask <message>` — Test AI response\n"
        "  `/help` — Show this help\n"
        "  `/cancel` — Cancel current operation\n\n"
        "*📖 Examples:*\n"
        "```\n"
        "/call +15551234567\n"
        "/call 5551234567\n"
        "/prompt_add\n"
        "```"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test the AI without making a call."""
    await update.message.reply_text(
        "The `/ask` command is not available.\n"
        "Use `/call` or `/p1` to make calls.",
        parse_mode="Markdown",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command (Telegram bot greeting)."""
    text = (
        "👋 *Welcome to AI Caller Bot!*\n\n"
        "I can make outbound AI-powered phone calls.\n\n"
        "Use /help to see all available commands."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Register handlers ──

def register_status_handlers(application):
    """Register status/help/ask handlers. /settings lives in scripts.py."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ask", ask_command))

    logger.info("Registered status handlers")
