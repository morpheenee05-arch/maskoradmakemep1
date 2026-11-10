"""
/wipes — inline archive of vacated / unhealthy Google Voice slots.

Each agent keeps screenshots + index.json on disk. The controller pulls a
summary, then this UI lets the operator browse by server, by Google account,
and by cause, open a full event card, and fetch that event's screenshot.
"""
from __future__ import annotations

import base64
import html
import io
import time
from typing import Optional

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config

ARCHIVE_LIMIT = 80
PAGE_SIZE = 6
ACCOUNT_PAGE = 8
_SESSION_KEY = "wipe_archive"

_KIND = {
    "banned": ("🚫", "Banned"),
    "captcha": ("🤖", "Captcha"),
    "spam": ("🚨", "Spam"),
    "expired": ("💨", "Session"),
    "wipe": ("🧹", "Other"),
}
_KIND_ORDER = ("banned", "captcha", "spam", "expired", "wipe")
_FILTER_CB = {
    "all": "*",
    "banned": "b",
    "captcha": "c",
    "spam": "s",
    "expired": "e",
    "wipe": "w",
}
_FILTER_FROM_CB = {v: k for k, v in _FILTER_CB.items()}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def _kind_meta(kind: str) -> tuple[str, str]:
    return _KIND.get(kind or "wipe", ("🧹", kind or "wipe"))


