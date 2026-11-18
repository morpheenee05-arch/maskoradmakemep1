"""
/menu — main inline panel: Campaigns, Campaign results, Scripts, Spam check.

One message with inline keyboards instead of memorised commands. Campaigns
are named after the uploaded file (Bobby.txt → campaign "Bobby"); when a
campaign finishes, its results stay exportable/redialable from the
Campaign results screen for a week.

Callback scheme (all handled here): camp:<route>[:...]
"""
import asyncio
import html
import time
from io import BytesIO

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.commands.p1_dashboard import (
    _LABEL,
    category_title,
    format_dashboard,
    phones_for,
)


def _esc(v) -> str:
    return html.escape(str(v if v is not None else "—"))


async def _edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug(f"menu edit failed: {e}")


def _back(to: str = "camp:home") -> list:
    return [InlineKeyboardButton("🔙 Back", callback_data=to)]


class _UpdateShim:
    """Update proxy for callbacks: legacy handlers read `update.message`,
    which is None on a callback — and PTB forbids assigning it. Everything
    else (effective_user/chat/message) falls through to the real update."""

    __slots__ = ("_update", "message")

    def __init__(self, update, message):
        object.__setattr__(self, "_update", update)
        object.__setattr__(self, "message", message)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_update"), name)

    def __setattr__(self, name, value):
        raise AttributeError("shim is read-only")


def _shim(update, query):
    return _UpdateShim(update, query.message)


# ── home ──────────────────────────────────────────────────────────────────────

async def _home_payload(context) -> tuple:
    bus = context.bot_data.get("bus")
    agents_online = lines_ready = lines_total = 0
    if bus is not None:
        try:
            agents = [a for a in await bus.list_agents() if a.get("role") == "agent"]
            agents_online = len(agents)
            lines_ready = sum(a.get("ready_workers", 0) for a in agents)
            lines_total = sum(a.get("total_workers", 0) for a in agents)
        except Exception:
            pass
        active = await bus.get_p1_active_session()
    else:
        pool = context.bot_data.get("gv_pool")
        if pool:
            try:
                snap = pool.get_status_snapshot()
                lines_ready = snap.get("ready_workers", 0)
                lines_total = snap.get("total_workers", 0)
                agents_online = 1
            except Exception:
                pass
        q = context.bot_data.get("p1_queue")
        if q and q.get("running"):
            results = q.get("results") or {}
            done = sum(1 for r in results.values() if r.get("status") == "done")
            active = {
                "campaign_name": q.get("campaign_name") or "campaign",
                "done": done, "total": len(q.get("numbers") or []),
                "running": True,
            }
        else:
            active = None

    bundle = context.bot_data.get("script_bundle") or {}
    script_label = (
        bundle.get("label") or context.bot_data.get("script_profile") or "?"
    )

    lines = ["📞 <b>AI Caller</b>", "━━━━━━━━━━━━━━━━━━"]
    if agents_online:
        lines.append(
            f"🖥 Agents online: <b>{agents_online}</b> · "
            f"✅ lines ready: <b>{lines_ready}/{lines_total}</b>"
        )
    else:
        lines.append("🖥 <b>No agents online</b>")
    if active and active.get("running"):
        total = active.get("total", 0)
        done = active.get("done", 0)
        pct = (100.0 * done / total) if total else 0.0
        lines.append(
            f"📞 Campaign <b>{_esc(active.get('campaign_name') or 'campaign')}</b>"
            f" — running: <b>{done}/{total}</b> ({pct:.0f}%)"
        )
    else:
        lines.append("📞 No active campaign")
    lines.append(f"📝 Script: <code>{_esc(script_label)}</code>")
    lines.append("\nChoose an action below.")

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📞 Campaigns", callback_data="camp:campaigns"),
            InlineKeyboardButton("📊 Campaign results", callback_data="camp:results"),
        ],
        [
            InlineKeyboardButton("📝 Scripts", callback_data="camp:scripts"),
            InlineKeyboardButton("🚨 Spam check", callback_data="camp:spam"),
        ],
        [
            InlineKeyboardButton("🖥 Agents", callback_data="camp:agents"),
            InlineKeyboardButton("🧪 SIP test", callback_data="camp:sip"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="camp:set"),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="camp:home")],
    ])
    return "\n".join(lines), markup


# ── campaigns ─────────────────────────────────────────────────────────────────

