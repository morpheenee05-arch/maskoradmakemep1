"""
/settings — manage P1 script templates with inline buttons.

Create, edit, duplicate, delete operator-made scripts and pick which one
every new batch call uses.
"""
from __future__ import annotations

import html
from typing import Optional

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from audio.script_profiles import SCRIPT_SLOTS, SLOT_LABELS
from storage.script_templates import (
    add_custom,
    bundle_for,
    delete_custom,
    duplicate,
    get_active_key_sync,
    get_profile_async,
    list_catalog_async,
    set_active,
    update_custom,
)

WAIT_NAME, WAIT_INTRO, WAIT_P1, WAIT_P2, WAIT_NONE, WAIT_RETRY, WAIT_EDIT = range(30, 37)

_FIELD_CODE = {
    "l": "label",
    "i": "intro",
    "1": "pressed_1",
    "2": "pressed_2",
    "n": "no_response",
    "r": "retry",
}
_FIELD_PROMPT = {
    "label": "Send the new name (emoji allowed):",
    "intro": "Send the new <b>intro</b> — first thing they hear:",
    "pressed_1": "Send the line for when they press <b>1</b>:",
    "pressed_2": "Send the line for when they press <b>2</b>:",
    "no_response": "Send the line if they <b>don't answer</b>:",
    "retry": "Send the <b>retry reminder</b>:",
}
_MAX_SLOT = 1200
_MAX_LABEL = 40

_DRAFT_KEYS = (
    "script_draft_label",
    "script_draft_intro",
    "script_draft_1",
    "script_draft_2",
    "script_draft_none",
    "script_draft_retry",
    "script_edit_key",
    "script_edit_field",
)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def _clip(text: str, n: int = 180) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _slot_text(profile: dict, slot: str) -> str:
    val = profile.get(slot)
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        return val[1] or ""
    return val or ""


def _sync_bot_active(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    context.bot_data["script_profile"] = key
    context.bot_data["script_bundle"] = bundle_for(key)


def _home_keyboard(rows: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        mark = "✅ " if row.get("active") else ""
        tag = "" if row.get("builtin") else " · yours"
        buttons.append([
            InlineKeyboardButton(
                f"{mark}{row['label']}{tag}",
                callback_data=f"scr:v:{row['key']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton("➕ New template", callback_data="scr:new"),
    ])
    return InlineKeyboardMarkup(buttons)


def _home_text(rows: list[dict]) -> str:
    active = next((r for r in rows if r.get("active")), None)
    name = active["label"] if active else "—"
    n_custom = sum(1 for r in rows if not r.get("builtin"))
    return (
        "🎬 <b>Script templates</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Default for batch calls: <b>{_esc(name)}</b>\n"
        f"{len(rows)} template(s) · {n_custom} of yours\n\n"
        "Tap a template to preview, edit, or make it the default."
    )


def _view_text(profile: dict, active_key: str) -> str:
    key = profile.get("key") or ""
    is_active = key == active_key
    kind = "Built-in" if profile.get("builtin") else "Your template"
    badge = " · <b>default ✅</b>" if is_active else ""
    lines = [
        f"🎬 <b>{_esc(profile.get('label'))}</b>",
        f"{kind}{badge}",
        "",
    ]
    for slot in SCRIPT_SLOTS:
        lines.append(
            f"<b>{SLOT_LABELS[slot]}</b>\n"
            f"<i>{_esc(_clip(_slot_text(profile, slot)))}</i>"
        )
        lines.append("")
    lines.append("New <code>/p1</code> batches will use the default template.")
    return "\n".join(lines)


def _view_keyboard(profile: dict) -> InlineKeyboardMarkup:
    key = profile.get("key") or ""
    builtin = bool(profile.get("builtin"))
    rows = [
        [InlineKeyboardButton("✅ Use as default", callback_data=f"scr:use:{key}")],
    ]
    if builtin:
        rows.append([
            InlineKeyboardButton("📋 Duplicate", callback_data=f"scr:dup:{key}"),
            InlineKeyboardButton("👁 Full text", callback_data=f"scr:full:{key}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("✏️ Edit", callback_data=f"scr:e:{key}"),
            InlineKeyboardButton("👁 Full text", callback_data=f"scr:full:{key}"),
        ])
        rows.append([
            InlineKeyboardButton("📋 Duplicate", callback_data=f"scr:dup:{key}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"scr:d:{key}"),
        ])
    rows.append([InlineKeyboardButton("◀️ All templates", callback_data="scr:home")])
    return InlineKeyboardMarkup(rows)


def _edit_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Name", callback_data=f"scr:ef:{key}:l")],
        [InlineKeyboardButton("1️⃣ Intro", callback_data=f"scr:ef:{key}:i")],
        [
            InlineKeyboardButton("✅ Press 1", callback_data=f"scr:ef:{key}:1"),
            InlineKeyboardButton("2️⃣ Press 2", callback_data=f"scr:ef:{key}:2"),
        ],
        [
            InlineKeyboardButton("😶 No answer", callback_data=f"scr:ef:{key}:n"),
            InlineKeyboardButton("🔁 Retry", callback_data=f"scr:ef:{key}:r"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"scr:v:{key}")],
    ])


def _wizard_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="scr:cancel")],
    ])


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💾 Save", callback_data="scr:save"),
            InlineKeyboardButton("💾 Save & use", callback_data="scr:saveuse"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="scr:cancel")],
    ])