def _short(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "…"


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(_short(label, 64), callback_data=data[:64])


def _session(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(_SESSION_KEY, {})


def _identity(ev: dict) -> tuple[str, str]:
    email = (ev.get("email") or "").strip()
    if email:
        return "email", email
    phone = (ev.get("phone") or "").strip()
    if phone:
        return "phone", phone
    profile = (ev.get("profile") or "").strip()
    agent = ev.get("_agent") or "?"
    if profile:
        return "profile", f"{agent}/{profile}"
    return "slot", f"{agent}/W{ev.get('worker_id', '?')}"


def _identity_label(ev: dict) -> str:
    kind, key = _identity(ev)
    if kind == "email":
        return key
    if kind == "phone":
        return key
    if kind == "profile":
        return key.split("/", 1)[-1]
    return key


def _flatten(reports: dict, pending: set) -> list:
    rows = []
    for aid, msg in (reports or {}).items():
        for ev in msg.get("events") or []:
            row = dict(ev)
            row["_agent"] = aid
            rows.append(row)
    rows.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)
    for i, row in enumerate(rows):
        row["_ord"] = i
    return rows


def _apply_filter(rows: list, kind: str) -> list:
    if not kind or kind == "all":
        return list(rows)
    return [r for r in rows if (r.get("kind") or "wipe") == kind]


def _counts(rows: list) -> dict:
    out = {k: 0 for k in _KIND_ORDER}
    for r in rows:
        k = r.get("kind") or "wipe"
        out[k] = out.get(k, 0) + 1
    return out


def _group_accounts(rows: list) -> list:
    buckets: dict[str, dict] = {}
    order = []
    for ev in rows:
        _, key = _identity(ev)
        ident = key.lower() if "@" in key else key
        bucket = buckets.get(ident)
        if bucket is None:
            bucket = {
                "id": ident,
                "label": _identity_label(ev),
                "events": [],
                "email": ev.get("email") or "",
                "phone": ev.get("phone") or "",
                "profile": ev.get("profile") or "",
            }
            buckets[ident] = bucket
            order.append(ident)
        bucket["events"].append(ev)
        if ev.get("email") and not bucket["email"]:
            bucket["email"] = ev["email"]
        if ev.get("phone") and not bucket["phone"]:
            bucket["phone"] = ev["phone"]
        if ev.get("profile") and not bucket["profile"]:
            bucket["profile"] = ev["profile"]
    grouped = [buckets[k] for k in order]
    grouped.sort(key=lambda g: float((g["events"][0].get("ts") or 0)), reverse=True)
    return grouped


def _agents_of(rows: list) -> list:
    seen = []
    for r in rows:
        aid = r.get("_agent")
        if aid and aid not in seen:
            seen.append(aid)
    return seen


def _when(ev: dict) -> str:
    return ev.get("when") or "—"


def _clock(ev: dict) -> str:
    when = _when(ev)
    return when[11:16] if len(when) >= 16 else when


# ── Fetch ─────────────────────────────────────────────────────────────────────


async def _fetch_local(limit: int) -> tuple[dict, set]:
    from telephony import wipe_archive

    aid = getattr(config, "AGENT_ID", None) or "local"
    try:
        events = wipe_archive.list_events(limit=limit)
        return {
            aid: {
                "events": events,
                "dir": str(wipe_archive.archive_dir()),
                "error": None,
            }
        }, set()
    except Exception as e:
        return {aid: {"events": [], "error": str(e)}}, set()


async def _fetch_agents(bus, limit: int) -> tuple[dict, set]:
    from bot.commands.accounts import _agent_ids

    aids = await _agent_ids(bus)
    if not aids:
        return {}, set()
    group = await bus.new_results_group()
    job_owner = {}
    pending = set()
    reports = {}
    try:
        for aid in aids:
            jid = await bus.dispatch_job(aid, {
                "type": "wipe_history",
                "params": {"limit": limit, "photo": False},
            })
            job_owner[jid] = aid
            pending.add(aid)
        deadline = time.time() + 40
        while pending and time.time() < deadline:
            for entry_id, msg in await bus.consume_results(group, block_ms=1500):
                try:
                    await bus.ack_result(group, entry_id)
                    if msg.get("job_id") not in job_owner:
                        continue
                    aid = msg.get("agent_id") or job_owner[msg["job_id"]]
                    kind = msg.get("kind")
                    if kind == "wipe_history":
                        reports[aid] = msg
                    elif kind == "done":
                        pending.discard(aid)
                except Exception as e:
                    logger.warning(f"/wipes result error: {e}")
        for aid in aids:
            reports.setdefault(aid, {"events": [], "error": "no reply"})
    finally:
        await bus.destroy_results_group(group)
    return reports, pending


async def _load_archive(context: ContextTypes.DEFAULT_TYPE) -> dict:
    bus = context.bot_data.get("bus")
    if bus is None:
        reports, pending = await _fetch_local(ARCHIVE_LIMIT)
    else:
        reports, pending = await _fetch_agents(bus, ARCHIVE_LIMIT)
    rows = _flatten(reports, pending)
    sess = {
        "reports": reports,
        "pending": sorted(pending),
        "rows": rows,
        "tab": "servers",
        "filter": "all",
        "page": 0,
        "acct_page": 0,
        "fetched_at": time.strftime("%H:%M:%S"),
        "online": bus is not None,
    }
    context.user_data[_SESSION_KEY] = sess
    return sess


# ── Render ────────────────────────────────────────────────────────────────────


def _filter_bar(kind: str) -> str:
    if kind and kind != "all":
        icon, label = _kind_meta(kind)
        return f"Filter: {icon} <b>{_esc(label)}</b>\n"
    return ""


def _home_text(sess: dict) -> str:
    rows = _apply_filter(sess.get("rows") or [], sess.get("filter") or "all")
    all_rows = sess.get("rows") or []
    counts = _counts(all_rows)
    pending = sess.get("pending") or []
    tab = sess.get("tab") or "servers"
    n_agents = len(sess.get("reports") or {})
    n_accts = len(_group_accounts(rows))
    lines = [
        "🗂 <b>GV wipe archive</b>",
        f"<i>Updated { _esc(sess.get('fetched_at') or '—') }"
        f" · {len(all_rows)} event(s) · {n_agents} server(s)</i>",
        _filter_bar(sess.get("filter")),
    ]
    if pending:
        lines.append(
            f"⏱️ No reply: <code>{_esc(', '.join(pending))}</code>"
        )
    cause_bits = []
    for k in _KIND_ORDER:
        if counts.get(k):
            icon, label = _kind_meta(k)
            cause_bits.append(f"{icon} {counts[k]}")
    lines.append(" · ".join(cause_bits) if cause_bits else "<i>No wipe events stored yet.</i>")
    lines.append("")

    if tab == "causes":
        lines.append("<b>By cause</b>")
        for k in _KIND_ORDER:
            icon, label = _kind_meta(k)
            n = counts.get(k) or 0
            mark = " •" if sess.get("filter") == k else ""
            lines.append(f"  {icon} <b>{_esc(label)}</b> — {n}{mark}")
        lines.append(f"  📦 All — {len(all_rows)}")
    elif tab == "accounts":
        groups = _group_accounts(rows)
        page = max(0, int(sess.get("acct_page") or 0))
        chunk = groups[page * ACCOUNT_PAGE:(page + 1) * ACCOUNT_PAGE]
        pages = max(1, (len(groups) + ACCOUNT_PAGE - 1) // ACCOUNT_PAGE)
        lines.append(
            f"<b>By Google account</b>  ·  {n_accts} identity(ies)"
            f"  ·  {page + 1}/{pages}"
        )
        if not chunk:
            lines.append("<i>Nothing in this filter.</i>")
        for i, g in enumerate(chunk, start=page * ACCOUNT_PAGE + 1):
            last = g["events"][0]
            icon, _ = _kind_meta(last.get("kind"))
            agents = sorted({e.get("_agent") for e in g["events"] if e.get("_agent")})
            lines.append(
                f"\n{i}. {icon} <b>{_esc(g['label'])}</b>  ×{len(g['events'])}"
            )
            bits = []
            if g.get("phone"):
                bits.append(f"<code>{_esc(g['phone'])}</code>")
            if g.get("profile"):
                bits.append(f"<code>{_esc(g['profile'])}</code>")
            bits.append(_esc(", ".join(agents)) if agents else "—")
            lines.append("   " + " · ".join(bits))
            lines.append(
                f"   last {_esc(_when(last))} · {_esc(last.get('reason') or '')}"
            )
    else:
        reports = sess.get("reports") or {}
        lines.append("<b>By server</b>")
        if not reports:
            lines.append("<i>No agents reported.</i>")
        for aid in sorted(reports):
            msg = reports[aid] or {}
            evs = _apply_filter(
                [r for r in all_rows if r.get("_agent") == aid],
                sess.get("filter") or "all",
            )
            err = msg.get("error")
            last = evs[0] if evs else None
            head = f"🖥 <b>{_esc(aid)}</b>  —  {len(evs)}"
            if err:
                head += f"  ⚠️ {_esc(err)}"
            lines.append(head)
            if last:
                icon, _ = _kind_meta(last.get("kind"))
                who = _identity_label(last)
                lines.append(
                    f"   {icon} {_esc(_when(last))} · W{last.get('worker_id')} "
                    f"<code>{_esc(who)}</code>"
                )
                lines.append(f"   {_esc(_short(last.get('reason') or '', 90))}")
            elif not err:
                lines.append("   <i>no wipes on this agent</i>")
    return "\n".join(lines).strip()


def _home_keyboard(sess: dict) -> InlineKeyboardMarkup:
    tab = sess.get("tab") or "servers"
    filt = sess.get("filter") or "all"
    rows_all = sess.get("rows") or []
    rows = _apply_filter(rows_all, filt)
    counts = _counts(rows_all)

    def tab_btn(label, key):
        mark = "• " if tab == key else ""
        return _btn(f"{mark}{label}", f"wipe:t:{key[0]}")

    kb = [[
        tab_btn("🖥 Servers", "servers"),
        tab_btn("👤 Accounts", "accounts"),
        tab_btn("🏷 Causes", "causes"),
    ]]
    cause_row = []
    for k in _KIND_ORDER:
        n = counts.get(k) or 0
        if not n:
            continue
        icon, _ = _kind_meta(k)
        mark = "·" if filt == k else ""
        cause_row.append(_btn(f"{mark}{icon}{n}", f"wipe:f:{_FILTER_CB[k]}"))
    if cause_row:
        all_mark = "·" if filt == "all" else ""
        cause_row.append(_btn(f"{all_mark}All {len(rows_all)}", "wipe:f:*"))
        for i in range(0, len(cause_row), 3):
            kb.append(cause_row[i:i + 3])

    if tab == "accounts":
        groups = _group_accounts(rows)
        page = max(0, int(sess.get("acct_page") or 0))
        chunk = groups[page * ACCOUNT_PAGE:(page + 1) * ACCOUNT_PAGE]
        for i, g in enumerate(chunk):
            idx = page * ACCOUNT_PAGE + i
            last = g["events"][0]
            icon, _ = _kind_meta(last.get("kind"))
            kb.append([_btn(
                f"{icon} {g['label']}  ×{len(g['events'])}",
                f"wipe:u:{idx}",
            )])
        pages = max(1, (len(groups) + ACCOUNT_PAGE - 1) // ACCOUNT_PAGE)
        if pages > 1:
            kb.append(_pager("wipe:up", page, pages))
    elif tab == "causes":
        for k in _KIND_ORDER:
            n = counts.get(k) or 0
            icon, label = _kind_meta(k)
            kb.append([_btn(f"{icon} {label}  ({n})", f"wipe:f:{_FILTER_CB[k]}")])
    else:
        reports = sess.get("reports") or {}
        row = []
        for i, aid in enumerate(sorted(reports)):
            n = sum(1 for r in rows if r.get("_agent") == aid)
            row.append(_btn(f"{aid}  {n}", f"wipe:a:{i}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)

    kb.append([_btn("🔄 Refresh", "wipe:r")])
    return InlineKeyboardMarkup(kb)


def _pager(prefix: str, page: int, pages: int) -> list:
    return [
        _btn("«", f"{prefix}:{max(0, page - 1)}"),
        _btn(f"{page + 1}/{pages}", f"{prefix}:{page}"),
        _btn("»", f"{prefix}:{min(pages - 1, page + 1)}"),
    ]


def _server_text(sess: dict, aid: str) -> str:
    filt = sess.get("filter") or "all"
    rows = [r for r in _apply_filter(sess.get("rows") or [], filt) if r.get("_agent") == aid]
    msg = (sess.get("reports") or {}).get(aid) or {}
    page = max(0, int(sess.get("page") or 0))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    lines = [
        f"🖥 <b>{_esc(aid)}</b>",
        f"<i>{len(rows)} event(s) · newest first · page {page + 1}/{pages}</i>",
        _filter_bar(filt),
    ]
    if msg.get("error"):
        lines.append(f"⚠️ {_esc(msg['error'])}")
    if msg.get("dir"):
        lines.append(f"📁 <code>{_esc(msg['dir'])}</code>")
    if not chunk:
        lines.append("\n<i>No events on this server for the current filter.</i>")
        return "\n".join(lines)
    for n, ev in enumerate(chunk, start=page * PAGE_SIZE + 1):
        icon, label = _kind_meta(ev.get("kind"))
        shot = "📷" if ev.get("screenshot") else "—"
        lines.append(
            f"\n<b>{n}.</b> {icon} <b>{_esc(label)}</b>  {_esc(_when(ev))}  {shot}"
        )
        lines.append(
            f"   W{ev.get('worker_id')} · <code>{_esc(_identity_label(ev))}</code>"
        )
        if ev.get("phone"):
            lines.append(f"   tel <code>{_esc(ev['phone'])}</code>")
        if ev.get("profile"):
            lines.append(f"   profile <code>{_esc(ev['profile'])}</code>")
        lines.append(f"   {_esc(ev.get('reason') or '')}")
    return "\n".join(lines)


def _server_keyboard(sess: dict, aid: str) -> InlineKeyboardMarkup:
    filt = sess.get("filter") or "all"
    rows = [r for r in _apply_filter(sess.get("rows") or [], filt) if r.get("_agent") == aid]
    page = max(0, int(sess.get("page") or 0))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    kb = []
    for ev in chunk:
        icon, _ = _kind_meta(ev.get("kind"))
        shot = "📷" if ev.get("screenshot") else ""
        kb.append([_btn(
            f"{icon} {_clock(ev)} W{ev.get('worker_id')} {_identity_label(ev)} {shot}".strip(),
            f"wipe:e:{ev.get('_ord', 0)}",
        )])
    if pages > 1:
        kb.append(_pager("wipe:sp", page, pages))
    kb.append([_btn("◀️ Archive", "wipe:h"), _btn("🔄 Refresh", "wipe:r")])
    return InlineKeyboardMarkup(kb)


def _account_text(sess: dict, group: dict) -> str:
    evs = group["events"]
    lines = [
        f"👤 <b>{_esc(group['label'])}</b>",
        f"<i>{len(evs)} wipe(s) for this identity · newest first</i>",
        "",
    ]
    if group.get("email"):
        lines.append(f"Account  <code>{_esc(group['email'])}</code>")
    if group.get("phone"):
        lines.append(f"Number   <code>{_esc(group['phone'])}</code>")
    if group.get("profile"):
        lines.append(f"Profile  <code>{_esc(group['profile'])}</code>")
    agents = sorted({e.get("_agent") for e in evs if e.get("_agent")})
    lines.append(f"Servers  {_esc(', '.join(agents))}")
    for n, ev in enumerate(evs[:20], start=1):
        icon, label = _kind_meta(ev.get("kind"))
        shot = "📷" if ev.get("screenshot") else "—"
        lines.append(
            f"\n<b>{n}.</b> {icon} {_esc(label)}  {_esc(_when(ev))}  {shot}"
        )
        lines.append(
            f"   {_esc(ev.get('_agent'))} · W{ev.get('worker_id')} · "
            f"<code>{_esc(ev.get('profile') or '—')}</code>"
        )
        lines.append(f"   {_esc(ev.get('reason') or '')}")
    if len(evs) > 20:
        lines.append(f"\n<i>…and {len(evs) - 20} older event(s)</i>")
    return "\n".join(lines)


def _account_keyboard(group: dict) -> InlineKeyboardMarkup:
    kb = []
    for ev in group["events"][:12]:
        icon, _ = _kind_meta(ev.get("kind"))
        shot = "📷" if ev.get("screenshot") else ""
        kb.append([_btn(
            f"{icon} {_clock(ev)} {ev.get('_agent')} W{ev.get('worker_id')} {shot}".strip(),
            f"wipe:e:{ev.get('_ord', 0)}",
        )])
    kb.append([_btn("◀️ Accounts", "wipe:t:a"), _btn("🏠 Archive", "wipe:h")])
    return InlineKeyboardMarkup(kb)


def _event_text(ev: dict, pos: int, total: int) -> str:
    icon, label = _kind_meta(ev.get("kind"))
    shot = ev.get("screenshot")
    lines = [
        f"{icon} <b>{_esc(label)}</b>  ·  {_esc(ev.get('_agent') or '—')}"
        f"  ·  #{pos} of {total}",
        "",
        f"<b>When</b>      {_esc(_when(ev))}",
        f"<b>Server</b>    {_esc(ev.get('_agent') or '—')}",
        f"<b>Worker</b>    W{_esc(ev.get('worker_id'))}",
        f"<b>Profile</b>   <code>{_esc(ev.get('profile') or '—')}</code>",
        f"<b>Account</b>   <code>{_esc(ev.get('email') or '—')}</code>",
        f"<b>Number</b>    <code>{_esc(ev.get('phone') or '—')}</code>",
        f"<b>State</b>     {_esc(ev.get('login_state') or '—')}",
        f"<b>Outcome</b>   {_esc(ev.get('login_outcome') or '—')}",
    ]
    if ev.get("login_detail"):
        lines.append(f"<b>Detail</b>    {_esc(ev['login_detail'])}")
    lines.append(f"<b>Cause</b>     {_esc(ev.get('reason') or '—')}")
    lines.append("")
    if ev.get("title"):
        lines.append(f"<b>Title</b>     {_esc(ev['title'])}")
    if ev.get("url"):
        url = ev["url"]
        lines.append(f"<b>URL</b>       <code>{_esc(_short(url, 180))}</code>")
    lines.append(
        f"<b>Screenshot</b>  {'yes · ' + _esc(shot) if shot else 'none captured'}"
    )
    lines.append(
        f"<b>Browser</b>   {'open at snapshot' if ev.get('has_browser') else 'already closed'}"
    )
    snippet = (ev.get("text") or "").strip()
    if snippet:
        lines.append("")
        lines.append("<b>Page text</b>")
        lines.append(f"<i>{_esc(_short(snippet, 500))}</i>")
    return "\n".join(lines)


def _event_keyboard(sess: dict, ev: dict) -> InlineKeyboardMarkup:
    rows = _apply_filter(sess.get("rows") or [], sess.get("filter") or "all")
    ords = [r.get("_ord") for r in rows]
    try:
        i = ords.index(ev.get("_ord"))
    except ValueError:
        i = 0
        rows = [ev]
    prev_ord = rows[i - 1].get("_ord") if i > 0 else None
    next_ord = rows[i + 1].get("_ord") if i + 1 < len(rows) else None
    nav = []
    if prev_ord is not None:
        nav.append(_btn("« Prev", f"wipe:e:{prev_ord}"))
    nav.append(_btn(f"{i + 1}/{len(rows)}", f"wipe:e:{ev.get('_ord', 0)}"))
    if next_ord is not None:
        nav.append(_btn("Next »", f"wipe:e:{next_ord}"))
    kb = []
    if ev.get("screenshot"):
        kb.append([_btn("📷 Screenshot", f"wipe:s:{ev.get('_ord', 0)}")])
    if nav:
        kb.append(nav)
    back = [_btn("🏠 Archive", "wipe:h")]
    aid = ev.get("_agent")
    if aid:
        aids = sorted(sess.get("reports") or {})
        if aid in aids:
            back.insert(0, _btn(f"◀️ {aid}", f"wipe:a:{aids.index(aid)}"))
    kb.append(back)
    return InlineKeyboardMarkup(kb)


async def _edit(query, text: str, kb: InlineKeyboardMarkup):
    try:
        await query.edit_message_text(
            text[:4000],
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.debug(f"wipes edit: {e}")


def _event_by_ord(sess: dict, ord_i: int) -> Optional[dict]:
    for r in sess.get("rows") or []:
        if r.get("_ord") == ord_i:
            return r
    return None


def _need_sess(context) -> Optional[dict]:
    sess = context.user_data.get(_SESSION_KEY)
    if sess and sess.get("rows") is not None:
        return sess
    return None


# ── Command / callbacks ───────────────────────────────────────────────────────


async def wipes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    wait = await msg.reply_text("⏳ Collecting wipe archive from agents…")
    try:
        sess = await _load_archive(context)
    except Exception as e:
        await wait.edit_text(f"❌ Could not load archive: {_esc(e)}", parse_mode="HTML")
        return
    try:
        await wait.edit_text(
            _home_text(sess),
            parse_mode="HTML",
            reply_markup=_home_keyboard(sess),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"/wipes render: {e}")
        await msg.reply_text(
            _home_text(sess),
            parse_mode="HTML",
            reply_markup=_home_keyboard(sess),
            disable_web_page_preview=True,
        )


async def wipes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "h"

    if action == "r":
        try:
            await query.answer("Refreshing…")
        except Exception:
            pass
        try:
            sess = await _load_archive(context)
        except Exception as e:
            await query.answer(str(e)[:180], show_alert=True)
            return
        await _edit(query, _home_text(sess), _home_keyboard(sess))
        return

    sess = _need_sess(context)
    if sess is None:
        try:
            await query.answer("Loading archive…")
        except Exception:
            pass
        try:
            sess = await _load_archive(context)
        except Exception as e:
            await query.answer(str(e)[:180], show_alert=True)
            return

    if action == "s":
        ord_i = int(parts[2]) if len(parts) > 2 else 0
        ev = _event_by_ord(sess, ord_i)
        if not ev or not ev.get("screenshot"):
            await query.answer("No screenshot stored for this event", show_alert=True)
            return
        await _send_event_photo(query, context, ev)
        return

    try:
        await query.answer()
    except Exception:
        pass

    if action in ("h", "home"):
        sess["tab"] = "servers"
        sess["page"] = 0
        await _edit(query, _home_text(sess), _home_keyboard(sess))
        return

    if action == "t":
        code = parts[2] if len(parts) > 2 else "s"
        sess["tab"] = {"s": "servers", "a": "accounts", "c": "causes"}.get(code, "servers")
        sess["page"] = 0
        sess["acct_page"] = 0
        await _edit(query, _home_text(sess), _home_keyboard(sess))
        return

    if action == "f":
        code = parts[2] if len(parts) > 2 else "*"
        sess["filter"] = _FILTER_FROM_CB.get(code, "all")
        sess["page"] = 0
        sess["acct_page"] = 0
        await _edit(query, _home_text(sess), _home_keyboard(sess))
        return

    if action == "up":
        sess["acct_page"] = max(0, int(parts[2]) if len(parts) > 2 else 0)
        sess["tab"] = "accounts"
        await _edit(query, _home_text(sess), _home_keyboard(sess))
        return

    if action == "a":
        aids = sorted(sess.get("reports") or {})
        idx = int(parts[2]) if len(parts) > 2 else 0
        if idx < 0 or idx >= len(aids):
            await _edit(query, _home_text(sess), _home_keyboard(sess))
            return
        sess["agent"] = aids[idx]
        sess["page"] = 0
        await _edit(query, _server_text(sess, aids[idx]), _server_keyboard(sess, aids[idx]))
        return

    if action == "sp":
        aid = sess.get("agent")
        sess["page"] = max(0, int(parts[2]) if len(parts) > 2 else 0)
        if not aid:
            await _edit(query, _home_text(sess), _home_keyboard(sess))
            return
        await _edit(query, _server_text(sess, aid), _server_keyboard(sess, aid))
        return

    if action == "u":
        filt = sess.get("filter") or "all"
        groups = _group_accounts(_apply_filter(sess.get("rows") or [], filt))
        idx = int(parts[2]) if len(parts) > 2 else 0
        if idx < 0 or idx >= len(groups):
            sess["tab"] = "accounts"
            await _edit(query, _home_text(sess), _home_keyboard(sess))
            return
        sess["account_idx"] = idx
        g = groups[idx]
        await _edit(query, _account_text(sess, g), _account_keyboard(g))
        return

    if action == "e":
        ord_i = int(parts[2]) if len(parts) > 2 else 0
        ev = _event_by_ord(sess, ord_i)
        if not ev:
            await query.answer("Event no longer in this snapshot — refresh", show_alert=True)
            await _edit(query, _home_text(sess), _home_keyboard(sess))
            return
        filt_rows = _apply_filter(sess.get("rows") or [], sess.get("filter") or "all")
        pos = next((i + 1 for i, r in enumerate(filt_rows) if r.get("_ord") == ord_i), 1)
        await _edit(query, _event_text(ev, pos, len(filt_rows)), _event_keyboard(sess, ev))
        return

    await _edit(query, _home_text(sess), _home_keyboard(sess))


async def _send_event_photo(query, context, ev: dict):
    name = ev.get("screenshot")
    aid = ev.get("_agent")
    caption = (
        f"{_kind_meta(ev.get('kind'))[0]} {aid} · W{ev.get('worker_id')} · "
        f"{_identity_label(ev)}\n{_when(ev)}\n{ev.get('reason') or ''}"
    )[:1024]
    bus = context.bot_data.get("bus")
    raw = None
    err = None
    if bus is None:
        from telephony import wipe_archive
        path = wipe_archive.screenshot_file(name)
        if path is None:
            err = "file not on this host"
        else:
            raw = path.read_bytes()
    else:
        group = await bus.new_results_group()
        try:
            jid = await bus.dispatch_job(aid, {
                "type": "wipe_history",
                "params": {"screenshot": name},
            })
            deadline = time.time() + 25
            while time.time() < deadline:
                for entry_id, msg in await bus.consume_results(group, block_ms=1200):
                    try:
                        await bus.ack_result(group, entry_id)
                    except Exception:
                        pass
                    if msg.get("job_id") != jid:
                        continue
                    if msg.get("kind") == "photo":
                        if msg.get("error"):
                            err = msg["error"]
                        elif msg.get("image_b64"):
                            try:
                                raw = base64.b64decode(msg["image_b64"])
                            except Exception as e:
                                err = str(e)
                    if msg.get("kind") == "done":
                        deadline = 0
                        break
        finally:
            await bus.destroy_results_group(group)
        if raw is None and not err:
            err = "agent did not return the screenshot (file may have been pruned)"

    if raw is None:
        await query.answer(err or "screenshot missing", show_alert=True)
        return
    bio = io.BytesIO(raw)
    bio.name = name if str(name).lower().endswith(".png") else "wipe.png"
    try:
        await query.message.reply_photo(photo=bio, caption=caption)
        await query.answer("Screenshot sent")
    except Exception as e:
        logger.warning(f"/wipes photo send failed: {e}")
        await query.answer(f"Could not send photo: {e}", show_alert=True)


def register_wipes_handlers(application):
    application.add_handler(CommandHandler("wipes", wipes_command))
    application.add_handler(CommandHandler("wipehistory", wipes_command))
    application.add_handler(CallbackQueryHandler(wipes_callback, pattern=r"^wipe:"))
    logger.info("Registered wipe-history handlers")