async def _campaigns_payload(context) -> tuple:
    bus = context.bot_data.get("bus")
    active = None
    if bus is not None:
        active = await bus.get_p1_active_session()
    else:
        q = context.bot_data.get("p1_queue")
        if q and q.get("running"):
            results = q.get("results") or {}
            active = {
                "campaign_name": q.get("campaign_name") or "campaign",
                "done": sum(1 for r in results.values() if r.get("status") == "done"),
                "total": len(q.get("numbers") or []),
                "running": True,
            }

    lines = ["📞 <b>Campaigns</b>", ""]
    rows = []
    if active and active.get("running"):
        total = active.get("total", 0)
        done = active.get("done", 0)
        lines.append(
            f"Active: <b>{_esc(active.get('campaign_name') or 'campaign')}</b>"
            f" — {done}/{total} 🔄"
        )
        if bus is not None:
            rows.append([InlineKeyboardButton(
                "📡 Live dashboard", callback_data="camp:live"
            )])
        rows.append([InlineKeyboardButton(
            "⏹ Stop campaign", callback_data="camp:stop"
        )])
    else:
        lines.append("No active campaign.")
    lines += [
        "",
        "▶️ <b>New campaign</b>: drop a <code>.txt</code> file into this "
        "chat — numbers are auto-formatted from any layout, and the file "
        "name becomes the campaign name.",
    ]
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _stop_campaign(update, context, query):
    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.p1_distributed import p1stop_distributed
        await p1stop_distributed(_shim(update, query), context, bus)
    else:
        q = context.bot_data.get("p1_queue")
        if q and q.get("running"):
            q["running"] = False
            await query.message.reply_text("🛑 Local queue stopping…")
        else:
            await query.answer("No campaign is running.", show_alert=True)


# ── campaign results ──────────────────────────────────────────────────────────

async def _load_history(context) -> list:
    bus = context.bot_data.get("bus")
    if bus is not None:
        try:
            return await bus.get_p1_history(limit=20)
        except Exception as e:
            logger.warning(f"menu: history load failed: {e}")
            return []
    history = context.bot_data.get("campaign_history") or {}
    return sorted(
        history.values(), key=lambda s: s.get("started_at", 0), reverse=True
    )


async def _find_campaign(context, run_id: str):
    bus = context.bot_data.get("bus")
    if bus is not None:
        return await bus.get_p1_session(run_id)
    return (context.bot_data.get("campaign_history") or {}).get(run_id)


def _camp_button_label(s: dict) -> str:
    name = s.get("campaign_name") or "campaign"
    done, total = s.get("done", 0), s.get("total", 0)
    counts = s.get("counts") or {}
    hot = counts.get("1", 0) + counts.get("2", 0)
    ts = s.get("started_at", 0)
    date = time.strftime("%d.%m", time.localtime(ts)) if ts else "?"
    label = f"{name} · {date} · {done}/{total}"
    if hot:
        label += f" · 🔥{hot}"
    return label[:60]