async def _render_home(target, context, *, edit: bool) -> None:
    rows = await list_catalog_async()
    text = _home_text(rows)
    kb = _home_keyboard(rows)
    if edit:
        try:
            await target.edit_message_text(
                text, parse_mode="HTML", reply_markup=kb,
                disable_web_page_preview=True,
            )
            return
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug(f"script home edit: {e}")
            return
    await target.reply_text(
        text, parse_mode="HTML", reply_markup=kb,
        disable_web_page_preview=True,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _sync_bot_active(context, context.bot_data.get("script_profile") or get_active_key_sync())
    await _render_home(update.message, context, edit=False)


async def script_home_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    await _render_home(query, context, edit=True)
    return ConversationHandler.END


async def script_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    profile = await get_profile_async(key)
    try:
        await query.answer()
    except Exception:
        pass
    if not profile:
        await query.answer("Template not found", show_alert=True)
        await _render_home(query, context, edit=True)
        return
    active = context.bot_data.get("script_profile") or get_active_key_sync()
    try:
        await query.edit_message_text(
            _view_text(profile, active),
            parse_mode="HTML",
            reply_markup=_view_keyboard(profile),
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug(f"script view: {e}")
    return ConversationHandler.END


async def script_use_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    ok = await set_active(key)
    if not ok:
        await query.answer("Template not found", show_alert=True)
        return
    _sync_bot_active(context, key)
    profile = await get_profile_async(key)
    try:
        await query.answer(f"Default → {profile.get('label') if profile else key}")
    except Exception:
        pass
    if profile:
        try:
            await query.edit_message_text(
                _view_text(profile, key)
                + "\n\n✅ This template is now the default for all new batch calls.",
                parse_mode="HTML",
                reply_markup=_view_keyboard(profile),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    logger.info(f"Script profile changed to: {key}")


async def script_full_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    profile = await get_profile_async(key)
    try:
        await query.answer()
    except Exception:
        pass
    if not profile:
        return
    chunks = [f"🎬 <b>{_esc(profile.get('label'))}</b>\n"]
    for slot in SCRIPT_SLOTS:
        chunks.append(
            f"<b>{SLOT_LABELS[slot]}</b>\n{_esc(_slot_text(profile, slot))}\n"
        )
    text = "\n".join(chunks)
    if len(text) > 3500:
        text = text[:3490] + "…"
    try:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data=f"scr:v:{key}")],
            ]),
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug(f"script full: {e}")


