"""
P1 cluster dashboard — formatting and session stat helpers.

Shared by the result loop (p1_distributed) and /p1live live viewer.
"""
import html
import time
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _esc(v) -> str:
    return html.escape(str(v if v is not None else "—"))


_LABEL = {
    "1": "🔴 pressed 1",
    "2": "🟢 pressed 2",
    "voicemail": "📵 voicemail",
    "not_answered": "📵 no answer",
    "hangup": "📞 hung up",
    "account_banned": "🚫 acc banned",
    "timeout": "⚪ timeout",
    "failed": "❌ failed",
    "cancelled": "🚫 cancelled",
}

_MEDALS = ("🥇", "🥈", "🥉")


def new_session(
    run_id: str,
    total: int,
    script_profile: str,
    agents: List[str],
    line_map: Optional[Dict[str, str]] = None,
    campaign_name: Optional[str] = None,
) -> dict:
    now = time.time()
    return {
        "run_id": run_id,
        "campaign_name": (campaign_name or "").strip() or "campaign",
        "total": total,
        "done": 0,
        "counts": {},
        "by_result": {},
        "agents": {aid: _empty_agent_stats(now) for aid in agents},
        "hot": [],
        "hangups": [],
        "running": True,
        "started_at": now,
        "updated_at": now,
        "script_profile": script_profile,
        "agents_involved": sorted(agents),
        "pending_agents": sorted(agents),
        "was_stopped": False,
        "queue_remaining": total,
        "line_map": dict(line_map or {}),
    }


def source_line(session: dict, phone: str) -> str:
    """Original input line for a phone (file row or /p1 arg), else the phone."""
    return (session.get("line_map") or {}).get(phone) or phone


def format_copy_line(session: dict, phone: str) -> str:
    """HTML-safe full line in <code> for one-tap copy in Telegram."""
    return f"<code>{_esc(source_line(session, phone))}</code>"


def _empty_agent_stats(now: float) -> dict:
    return {
        "done": 0,
        "counts": {},
        "first_ts": None,
        "last_ts": None,
        "_seed": now,
    }


def record_progress(session: dict, agent_id: str, result: str, phone: str) -> None:
    """Update in-memory session snapshot for one completed call."""
    now = time.time()
    session["done"] = session.get("done", 0) + 1
    session["updated_at"] = now

    counts = session.setdefault("counts", {})
    counts[result] = counts.get(result, 0) + 1

    # Per-result phone lists feed the export / redial buttons in the menu.
    if result in ("1", "2", "hangup", "not_answered", "voicemail"):
        lst = session.setdefault("by_result", {}).setdefault(result, [])
        if len(lst) < 100000:
            lst.append(phone)

    agents = session.setdefault("agents", {})
    st = agents.setdefault(agent_id, _empty_agent_stats(now))
    st["done"] = st.get("done", 0) + 1
    ac = st.setdefault("counts", {})
    ac[result] = ac.get(result, 0) + 1
    if st.get("first_ts") is None:
        st["first_ts"] = now
    st["last_ts"] = now

    if result in ("1", "2"):
        hot = session.setdefault("hot", [])
        hot.append({
            "result": result,
            "phone": phone,
            "line": source_line(session, phone),
            "agent": agent_id,
            "ts": now,
        })
        session["hot"] = hot[-50:]

    if result == "hangup":
        hangups = session.setdefault("hangups", [])
        hangups.append({
            "result": result,
            "phone": phone,
            "line": source_line(session, phone),
            "agent": agent_id,
            "ts": now,
        })
        session["hangups"] = hangups[-500:]


def record_agent_done(session: dict, agent_id: str) -> None:
    pending = session.setdefault("pending_agents", [])
    if agent_id in pending:
        pending.remove(agent_id)
    session["pending_agents"] = pending