async def _results_payload(context) -> tuple:
    history = await _load_history(context)
    lines = ["📊 <b>Campaign results</b>", ""]
    if not history:
        lines.append("No finished campaigns yet.")
        rows = [_back()]
        return "\n".join(lines), InlineKeyboardMarkup(rows)
    rows = []
    for s in history[:15]:
        rows.append([InlineKeyboardButton(
            _camp_button_label(s),
            callback_data=f"camp:res:{s.get('run_id')}",
        )])
    rows.append(_back())
    lines.append("Pick a campaign:")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _campaign_detail_payload(context, run_id: str) -> tuple:
    s = await _find_campaign(context, run_id)
    if not s:
        return (
            "⚠️ Campaign data expired (older than a week).",
            InlineKeyboardMarkup([_back("camp:results")]),
        )
    name = s.get("campaign_name") or "campaign"
    done, total = s.get("done", 0), s.get("total", 0)
    pct = (100.0 * done / total) if total else 0.0
    counts = s.get("counts") or {}
    ts = s.get("started_at", 0)
    date = time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "?"
    lines = [
        f"📊 <b>Campaign {_esc(name)}</b>",
        f"<code>{_esc(run_id)}</code> · {date} · script "
        f"<code>{_esc(s.get('script_label') or s.get('script_profile') or '?')}</code>",
        f"<b>{done}/{total}</b> ({pct:.1f}%)",
        "",
    ]
    if counts:
        lines += [
            " · ".join(
                f"{_LABEL.get(k, k)}: <b>{v}</b>"
                for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
            ),
            "",
        ]
    rows = []
    exp_row = []
    for cat in ("hangup", "noanswer", "hot"):
        n = len(phones_for(s, cat))
        if n:
            exp_row.append(InlineKeyboardButton(
                f"📥 {category_title(cat)} ({n})",
                callback_data=f"camp:exp:{run_id}:{cat}",
            ))
    if exp_row:
        rows.append(exp_row)
        lines.append("📥 Export as .txt — or redial in one tap:")
    redial_row = []
    for cat in ("hangup", "noanswer"):
        if phones_for(s, cat):
            redial_row.append(InlineKeyboardButton(
                f"🔁 Redial {category_title(cat)}",
                callback_data=f"camp:redial:{run_id}:{cat}",
            ))
    if redial_row:
        rows.append(redial_row)
    rows.append(_back("camp:results"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _export_campaign(update, context, query, run_id: str, cat: str):
    s = await _find_campaign(context, run_id)
    phones = phones_for(s or {}, cat)
    if not phones:
        await query.answer("Nothing to export for this category.", show_alert=True)
        return
    line_map = (s or {}).get("line_map") or {}
    lines = [line_map.get(p, p) for p in phones]
    name = ((s or {}).get("campaign_name") or "campaign").replace(" ", "_")
    bio = BytesIO("\n".join(lines).encode("utf-8"))
    bio.name = f"{name}_{cat}.txt"
    await query.message.reply_document(
        document=bio,
        filename=bio.name,
        caption=(
            f"📥 {_esc(name)} — {category_title(cat)}: "
            f"<b>{len(lines)}</b> number(s)",
        ),
        parse_mode="HTML",
    )


async def _redial_campaign(update, context, query, run_id: str, cat: str):
    s = await _find_campaign(context, run_id)
    phones = phones_for(s or {}, cat)
    if not phones:
        await query.answer("Nothing to redial for this category.", show_alert=True)
        return
    line_map_all = (s or {}).get("line_map") or {}
    line_map = {p: line_map_all.get(p, p) for p in phones}
    base_name = (s or {}).get("campaign_name") or "campaign"
    name = f"{base_name} redial"[:40]
    script_profile = context.bot_data.get("script_profile", "suspicious")

    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.p1_distributed import p1_distributed
        await p1_distributed(
            _shim(update, query), context, bus, phones, script_profile,
            line_map=line_map, campaign_name=name,
        )
        return

    # Standalone: seed a local queue exactly like a confirmed file upload.
    pool = context.bot_data.get("gv_pool")
    if not pool:
        await query.answer("Telephony pool is not ready.", show_alert=True)
        return
    q = context.bot_data.get("p1_queue")
    if q and q.get("running"):
        await query.answer("A campaign is already running.", show_alert=True)
        return
    from bot.commands.call import _build_queue_status, _run_bulk_queue_workers
    chat_id = query.message.chat_id
    results = {phone: {"status": "pending"} for phone in phones}
    status_msg = await query.message.reply_text(
        _build_queue_status(phones, results, 0, True), parse_mode="Markdown",
    )
    queue_state = {
        "numbers": phones,
        "results": results,
        "current_index": 0,
        "running": True,
        "chat_id": chat_id,
        "status_message_id": status_msg.message_id if status_msg else None,
        "line_map": line_map,
        "campaign_name": name,
        "started_at": time.time(),
    }
    context.bot_data["p1_queue"] = queue_state
    asyncio.create_task(
        _run_bulk_queue_workers(
            context, phones, results, queue_state,
            chat_id, query.from_user.id,
            queue_state["status_message_id"],
        ),
        name="p1_redial",
    )


# ── scripts ───────────────────────────────────────────────────────────────────

async def _scripts_payload(context) -> tuple:
    from storage.script_templates import list_catalog_async
    rows_data = await list_catalog_async()
    lines = [
        "📝 <b>Scripts</b>",
        "",
        "The active script is played on every new campaign.",
        "Manage the texts with /scripts.",
    ]
    rows = []
    for r in rows_data:
        mark = "✅ " if r.get("active") else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{r.get('label') or r['key']}",
            callback_data=f"camp:script:{r['key']}",
        )])
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _select_script(context, query, key: str):
    from storage.script_templates import bundle_for, set_active
    ok = await set_active(key)
    if not ok:
        await query.answer("Unknown script.", show_alert=True)
        return
    context.bot_data["script_profile"] = key
    context.bot_data["script_bundle"] = bundle_for(key)
    label = (context.bot_data["script_bundle"] or {}).get("label") or key
    await query.answer(f"Active script → {label}")


# ── spam check ────────────────────────────────────────────────────────────────