async def script_dup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    new_key = await duplicate(key)
    if not new_key:
        await query.answer("Could not duplicate", show_alert=True)
        return
    profile = await get_profile_async(new_key)
    await query.answer("Duplicated — edit the copy")
    active = context.bot_data.get("script_profile") or get_active_key_sync()
    try:
        await query.edit_message_text(
            _view_text(profile, active)
            + "\n\n📋 Copy created. Edit the lines, then Use as default.",
            parse_mode="HTML",
            reply_markup=_view_keyboard(profile),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def script_del_ask_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    profile = await get_profile_async(key)
    try:
        await query.answer()
    except Exception:
        pass
    if not profile or profile.get("builtin"):
        await query.answer("Built-in templates cannot be deleted", show_alert=True)
        return
    await query.edit_message_text(
        f"🗑 Delete <b>{_esc(profile.get('label'))}</b>?\n\n"
        "This cannot be undone. If it is the default, "
        "the built-in Security Alert script will take over.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑 Yes, delete", callback_data=f"scr:dx:{key}"),
                InlineKeyboardButton("◀️ Keep it", callback_data=f"scr:v:{key}"),
            ],
        ]),
    )


async def script_del_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    ok = await delete_custom(key)
    if not ok:
        await query.answer("Could not delete", show_alert=True)
        return
    new_active = get_active_key_sync()
    _sync_bot_active(context, new_active)
    await query.answer("Deleted")
    await _render_home(query, context, edit=True)


async def script_edit_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    key = query.data.split(":", 2)[2]
    profile = await get_profile_async(key)
    try:
        await query.answer()
    except Exception:
        pass
    if not profile or profile.get("builtin"):
        await query.answer("Duplicate a built-in first, then edit the copy", show_alert=True)
        return
    await query.edit_message_text(
        f"✏️ <b>Edit {_esc(profile.get('label'))}</b>\n\n"
        "Pick the line to change, then send the new text.",
        parse_mode="HTML",
        reply_markup=_edit_keyboard(key),
    )
    return ConversationHandler.END


# ── Create / edit conversations ───────────────────────────────────────────────

def _clear_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in _DRAFT_KEYS:
        context.user_data.pop(k, None)


async def script_new_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    _clear_draft(context)
    await query.edit_message_text(
        "➕ <b>New script template</b>  ·  1/6\n\n"
        "Send a <b>name</b> — e.g. <code>Bank Alert</code> or <code>🏦 Package</code>.",
        parse_mode="HTML",
        reply_markup=_wizard_cancel_kb(),
    )
    return WAIT_NAME


async def _wizard_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name or len(name) > _MAX_LABEL + 8:
        await update.message.reply_text(
            f"Name must be 1–{_MAX_LABEL} characters. Try again.",
            reply_markup=_wizard_cancel_kb(),
        )
        return WAIT_NAME
    context.user_data["script_draft_label"] = name[:_MAX_LABEL]
    await update.message.reply_text(
        "➕ <b>New template</b>  ·  2/6\n\n"
        "Send the <b>intro</b> — first thing they hear "
        "(tell them to press 1 / press 2).",
        parse_mode="HTML",
        reply_markup=_wizard_cancel_kb(),
    )
    return WAIT_INTRO


def _too_long(text: str) -> Optional[str]:
    if not (text or "").strip():
        return "This line cannot be empty."
    if len(text) > _MAX_SLOT:
        return f"Too long ({len(text)} chars, max {_MAX_SLOT})."
    return None


async def _wizard_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    err = _too_long(text)
    if err:
        await update.message.reply_text(err, reply_markup=_wizard_cancel_kb())
        return WAIT_INTRO
    context.user_data["script_draft_intro"] = text
    await update.message.reply_text(
        "➕ <b>New template</b>  ·  3/6\n\n"
        "What should they hear after pressing <b>1</b>?",
        parse_mode="HTML",
        reply_markup=_wizard_cancel_kb(),
    )
    return WAIT_P1


async def _wizard_p1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    err = _too_long(text)
    if err:
        await update.message.reply_text(err, reply_markup=_wizard_cancel_kb())
        return WAIT_P1
    context.user_data["script_draft_1"] = text
    await update.message.reply_text(
        "➕ <b>New template</b>  ·  4/6\n\n"
        "What should they hear after pressing <b>2</b>?",
        parse_mode="HTML",
        reply_markup=_wizard_cancel_kb(),
    )
    return WAIT_P2