def _progress_bar(done: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "░" * width
    filled = min(width, int(width * done / total))
    return "█" * filled + "░" * (width - filled)


def _elapsed_str(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hrs = mins // 60
    return f"{hrs}h {mins % 60}m"


def _agent_rankings(
    agents: dict, total_done: int, session_elapsed: float
) -> List[Tuple[str, int, float, float, dict]]:
    ranked = []
    for aid, st in agents.items():
        done = st.get("done", 0)
        if done <= 0:
            continue
        first = st.get("first_ts") or st.get("_seed", time.time())
        last = st.get("last_ts") or first
        span = max(last - first, 1.0)
        rate = done / (span / 60.0)
        share = (100.0 * done / total_done) if total_done else 0.0
        ranked.append((aid, done, rate, share, st))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def _result_breakdown(counts: dict) -> str:
    if not counts:
        return "<i>no results yet</i>"
    parts = [
        f"{_LABEL.get(k, k)}: <b>{v}</b>"
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return " · ".join(parts)


def format_dashboard(session: dict, view: str = "overview") -> str:
    """Build HTML dashboard text for Telegram."""
    run_id = session.get("run_id", "?")
    total = session.get("total", 0)
    done = session.get("done", 0)
    running = session.get("running", False)
    counts = session.get("counts", {})
    agents = session.get("agents", {})
    started_at = session.get("started_at", time.time())
    elapsed = max(time.time() - started_at, 1.0)
    pct = (100.0 * done / total) if total else 0.0
    hot1 = counts.get("1", 0)
    hot2 = counts.get("2", 0)
    overall_rate = done / (elapsed / 60.0) if done else 0.0
    bar = _progress_bar(done, total)
    status = "🔄 running" if running else "✅ done"
    campaign = session.get("campaign_name") or "campaign"

    header = [
        f"📞 <b>Campaign {_esc(campaign)}</b> — {status}",
        f"<code>{_esc(run_id)}</code> · "
        f"script <code>{_esc(session.get('script_label') or session.get('script_profile', '?'))}</code>",
        "",
        f"<b>{done}/{total}</b>  ({pct:.1f}%)"
        + (f"  ·  🔴1={hot1}  🟢2={hot2}" if (hot1 or hot2) else ""),
        f"<code>{bar}</code>  {_elapsed_str(elapsed)}"
        + (f"  ·  ⚡ {overall_rate:.1f}/min" if done else ""),
    ]

    if view == "hot":
        body = _format_hot_view(session)
    elif view == "servers":
        body = _format_servers_view(agents, done, elapsed)
    else:
        body = _format_overview_view(counts, agents, done, elapsed)

    if not running and done < total:
        suffix = "stopped" if session.get("was_stopped") else "timeout"
        header.append(f"\n⚠️ <i>ended early ({suffix})</i>")

    return "\n".join(header) + "\n\n" + body


def _format_overview_view(
    counts: dict, agents: dict, total_done: int, elapsed: float
) -> str:
    lines = [
        "<b>📊 Results</b>",
        _result_breakdown(counts),
        "",
        "<b>🏆 Top servers</b>",
    ]
    ranked = _agent_rankings(agents, total_done, elapsed)
    if not ranked:
        lines.append("<i>waiting for first call…</i>")
    else:
        for i, (aid, d, rate, share, st) in enumerate(ranked[:5]):
            medal = _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}."
            hot = st.get("counts", {}).get("1", 0)
            hot_bit = f" · 🔴{hot}" if hot else ""
            lines.append(
                f"{medal} <code>{_esc(aid)}</code>  "
                f"<b>{d}</b> ({share:.0f}%) · ⚡ {rate:.1f}/min{hot_bit}"
            )
        if len(ranked) >= 2:
            fastest = max(ranked, key=lambda x: x[2])
            slowest = min(
                (r for r in ranked if r[1] >= 3),
                key=lambda x: x[2],
                default=None,
            )
            if slowest and fastest[0] != slowest[0]:
                lines += [
                    "",
                    f"⚡ fastest: <code>{_esc(fastest[0])}</code> "
                    f"({fastest[2]:.1f}/min)",
                    f"🐢 slowest: <code>{_esc(slowest[0])}</code> "
                    f"({slowest[2]:.1f}/min)",
                ]
    lines.append("\n<i>🖥 Servers · 🔥 Hot · 🔄 Refresh</i>")
    return "\n".join(lines)


def _format_servers_view(
    agents: dict, total_done: int, elapsed: float
) -> str:
    ranked = _agent_rankings(agents, total_done, elapsed)
    if not ranked:
        return "<b>🖥 Servers</b>\n<i>no activity yet</i>"

    lines = ["<b>🖥 Server leaderboard</b>", ""]
    max_done = ranked[0][1] if ranked else 1

    for i, (aid, d, rate, share, st) in enumerate(ranked):
        medal = _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}."
        mini_bar = _progress_bar(d, max_done, width=10)
        ac = st.get("counts", {})
        extras = []
        for key in ("1", "2", "voicemail", "hangup", "timeout", "not_answered", "failed"):
            if ac.get(key):
                short = {"1": "🔴1", "2": "🟢2"}.get(key, _LABEL.get(key, key))
                extras.append(f"{short}:{ac[key]}")
        extra_str = f"\n    {' · '.join(extras)}" if extras else ""
        lines.append(
            f"{medal} <code>{_esc(aid)}</code>\n"
            f"    <code>{mini_bar}</code>  <b>{d}</b> ({share:.0f}%)"
            f" · ⚡ {rate:.1f}/min{extra_str}"
        )

    idle = [
        aid for aid, st in agents.items()
        if st.get("done", 0) == 0
    ]
    if idle:
        lines += ["", f"<i>💤 idle: {', '.join(_esc(a) for a in sorted(idle))}</i>"]
    return "\n".join(lines)


def _format_hot_view(session: dict) -> str:
    hot = session.get("hot") or []
    if not hot:
        return "<b>🔥 Hot results</b>\n<i>no 🔴1 / 🟢2 yet</i>"

    lines = ["<b>🔥 Hot results</b>", ""]
    for item in reversed(hot[-30:]):
        res = item.get("result", "?")
        label = _LABEL.get(res, res)
        line = item.get("line") or source_line(session, item.get("phone", ""))
        lines.append(
            f"{label} — <code>{_esc(line)}</code> "
            f"(<code>{_esc(item.get('agent'))}</code>)"
        )
    return "\n".join(lines)


def format_completion_notice(session: dict) -> str:
    """Short standalone alert when a bulk run finishes."""
    total = session.get("total", 0)
    done = session.get("done", 0)
    counts = session.get("counts", {})
    hot1 = counts.get("1", 0)
    hot2 = counts.get("2", 0)
    run_id = session.get("run_id", "?")
    campaign = session.get("campaign_name") or "campaign"
    profile = session.get("script_label") or session.get("script_profile", "?")
    pct = (100.0 * done / total) if total else 0.0

    if session.get("was_stopped"):
        status = "stopped"
    elif done < total:
        status = "ended early"
    else:
        status = "complete"

    lines = [
        f"✅ <b>Campaign {_esc(campaign)} — {status}</b>",
        f"<code>{_esc(run_id)}</code> · script <code>{_esc(profile)}</code>",
        f"<b>{done}/{total}</b> ({pct:.1f}%)",
    ]
    if hot1 or hot2:
        lines.append(f"🔴1={hot1}  🟢2={hot2}")
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        brief = " · ".join(
            f"{_LABEL.get(k, k)}: <b>{v}</b>" for k, v in top
        )
        lines.append(brief)
    return "\n".join(lines)


# ── Export / redial (post-campaign buttons) ──────────────────────────────────

EXPORT_CATEGORIES = {
    "hangup": ("hangup",),
    "noanswer": ("not_answered", "voicemail"),
    "naonly": ("not_answered",),
    "hot": ("1", "2"),
}

_CATEGORY_TITLE = {
    "hangup": "hang ups",
    "noanswer": "no answers",
    "naonly": "no answer only",
    "hot": "hot (pressed 1/2)",
}


def phones_for(session: dict, category: str) -> List[str]:
    """All phones of one export category, in call order."""
    by = session.get("by_result") or {}
    out: List[str] = []
    for key in EXPORT_CATEGORIES.get(category, ()):
        out.extend(by.get(key) or [])
    return out


def category_title(category: str) -> str:
    return _CATEGORY_TITLE.get(category, category)


def completion_keyboard(session: dict) -> InlineKeyboardMarkup:
    """Export / redial buttons attached to the campaign-complete message."""
    run_id = session.get("run_id", "")
    rows: List[List[InlineKeyboardButton]] = []

    exp_row: List[InlineKeyboardButton] = []
    for cat in ("hangup", "noanswer", "hot"):
        n = len(phones_for(session, cat))
        if n:
            exp_row.append(InlineKeyboardButton(
                f"📥 {category_title(cat)} ({n})",
                callback_data=f"camp:exp:{run_id}:{cat}",
            ))
    if exp_row:
        rows.append(exp_row)

    redial_row: List[InlineKeyboardButton] = []
    for cat in ("hangup", "noanswer", "naonly"):
        if phones_for(session, cat):
            redial_row.append(InlineKeyboardButton(
                f"🔁 Redial {category_title(cat)}",
                callback_data=f"camp:redial:{run_id}:{cat}",
            ))
    if redial_row:
        rows.append(redial_row)

    rows.append([
        InlineKeyboardButton("📊 Campaign results", callback_data="camp:results"),
        InlineKeyboardButton("🏠 Menu", callback_data="camp:home"),
    ])
    return InlineKeyboardMarkup(rows)


def format_final_summary(session: dict) -> str:
    """Enhanced completion message with full leaderboard."""
    session = dict(session)
    session["running"] = False
    text = format_dashboard(session, view="overview")
    ranked = _agent_rankings(
        session.get("agents", {}),
        session.get("done", 0),
        max(time.time() - session.get("started_at", time.time()), 1.0),
    )
    if len(ranked) > 5:
        text += "\n\n<b>🖥 All servers</b>\n"
        text += _format_servers_view(
            session.get("agents", {}),
            session.get("done", 0),
            max(time.time() - session.get("started_at", time.time()), 1.0),
        ).split("\n\n", 1)[-1]
    hot = session.get("hot") or []
    if hot:
        text += "\n\n<b>Hot results</b>\n"
        for item in hot[-30:]:
            label = _LABEL.get(item.get("result"), item.get("result"))
            line = item.get("line") or source_line(session, item.get("phone", ""))
            text += (
                f"{label} — <code>{_esc(line)}</code> "
                f"(<code>{_esc(item.get('agent'))}</code>)\n"
            )
    return text.rstrip()