def _spam_payload() -> tuple:
    lines = [
        "🚨 <b>Spam check</b>",
        "",
        "Profiles call each other (calls are not answered); the receiver's "
        "history is checked for a «Suspected spam» flag.",
        "Screenshots are sent for flagged numbers only.",
    ]
    rows = [
        [InlineKeyboardButton("▶️ Run (parallel)", callback_data="camp:spam:run:parallel")],
        [InlineKeyboardButton("🔁 Sequential", callback_data="camp:spam:run:seq")],
        [InlineKeyboardButton("🐞 Debug (all screenshots)", callback_data="camp:spam:run:debug")],
        _back(),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── settings (remote agent config + Deepgram key pool) ───────────────────────

_SET_PAGE = 8


async def _load_settings(context) -> dict:
    """Current remote overrides: Redis hash (controller) or JSON (standalone)."""
    bus = context.bot_data.get("bus")
    if bus is not None:
        try:
            return await bus.get_settings()
        except Exception as e:
            logger.warning(f"menu: settings load failed: {e}")
            return {}
    from infra.remote_settings import load_local_settings
    return load_local_settings()


def _setting_current_display(name: str, overrides: dict) -> tuple:
    """(display string, is_override) for the settings list."""
    from infra.remote_settings import SETTABLE, display_value
    import config as _cfg

    typ = SETTABLE[name][0]
    if name in overrides:
        return display_value(name, overrides[name]), True
    if typ == "keys":
        from infra.dg_keys import get_pool, mask_key
        keys = get_pool().keys()
        return (", ".join(mask_key(k) for k in keys) or "(none)"), False
    return str(getattr(_cfg, name, "?")), False


async def _settings_home_payload(context) -> tuple:
    """Settings sections: Deepgram key pool + per-agent remote settings."""
    from infra.remote_settings import SETTABLE

    overrides = await _load_settings(context)
    rows = await _dg_report(context, overrides)
    total = sum(b for _, st, b, _ in rows if b)
    alive = sum(1 for _, st, b, _ in rows if st == "ok" and (b or 0) > 0)

    lines = [
        "⚙️ <b>Settings</b>",
        "",
        f"🔑 Deepgram: <b>{len(rows)}</b> key(s), "
        f"<b>{alive}</b> active · balance <b>${total:.2f}</b>",
        f"🖥 Agent overrides active: <b>{len(overrides)}</b> "
        "(applied to ALL agents within ~20s)",
        "",
        "Choose a section:",
    ]
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔑 Deepgram API — {len(rows)} keys · ${total:.2f}",
            callback_data="camp:dg",
        )],
        [InlineKeyboardButton(
            "🖥 Agents settings (all agents)", callback_data="camp:agentset",
        )],
        [InlineKeyboardButton(
            "📦 Fleet update (git pull + restart)", callback_data="camp:update",
        )],
        _back(),
    ])
    return "\n".join(lines), markup


async def _update_payload(context) -> tuple:
    """Confirm screen for the fleet-wide git update."""
    from infra.updater import available, current_branch, current_version

    bus = context.bot_data.get("bus")
    lines = ["📦 <b>Fleet update</b>", ""]
    if available():
        lines.append(
            f"This controller: <code>{current_version()}</code> "
            f"(branch <code>{_esc(current_branch())}</code>)"
        )
    else:
        lines.append("⚠️ This controller is not a git checkout.")
    lines += [
        "",
        "On confirm:",
        "1. This controller runs <code>git pull</code>,",
        "2. an update signal is pushed to Redis,",
        "3. every agent pulls the latest commit and restarts itself "
        "within ~40s.",
        "",
        "Per-server .env / profiles / storage are NOT touched. "
        "New versions show up in 🖥 Agents afterwards.",
    ]
    if bus is None:
        lines.append("\n⚠️ Standalone mode: only this node will be updated.")
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Update everything now", callback_data="camp:update:go")],
        _back("camp:set"),
    ])
    return "\n".join(lines), markup


async def _run_fleet_update(update, context, query):
    """Pull on the controller, then broadcast the signal to all agents."""
    from infra.updater import apply_update, available, current_version

    lines = ["📦 <b>Fleet update</b>", ""]
    if available():
        loop = asyncio.get_running_loop()
        ok, detail = await loop.run_in_executor(None, apply_update, "")
        icon = "✅" if ok else "⚠️"
        lines.append(f"{icon} Controller: {_esc(detail)}")
    else:
        lines.append("⚠️ Controller: not a git checkout — skipped self-pull.")

    bus = context.bot_data.get("bus")
    if bus is not None:
        try:
            await bus.set_update_signal({"ref": ""})
            lines += [
                "",
                "📡 Signal sent. Agents pull and restart within ~40s — "
                "watch versions change in 🖥 Agents.",
            ]
        except Exception as e:
            lines.append(f"\n❌ Redis signal failed: {_esc(e)}")
    else:
        lines.append(
            "\nℹ️ Standalone: restart this bot to run the new code."
        )
    await query.message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Back to settings", callback_data="camp:set"),
        ]]),
    )


