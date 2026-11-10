"""
/hangup list — lines that picked up and then hung up.

Shows hangup results from the active/last P1 session (controller mode,
read from Redis) or from the local standalone bulk run, newest first,
10 per page, with inline pagination buttons.

The call-terminating action of /hangup is preserved as /hangup <call_id>
(see bot/commands/call.py).
"""
import html
import math
import time

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

PAGE_SIZE = 10
LOCAL_KEY = "p1_local_hangups"


def _esc(v) -> str:
    return html.escape(str(v if v is not None else "—"))


def record_local_hangup(context: ContextTypes.DEFAULT_TYPE, phone: str,
                        line: str = None, agent: str = "local") -> None:
    """Store one hangup line from the standalone (non-distributed) bulk run."""
    lst = context.bot_data.setdefault(LOCAL_KEY, [])
    lst.append({
        "phone": phone,
        "line": line or phone,
        "agent": agent,
        "ts": time.time(),
    })
    del lst[:-500]


async def _get_hangups(context: ContextTypes.DEFAULT_TYPE):
    """(items, source_note) — newest last inside the list."""
    bus = context.bot_data.get("bus")
    if bus is not None:
        session = await bus.get_p1_viewable_session()
        if session:
            items = session.get("hangups") or []
            note = f"run <code>{_esc(session.get('run_id', '?'))}</code>"
            if session.get("running"):
                note += " · 🔄 live"
            return items, note
        return [], ""
    return context.bot_data.get(LOCAL_KEY, []) or [], "local run"


def format_hangups_page(items: list, page: int, source_note: str = "") -> "tuple[str, int]":
    """Render one page (newest first). Returns (text, total_pages)."""
    total = len(items)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))

    ordered = list(reversed(items))  # newest first
    chunk = ordered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    header = [
        "📞 <b>Picked up → hung up</b>",
        f"lines: <b>{total}</b>"
        + (f" · {source_note}" if source_note else "")
        + (f" · page {page + 1}/{pages}" if pages > 1 else ""),
        "",
    ]
    if not chunk:
        header.append("<i>no hangups yet</i>")
    else:
        for idx, item in enumerate(chunk, start=page * PAGE_SIZE + 1):
            line = item.get("line") or item.get("phone", "?")
            agent = item.get("agent", "?")
            ts = item.get("ts")
            tstr = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "??:??:??"
            header.append(
                f"{idx}. <code>{_esc(line)}</code>\n"
                f"    🖥 <code>{_esc(agent)}</code> · 🕒 {tstr}"
            )
    if pages > 1:
        header.append("\n<i>◀️ ▶️ — листать по 10 шт</i>")
    return "\n".join(header), pages


def hangups_keyboard(page: int, pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"hangups:p:{page - 1}"))
    if pages > 1:
        row.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="hangups:n:0"))
    if page < pages - 1:
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"hangups:p:{page + 1}"))
    refresh = InlineKeyboardButton("🔄 Refresh", callback_data=f"hangups:r:{page}")
    return InlineKeyboardMarkup([row, [refresh]] if row else [[refresh]])


async def hangup_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show hangup lines with inline pagination (10 per page)."""
    items, note = await _get_hangups(context)
    if not items:
        await update.message.reply_text(
            "💤 <b>No hangup lines yet</b>\n\n"
            "Здесь появятся номера, где взяли трубку и сбросили.",
            parse_mode="HTML",
        )
        return
    text, pages = format_hangups_page(items, 0, note)
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=hangups_keyboard(0, pages),
    )


async def hangups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "hangups":
        return
    action = parts[1]
    if action == "n":  # page indicator — no-op
        return
    try:
        page = int(parts[2])
    except ValueError:
        page = 0

    items, note = await _get_hangups(context)
    text, pages = format_hangups_page(items, page, note)
    page = max(0, min(page, pages - 1))
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=hangups_keyboard(page, pages),
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug(f"hangups render: {e}")


def register_p1_hangups_handlers(application):
    application.add_handler(CallbackQueryHandler(hangups_callback, pattern=r"^hangups:"))
    logger.info("Registered hangups list handlers")