async def _wizard_p2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    err = _too_long(text)
    if err:
        await update.message.reply_text(err, reply_markup=_wizard_cancel_kb())
        return WAIT_P2
    context.user_data["script_draft_2"] = text
    await update.message.reply_text(
        "➕ <b>New template</b>  ·  5/6\n\n"
        "Line if they <b>don't press anything</b>:",
        parse_mode="HTML",
        reply_markup=_wizard_cancel_kb(),
    )
    return WAIT_NONE


async def _wizard_none(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    err = _too_long(text)
    if err:
        await update.message.reply_text(err, reply_markup=_wizard_cancel_kb())
        return WAIT_NONE
    context.user_data["script_draft_none"] = text
    await update.message.reply_text(
        "➕ <b>New template</b>  ·  6/6\n\n"
        "Short <b>retry reminder</b> (played once if they stay silent after the intro):",
        parse_mode="HTML",
        reply_markup=_wizard_cancel_kb(),
    )
    return WAIT_RETRY


async def _wizard_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    err = _too_long(text)
    if err:
        await update.message.reply_text(err, reply_markup=_wizard_cancel_kb())
        return WAIT_RETRY
    context.user_data["script_draft_retry"] = text
    ud = context.user_data
    preview = (
        "👀 <b>Preview</b>\n"
        f"<b>{_esc(ud.get('script_draft_label'))}</b>\n\n"
        f"<b>Intro</b>\n<i>{_esc(_clip(ud.get('script_draft_intro'), 220))}</i>\n\n"
        f"<b>Press 1</b>\n<i>{_esc(_clip(ud.get('script_draft_1'), 160))}</i>\n\n"
        f"<b>Press 2</b>\n<i>{_esc(_clip(ud.get('script_draft_2'), 160))}</i>\n\n"
        f"<b>No answer</b>\n<i>{_esc(_clip(ud.get('script_draft_none'), 140))}</i>\n\n"
        f"<b>Retry</b>\n<i>{_esc(_clip(ud.get('script_draft_retry'), 140))}</i>"
    )
    await update.message.reply_text(
        preview, parse_mode="HTML", reply_markup=_confirm_kb(),
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def _save_draft(context, *, use: bool) -> Optional[str]:
    ud = context.user_data
    key = await add_custom(
        ud.get("script_draft_label") or "Script",
        ud.get("script_draft_intro") or "",
        ud.get("script_draft_1") or "",
        ud.get("script_draft_2") or "",
        ud.get("script_draft_none") or "",
        ud.get("script_draft_retry") or "",
    )
    if key and use:
        await set_active(key)
        _sync_bot_active(context, key)
    _clear_draft(context)
    return key


async def script_save_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    use = query.data == "scr:saveuse"
    if not context.user_data.get("script_draft_intro"):
        await query.answer("Nothing to save — start with ➕ New template", show_alert=True)
        return
    key = await _save_draft(context, use=use)
    if not key:
        await query.answer("Save failed", show_alert=True)
        return
    profile = await get_profile_async(key)
    extra = " and set as the default" if use else ""
    await query.answer(f"Saved{extra}")
    active = context.bot_data.get("script_profile") or get_active_key_sync()
    await query.edit_message_text(
        _view_text(profile, active)
        + (f"\n\n✅ Saved{extra}." if profile else ""),
        parse_mode="HTML",
        reply_markup=_view_keyboard(profile) if profile else None,
        disable_web_page_preview=True,
    )


async def script_edit_field_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    # scr:ef:{key}:{code}
    if len(parts) < 4:
        await query.answer()
        return ConversationHandler.END
    key, code = parts[2], parts[3]
    field = _FIELD_CODE.get(code)
    profile = await get_profile_async(key)
    if not field or not profile or profile.get("builtin"):
        await query.answer("Cannot edit this", show_alert=True)
        return ConversationHandler.END
    context.user_data["script_edit_key"] = key
    context.user_data["script_edit_field"] = field
    current = (
        profile.get("label") if field == "label"
        else _slot_text(profile, field)
    )
    try:
        await query.answer()
    except Exception:
        pass
    await query.edit_message_text(
        f"✏️ <b>{_esc(profile.get('label'))}</b> — {SLOT_LABELS.get(field, field)}\n\n"
        f"<i>Current:</i>\n{_esc(_clip(current, 500))}\n\n"
        f"{_FIELD_PROMPT[field]}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Back", callback_data=f"scr:e:{key}")],
        ]),
    )
    return WAIT_EDIT