async def _agents_settings_payload(context, page: int = 0) -> tuple:
    """Remote settings pushed to every connected agent (Deepgram keys live
    in their own 🔑 section)."""
    from infra.remote_settings import SETTABLE

    overrides = await _load_settings(context)
    names = [n for n in SETTABLE.keys() if SETTABLE[n][0] != "keys"]
    pages = max(1, (len(names) + _SET_PAGE - 1) // _SET_PAGE)
    page = max(0, min(page, pages - 1))

    lines = [
        "🖥 <b>Agents settings</b>",
        "",
        "Every change is pushed to <b>all agents</b> and applied live "
        "within ~20s — no restart needed.",
        "🔄 = only takes effect after an agent restart.",
        "",
        "Tap a setting to change it:",
    ]
    rows = []
    for name in names[page * _SET_PAGE:(page + 1) * _SET_PAGE]:
        disp, is_override = _setting_current_display(name, overrides)
        mark = "•" if is_override else ""
        restart = " 🔄" if SETTABLE[name][1] else ""
        emoji = SETTABLE[name][2] if len(SETTABLE[name]) > 2 else "⚙️"
        label = f"{emoji} {mark}{name} = {disp}{restart}"
        rows.append([InlineKeyboardButton(
            label[:62], callback_data=f"camp:setone:{name}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"camp:setpage:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"camp:setpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(_back("camp:set"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _setting_detail_payload(context, name: str) -> tuple:
    from infra.remote_settings import SETTABLE

    typ, restart, *_ = SETTABLE[name]
    emoji = SETTABLE[name][2] if len(SETTABLE[name]) > 2 else "⚙️"
    overrides = await _load_settings(context)
    disp, is_override = _setting_current_display(name, overrides)

    context.bot_data["settings_pending"] = name

    hints = {
        "int": "an integer (e.g. <code>240</code>)",
        "float": "a number (e.g. <code>12.5</code>)",
        "bool": "<code>1</code>/<code>0</code> or <code>true</code>/<code>false</code>",
        "str": "plain text",
        "keys": (
            "one or several Deepgram API keys separated by commas, spaces "
            "or new lines — every key is verified online before saving"
        ),
    }
    lines = [
        f"{emoji} <b>{_esc(name)}</b>",
        "",
        f"Current value: <code>{_esc(disp)}</code>"
        + (" (remote override)" if is_override else " (default/env)"),
        f"Type: {hints.get(typ, 'text')}",
    ]
    if restart:
        lines.append("🔄 Takes effect only after an agent restart.")
    elif typ == "keys":
        lines.append(
            "Rotation is automatic: a key rejected by Deepgram (401/403 or "
            "quota) is parked and the next key is used — for TTS, STT and "
            "the Voice Agent alike."
        )
    lines += [
        "",
        "✏️ <b>Send the new value as a chat message now</b> "
        "(it will not be executed as a command).",
    ]
    rows = [
        [InlineKeyboardButton("❌ Cancel", callback_data="camp:setcancel")],
        _back("camp:set"),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── Deepgram key pool screen ──────────────────────────────────────────────────

_DG_CACHE_SEC = 60


def _dg_current_keys(overrides: dict) -> list:
    """Effective key list: remote override wins, else the local pool/env."""
    raw = overrides.get("DEEPGRAM_API_KEYS")
    if raw:
        from infra.remote_settings import parse_value
        try:
            return parse_value("DEEPGRAM_API_KEYS", raw)
        except ValueError:
            return []
    from infra.dg_keys import get_pool
    return get_pool().keys()


async def _dg_key_report(client, key: str) -> tuple:
    """(key, status, balance, note) — status: ok / dead / unknown.

    ok carries the remaining balance in USD (summed over project balances);
    dead means Deepgram itself rejected the key (401/403).
    """
    try:
        r = await client.get(
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {key}"},
        )
        if r.status_code in (401, 403):
            return (key, "dead", None, f"rejected ({r.status_code})")
        if r.status_code != 200:
            return (key, "unknown", None, f"HTTP {r.status_code}")
        projects = (r.json() or {}).get("projects") or []
        if not projects:
            return (key, "ok", 0.0, "no projects")
        pid = projects[0].get("project_id")
        rb = await client.get(
            f"https://api.deepgram.com/v1/projects/{pid}/balances",
            headers={"Authorization": f"Token {key}"},
        )
        if rb.status_code != 200:
            return (key, "unknown", None, f"balances HTTP {rb.status_code}")
        balances = (rb.json() or {}).get("balances") or []
        total = sum(float(b.get("amount", 0) or 0) for b in balances)
        return (key, "ok", total, "ok")
    except Exception as e:
        return (key, "unknown", None, f"check failed ({type(e).__name__})")


async def _dg_report(context, overrides: dict, force: bool = False) -> list:
    """Report rows for every key, cached for a minute (screen renders often)."""
    cache = context.bot_data.get("dg_report_cache")
    if not force and cache and time.time() - cache.get("ts", 0) < _DG_CACHE_SEC:
        return cache["rows"]
    keys = _dg_current_keys(overrides)
    if not keys:
        return []
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        rows = list(await asyncio.gather(
            *[_dg_key_report(client, k) for k in keys]
        ))
    context.bot_data["dg_report_cache"] = {"ts": time.time(), "rows": rows}
    return rows


async def _dg_save_keys(context, keys: list) -> str:
    """Persist the pool (or delete the override when empty). Returns where."""
    context.bot_data.pop("dg_report_cache", None)
    bus = context.bot_data.get("bus")
    if bus is not None:
        if keys:
            await bus.save_settings({"DEEPGRAM_API_KEYS": " ".join(keys)})
        else:
            await bus.delete_setting("DEEPGRAM_API_KEYS")
        return "all agents (applied within ~20s)"
    from infra.remote_settings import apply_settings, save_local_setting
    save_local_setting("DEEPGRAM_API_KEYS", " ".join(keys))
    import config as _cfg
    if keys:
        apply_settings(_cfg, {"DEEPGRAM_API_KEYS": " ".join(keys)})
    else:
        from infra.dg_keys import get_pool
        get_pool().set_keys(
            list(getattr(_cfg, "DEEPGRAM_API_KEYS", None) or [])
        )
    return "this standalone node (applied immediately)"


async def _dg_payload(context, force: bool = False) -> tuple:
    from infra.dg_keys import mask_key

    overrides = await _load_settings(context)
    rows = await _dg_report(context, overrides, force=force)
    total = sum(b for _, st, b, _ in rows if b)
    alive = sum(1 for _, st, b, _ in rows if st == "ok" and (b or 0) > 0)

    lines = [
        "🔑 <b>Deepgram API</b>",
        "",
        f"Keys: <b>{len(rows)}</b> · active: <b>{alive}</b> · "
        f"total balance: <b>${total:.2f}</b>",
        "",
    ]
    if not rows:
        lines.append("No keys yet — add some below.")
    for key, st, bal, note in rows:
        mask = _esc(mask_key(key))
        if st == "ok":
            icon = "✅" if (bal or 0) > 0 else "🪫"
            lines.append(f"{icon} <code>{mask}</code> — ${bal:.2f}")
        elif st == "dead":
            lines.append(f"❌ <code>{mask}</code> — {_esc(note)}")
        else:
            lines.append(f"⚠️ <code>{mask}</code> — {_esc(note)}")
    lines += [
        "",
        "Rotation is automatic: a rejected key is parked and agents switch "
        "to the next one (TTS, STT, Voice Agent).",
    ]
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add keys", callback_data="camp:dg:add")],
        [
            InlineKeyboardButton("🔄 Refresh balances", callback_data="camp:dg:refresh"),
            InlineKeyboardButton("🧹 Remove dead/spent", callback_data="camp:dg:clean"),
        ],
        _back("camp:set"),
    ])
    return "\n".join(lines), markup


async def _dg_add_payload(context) -> tuple:
    context.bot_data["settings_pending"] = "DEEPGRAM_API_KEYS"
    lines = [
        "🔑 <b>Add Deepgram keys</b>",
        "",
        "Send one or several API keys separated by commas, spaces or new "
        "lines. Every key is verified online (validity + balance) and "
        "<b>merged</b> with the existing pool — dead and $0 keys are "
        "dropped automatically.",
    ]
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="camp:setcancel")],
        _back("camp:dg"),
    ])
    return "\n".join(lines), markup


