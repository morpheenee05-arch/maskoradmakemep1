"""
/p1live — live P1 session dashboard with inline buttons.

Any user on the controller bot can watch the active bulk run in real time,
even if they did not start it. Stats are read from Redis.
"""
import asyncio

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.commands.p1_dashboard import format_dashboard


def live_keyboard(view: str, auto: bool) -> InlineKeyboardMarkup:
    def _tab(label: str, key: str) -> InlineKeyboardButton:
        active = view == key
        return InlineKeyboardButton(
            f"{'• ' if active else ''}{label}",
            callback_data=f"p1live:v:{key}",
        )

    return InlineKeyboardMarkup([
        [_tab("📊 Overview", "overview"), _tab("🖥 Servers", "servers"), _tab("🔥 Hot", "hot")],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"p1live:r:{view}"),
            InlineKeyboardButton(
                "⏱ Auto ON" if auto else "⏱ Auto OFF",
                callback_data=f"p1live:a:{0 if auto else 1}:{view}",
            ),
        ],
    ])


def _auto_key(chat_id: int, message_id: int):
    return (chat_id, message_id)


def _stop_auto(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    tasks = context.bot_data.setdefault("p1live_auto", {})
    entry = tasks.pop(_auto_key(chat_id, message_id), None)
    if entry and entry.get("task") and not entry["task"].done():
        entry["task"].cancel()


async def _render_live(
    bot, bus, chat_id: int, message_id: int, view: str, auto: bool,
) -> bool:
    """Edit message with current session stats. Returns False if session gone."""
    session = await bus.get_p1_viewable_session()
    if not session:
        return False

    running = session.get("running", False)
    text = format_dashboard(session, view)
    if not running:
        text += "\n\n<i>✅ session finished</i>"

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=live_keyboard(view, auto and running),
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug(f"p1live render: {e}")
    return True


async def _auto_refresh_loop(
    bot, bus, chat_id: int, message_id: int, view: str, context,
):
    try:
        while True:
            await asyncio.sleep(5)
            session = await bus.get_p1_active_session()
            if not session or not session.get("running"):
                _stop_auto(context, chat_id, message_id)
                await _render_live(bot, bus, chat_id, message_id, view, auto=False)
                break
            await _render_live(bot, bus, chat_id, message_id, view, auto=True)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("p1live auto-refresh crashed")


def _start_auto(context, bot, bus, chat_id, message_id, view):
    _stop_auto(context, chat_id, message_id)
    task = asyncio.create_task(
        _auto_refresh_loop(bot, bus, chat_id, message_id, view, context),
        name=f"p1live_auto_{message_id}",
    )
    context.bot_data.setdefault("p1live_auto", {})[_auto_key(chat_id, message_id)] = {
        "task": task,
        "view": view,
    }


async def p1live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the active P1 session dashboard (any user)."""
    bus = context.bot_data.get("bus")
    if bus is None:
        await update.message.reply_text(
            "⚠️ /p1live is only available in controller mode.",
        )
        return

    session = await bus.get_p1_viewable_session()
    if not session:
        await update.message.reply_text(
            "💤 <b>No active P1 session</b>\n\n"
            "Start one with <code>/p1</code> or upload a .txt file.",
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text(
        format_dashboard(session, "overview"),
        parse_mode="HTML",
        reply_markup=live_keyboard("overview", auto=False),
    )


async def p1live_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    bus = context.bot_data.get("bus")
    if bus is None:
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "p1live":
        return

    action = parts[1]
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    if action == "v":
        view = parts[2] if len(parts) > 2 else "overview"
        entry = context.bot_data.get("p1live_auto", {}).get(
            _auto_key(chat_id, message_id)
        )
        auto = bool(entry and entry.get("task") and not entry["task"].done())
        ok = await _render_live(
            context.bot, bus, chat_id, message_id, view, auto,
        )
        if not ok:
            await query.edit_message_text(
                "💤 <b>No active P1 session</b>",
                parse_mode="HTML",
            )
        return

    if action == "r":
        view = parts[2] if len(parts) > 2 else "overview"
        entry = context.bot_data.get("p1live_auto", {}).get(
            _auto_key(chat_id, message_id)
        )
        auto = bool(entry and entry.get("task") and not entry["task"].done())
        ok = await _render_live(
            context.bot, bus, chat_id, message_id, view, auto,
        )
        if not ok:
            _stop_auto(context, chat_id, message_id)
            await query.edit_message_text(
                "💤 <b>Session ended</b> — no live data.",
                parse_mode="HTML",
            )
        return

    if action == "a":
        enable = parts[2] == "1"
        view = parts[3] if len(parts) > 3 else "overview"
        session = await bus.get_p1_active_session()
        if enable and session and session.get("running"):
            _start_auto(context, context.bot, bus, chat_id, message_id, view)
            await _render_live(context.bot, bus, chat_id, message_id, view, True)
        else:
            _stop_auto(context, chat_id, message_id)
            await _render_live(context.bot, bus, chat_id, message_id, view, False)


def register_p1live_handlers(application):
    application.add_handler(CommandHandler("p1live", p1live_command))
    application.add_handler(CallbackQueryHandler(p1live_callback, pattern=r"^p1live:"))
    logger.info("Registered p1live handlers")