async def _wizard_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("script_edit_key")
    field = context.user_data.get("script_edit_field")
    text = (update.message.text or "").strip()
    if not key or not field:
        await update.message.reply_text("Nothing to edit. Open /settings again.")
        return ConversationHandler.END
    if field == "label":
        if not text or len(text) > _MAX_LABEL + 8:
            await update.message.reply_text(f"Name must be 1–{_MAX_LABEL} characters.")
            return WAIT_EDIT
        text = text[:_MAX_LABEL]
    else:
        err = _too_long(text)
        if err:
            await update.message.reply_text(err)
            return WAIT_EDIT
    ok = await update_custom(key, **{field: text})
    context.user_data.pop("script_edit_key", None)
    context.user_data.pop("script_edit_field", None)
    if not ok:
        await update.message.reply_text("Could not save that change.")
        return ConversationHandler.END
    profile = await get_profile_async(key)
    await update.message.reply_text(
        f"✅ Updated <b>{_esc((profile or {}).get('label') or key)}</b> — "
        f"{SLOT_LABELS.get(field, field)}.",
        parse_mode="HTML",
        reply_markup=_edit_keyboard(key),
    )
    return ConversationHandler.END


async def script_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _clear_draft(context)
    try:
        await query.answer("Cancelled")
    except Exception:
        pass
    await _render_home(query, context, edit=True)
    return ConversationHandler.END


async def script_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_draft(context)
    await update.message.reply_text("Cancelled.")
    await _render_home(update.message, context, edit=False)
    return ConversationHandler.END


def register_script_handlers(application):
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("script", settings_command))
    application.add_handler(CommandHandler("scripts", settings_command))

    async def _legacy_settings_profile(update, context):
        query = update.callback_query
        key = (query.data or "").split(":", 1)[-1]
        query.data = f"scr:use:{key}"
        return await script_use_cb(update, context)

    application.add_handler(CallbackQueryHandler(script_home_cb, pattern=r"^scr:home$"))
    application.add_handler(CallbackQueryHandler(script_cancel_cb, pattern=r"^scr:cancel$"))
    application.add_handler(
        CallbackQueryHandler(_legacy_settings_profile, pattern=r"^settings_profile:")
    )
    application.add_handler(CallbackQueryHandler(script_view_cb, pattern=r"^scr:v:"))
    application.add_handler(CallbackQueryHandler(script_use_cb, pattern=r"^scr:use:"))
    application.add_handler(CallbackQueryHandler(script_full_cb, pattern=r"^scr:full:"))
    application.add_handler(CallbackQueryHandler(script_dup_cb, pattern=r"^scr:dup:"))
    application.add_handler(CallbackQueryHandler(script_del_ask_cb, pattern=r"^scr:d:"))
    application.add_handler(CallbackQueryHandler(script_del_do_cb, pattern=r"^scr:dx:"))
    application.add_handler(CallbackQueryHandler(script_edit_menu_cb, pattern=r"^scr:e:"))
    application.add_handler(CallbackQueryHandler(script_save_cb, pattern=r"^scr:save"))

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(script_new_cb, pattern=r"^scr:new$"),
            CallbackQueryHandler(script_edit_field_cb, pattern=r"^scr:ef:"),
        ],
        states={
            WAIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_name)],
            WAIT_INTRO: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_intro)],
            WAIT_P1: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_p1)],
            WAIT_P2: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_p2)],
            WAIT_NONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_none)],
            WAIT_RETRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_retry)],
            WAIT_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, _wizard_edit_text)],
        },
        fallbacks=[
            CallbackQueryHandler(script_cancel_cb, pattern=r"^scr:cancel$"),
            CallbackQueryHandler(script_home_cb, pattern=r"^scr:home$"),
            CallbackQueryHandler(script_edit_menu_cb, pattern=r"^scr:e:"),
            CallbackQueryHandler(script_view_cb, pattern=r"^scr:v:"),
            CommandHandler("cancel", script_cancel_cmd),
        ],
        name="script_wizard",
        persistent=False,
        allow_reentry=True,
    )
    application.add_handler(conv)
    logger.info("Registered script template handlers")