async def _dg_cleanup(update, context, query):
    """Drop keys Deepgram rejects outright and keys with a $0 balance."""
    overrides = await _load_settings(context)
    rows = await _dg_report(context, overrides, force=True)
    if not rows:
        await query.answer("No keys to clean.", show_alert=True)
        return

    keep, dropped = [], []
    for key, st, bal, note in rows:
        if st == "dead":
            dropped.append((key, note))
        elif st == "ok" and not (bal or 0) > 0:
            dropped.append((key, "balance $0"))
        else:
            keep.append(key)  # ok-with-balance + unverifiable

    try:
        where = await _dg_save_keys(context, keep)
    except Exception as e:
        await query.answer(f"Save failed: {str(e)[:100]}", show_alert=True)
        return

    from infra.dg_keys import mask_key

    lines = [f"🧹 Cleanup done → {where}.", ""]
    if dropped:
        lines.append(f"Removed <b>{len(dropped)}</b> key(s):")
        for key, why in dropped:
            lines.append(f"  ❌ <code>{_esc(mask_key(key))}</code> — {_esc(why)}")
    else:
        lines.append("Nothing to remove — all keys are alive and funded.")
    lines.append(f"\nRemaining: <b>{len(keep)}</b> key(s).")
    await query.message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔑 Back to Deepgram", callback_data="camp:dg"),
        ]]),
    )


async def settings_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture the value message after a setting was tapped.

    Registered in group -1 so it runs before any ConversationHandler; when
    no setting is pending it returns immediately and touches nothing.
    """
    name = context.bot_data.get("settings_pending")
    if not name or not update.message or not (update.message.text or "").strip():
        return
    context.bot_data.pop("settings_pending", None)
    raw = update.message.text.strip()

    from infra.remote_settings import SETTABLE, parse_value
    from infra.dg_keys import mask_key

    try:
        value = parse_value(name, raw)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ {_esc(e)}\nChange cancelled — open ⚙️ Settings to retry.",
            parse_mode="HTML",
        )
        raise ApplicationHandlerStop

    report_lines = []
    if SETTABLE[name][0] == "keys":
        # Merge the new keys with the existing pool, then verify every key
        # online: dead (401/403) and $0-balance keys are dropped on the spot.
        overrides = await _load_settings(context)
        existing = _dg_current_keys(overrides)
        merged = list(dict.fromkeys(existing + list(value)))
        new_set = set(value)

        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            checks = list(await asyncio.gather(
                *[_dg_key_report(client, k) for k in merged]
            ))
        keep = []
        total = 0.0
        for k, st, bal, note in checks:
            mask = _esc(mask_key(k))
            added = " 🆕" if k in new_set else ""
            if st == "ok":
                if (bal or 0) > 0:
                    report_lines.append(f"✅ <code>{mask}</code> — ${bal:.2f}{added}")
                    keep.append(k)
                    total += bal
                else:
                    report_lines.append(f"🪫 <code>{mask}</code> — $0, dropped{added}")
            elif st == "dead":
                report_lines.append(f"❌ <code>{mask}</code> — {_esc(note)}, dropped{added}")
            else:
                report_lines.append(f"⚠️ <code>{mask}</code> — {_esc(note)}, kept{added}")
                keep.append(k)  # unverifiable (network) keys are kept
        if not keep:
            await update.message.reply_text(
                "❌ No usable keys — nothing saved.\n" + "\n".join(report_lines),
                parse_mode="HTML",
            )
            raise ApplicationHandlerStop
        try:
            where = await _dg_save_keys(context, keep)
        except Exception as e:
            await update.message.reply_text(f"❌ Save failed: {_esc(e)}",
                                            parse_mode="HTML")
            raise ApplicationHandlerStop
        lines = [
            f"✅ Deepgram pool updated → {where}.",
            f"Keys: <b>{len(keep)}</b> · total balance: <b>${total:.2f}</b>",
            "",
        ] + report_lines
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔑 Back to Deepgram", callback_data="camp:dg"),
            ]]),
        )
        raise ApplicationHandlerStop

    stored = str(value)

    bus = context.bot_data.get("bus")
    if bus is not None:
        try:
            await bus.save_settings({name: stored})
            where = "all agents (applied within ~20s)"
        except Exception as e:
            await update.message.reply_text(f"❌ Redis save failed: {_esc(e)}",
                                            parse_mode="HTML")
            raise ApplicationHandlerStop
    else:
        from infra.remote_settings import apply_settings, save_local_setting
        save_local_setting(name, stored)
        import config as _cfg
        apply_settings(_cfg, {name: stored})
        where = "this standalone node (applied immediately)"

    typ, restart, *_ = SETTABLE[name]
    lines = [f"✅ <b>{_esc(name)}</b> updated → {where}."]
    if restart:
        lines.append("🔄 This one also needs an agent restart to take effect.")
    lines += report_lines
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Back to settings", callback_data="camp:set"),
        ]]),
    )
    raise ApplicationHandlerStop


# ── handlers ──────────────────────────────────────────────────────────────────

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = await _home_payload(context)
    await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=markup,
        disable_web_page_preview=True,
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    route = parts[1] if len(parts) > 1 else "home"

    try:
        await query.answer()
    except Exception:
        pass

    try:
        if route == "home":
            text, markup = await _home_payload(context)
            await _edit(query, text, markup)

        elif route == "campaigns":
            text, markup = await _campaigns_payload(context)
            await _edit(query, text, markup)

        elif route == "live":
            bus = context.bot_data.get("bus")
            session = await bus.get_p1_viewable_session() if bus else None
            if not session:
                await query.answer("No active campaign.", show_alert=True)
                return
            from bot.commands.p1_live import live_keyboard
            kb = live_keyboard("overview", auto=False)
            kb = InlineKeyboardMarkup(
                list(kb.inline_keyboard) + [_back("camp:campaigns")]
            )
            await _edit(
                query, format_dashboard(session, "overview"), kb,
            )

        elif route == "stop":
            await _stop_campaign(update, context, query)
            text, markup = await _campaigns_payload(context)
            await _edit(query, text, markup)

        elif route == "results":
            text, markup = await _results_payload(context)
            await _edit(query, text, markup)

        elif route == "res" and len(parts) > 2:
            text, markup = await _campaign_detail_payload(context, parts[2])
            await _edit(query, text, markup)

        elif route == "exp" and len(parts) > 3:
            await _export_campaign(update, context, query, parts[2], parts[3])

        elif route == "redial" and len(parts) > 3:
            await _redial_campaign(update, context, query, parts[2], parts[3])

        elif route == "scripts":
            text, markup = await _scripts_payload(context)
            await _edit(query, text, markup)

        elif route == "script" and len(parts) > 2:
            await _select_script(context, query, parts[2])
            text, markup = await _scripts_payload(context)
            await _edit(query, text, markup)

        elif route == "agents":
            from bot.commands.status import status_command
            await status_command(_shim(update, query), context)

        elif route == "sip":
            from bot.commands.siptest import handle_sip_callback
            await handle_sip_callback(update, context, query, parts)

        elif route == "set":
            text, markup = await _settings_home_payload(context)
            await _edit(query, text, markup)

        elif route == "agentset":
            text, markup = await _agents_settings_payload(context, 0)
            await _edit(query, text, markup)

        elif route == "setpage" and len(parts) > 2:
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
            text, markup = await _agents_settings_payload(context, page)
            await _edit(query, text, markup)

        elif route == "setone" and len(parts) > 2:
            from infra.remote_settings import SETTABLE
            name = parts[2]
            if name not in SETTABLE:
                await query.answer("Unknown setting.", show_alert=True)
                return
            text, markup = await _setting_detail_payload(context, name)
            await _edit(query, text, markup)

        elif route == "update":
            sub = parts[2] if len(parts) > 2 else ""
            if sub == "go":
                await _run_fleet_update(update, context, query)
            else:
                text, markup = await _update_payload(context)
                await _edit(query, text, markup)

        elif route == "dg":
            sub = parts[2] if len(parts) > 2 else ""
            if sub == "add":
                text, markup = await _dg_add_payload(context)
                await _edit(query, text, markup)
            elif sub == "refresh":
                text, markup = await _dg_payload(context, force=True)
                await _edit(query, text, markup)
            elif sub == "clean":
                await _dg_cleanup(update, context, query)
                text, markup = await _dg_payload(context, force=True)
                await _edit(query, text, markup)
            else:
                text, markup = await _dg_payload(context)
                await _edit(query, text, markup)

        elif route == "setcancel":
            pending = context.bot_data.pop("settings_pending", None)
            if pending == "DEEPGRAM_API_KEYS":
                text, markup = await _dg_payload(context)
            else:
                text, markup = await _settings_home_payload(context)
            await _edit(query, text, markup)

        elif route == "spam":
            if len(parts) > 3 and parts[2] == "run":
                mode_arg = parts[3]
                context.args = (
                    ["debug"] if mode_arg == "debug"
                    else ["seq"] if mode_arg == "seq"
                    else []
                )
                from bot.commands.spamcheck import spamcheck_command
                await spamcheck_command(_shim(update, query), context)
            else:
                text, markup = _spam_payload()
                await _edit(query, text, markup)

        else:
            await query.answer("Unknown action.", show_alert=True)
    except Exception as e:
        logger.exception(f"menu callback failed ({data})")
        try:
            await query.answer(f"Error: {str(e)[:120]}", show_alert=True)
        except Exception:
            pass


async def _pending_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single group -1 text handler for all pending-input flows.

    PTB runs only the FIRST matching handler per group — registering
    settings_text_input and sip_text_input side by side meant the settings
    one swallowed every message (even with nothing pending) and the SIP
    number prompt never fired. One wrapper dispatches by pending flag.
    """
    if context.bot_data.get("sip_pending"):
        from bot.commands.siptest import sip_text_input
        await sip_text_input(update, context)
        return
    await settings_text_input(update, context)


def register_menu_handlers(application):
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^camp:"))
    # Pending-input capture (settings values, SIP test number) — group -1 so
    # it runs before conversations and stops propagation only when a pending
    # input is actually consumed.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _pending_text_input),
        group=-1,
    )
    logger.info("Registered menu handlers")
