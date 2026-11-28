"""
/proxy — per-account proxy routing for SIP calls.

One proxy per worker (account): W0 uses the first proxy, W1 the second, and
so on. The list lives on each agent (.sip_proxies.json) and is applied to NEW
SIP registrations — already-registered sockets keep their current route until
re-register (~1h) or the next campaign's prewarm.

Only SIP signaling (WebSocket + credential mint) goes through the proxy;
media (SRTP/UDP) always goes direct, same as the Chrome dialer.

Usage:
  /proxy                      status of every agent
  /proxy on                   enable with the stored list
  /proxy off                  disable (keep the list)
  /proxy clear                drop the list and disable
  /proxy set                  proxies, one per line, in the same message
Accepted formats: host:port, host:port:user:pass, user:pass:host:port,
user:pass@host:port, with an optional scheme prefix
(http/https/socks4/socks4a/socks5/socks5h). No scheme = http.
"""
import html
import re
import time

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from loguru import logger

from telephony.sip.proxy_config import parse_proxy

_USAGE = (
    "🌐 <b>SIP proxies</b>\n"
    "• <code>/proxy</code> — status\n"
    "• <code>/proxy on</code> / <code>/proxy off</code>\n"
    "• <code>/proxy clear</code> — drop the list\n"
    "• <code>/proxy set</code> + list, one per line:\n"
    "<code>/proxy set\n1.2.3.4:8080\nsocks5://user:pass@5.6.7.8:1080\n"
    "9.8.7.6:3128:login:password</code>\n"
    "Formats: host:port, host:port:user:pass, user:pass:host:port, "
    "user:pass@host:port; scheme http/https/socks4/socks5. "
    "No scheme = http.\n"
    "One proxy per account: W0 → 1st, W1 → 2nd…"
)


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


async def _send_html(target, text: str):
    send = getattr(target, "reply_text", None) or target.send_message
    for i in range(0, len(text), 3500):
        await send(
            text[i:i + 3500], parse_mode="HTML", disable_web_page_preview=True
        )


async def _fanout(bus, action: str, proxies=None, timeout=25):
    """Dispatch the proxies job to every agent and collect the replies."""
    agents = [a for a in await bus.list_agents() if a.get("role") == "agent"]
    aids = [a["agent_id"] for a in agents]
    if not aids:
        return {}, set()
    group = await bus.new_results_group()
    job_owner = {}
    try:
        for aid in aids:
            jid = await bus.dispatch_job(aid, {
                "type": "proxies",
                "params": {"action": action, "proxies": proxies or []},
            })
            job_owner[jid] = aid
        pending = set(job_owner.values())
        out = {}
        deadline = time.time() + timeout
        while pending and time.time() < deadline:
            for entry_id, msg in await bus.consume_results(group, block_ms=2000):
                try:
                    await bus.ack_result(group, entry_id)
                    if msg.get("job_id") not in job_owner:
                        continue
                    aid = msg.get("agent_id") or job_owner[msg["job_id"]]
                    if msg.get("kind") == "proxies":
                        out[aid] = msg
                        pending.discard(aid)
                except Exception as e:
                    logger.warning(f"proxy result handling error: {e}")
        return out, pending
    finally:
        await bus.destroy_results_group(group)


def _render(reports: dict, pending: set, action: str) -> str:
    out = ["🌐 <b>SIP proxies</b>"]
    if pending:
        out.append(
            f"⏱️ No reply from: <code>{_esc(', '.join(sorted(pending)))}</code>"
        )
    for aid in sorted(reports):
        msg = reports[aid] or {}
        st = msg.get("status") or {}
        enabled = st.get("enabled")
        count = st.get("count", 0)
        workers = st.get("workers", 0)
        mark = "🟢 ON" if enabled else "⚪ OFF"
        out.append(f"\n<b>{_esc(aid)}</b> — {mark}, {count} proxy / {workers} worker(s)")
        if msg.get("error"):
            out.append(f"  ⚠️ {_esc(msg['error'])}")
        for row in st.get("rows") or []:
            if row.get("ok"):
                out.append(
                    f"  • W{row['worker_id']} → <code>{_esc(row['proxy'])}</code>"
                )
            else:
                out.append(
                    f"  • W{row['worker_id']} → ❌ <code>{_esc(row['proxy'])}</code>"
                    f" ({_esc(row.get('error'))})"
                )
        if enabled and st.get("missing"):
            out.append(
                f"  🟠 <b>Not enough proxies:</b> {st['missing']} worker(s) "
                f"(W{count}…W{workers - 1}) will call DIRECT"
            )
        if enabled and count and not st.get("missing") and workers > count:
            pass
        if action == "set" and not msg.get("error"):
            out.append("  ✅ list saved, proxy mode ON")
    out.append(
        "\n<i>Applies to new SIP registrations; lines already registered "
        "switch over on re-register (~1h) or next campaign.</i>"
    )
    return "\n".join(out)


async def proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    text = (msg.text or "").strip()
    rest = re.split(r"\s", text, maxsplit=1)
    arg = rest[1].strip() if len(rest) > 1 else ""
    action, _, remainder = arg.partition("\n")
    if " " in action.strip():
        action, _, inline = action.strip().partition(" ")
        remainder = (inline + "\n" + remainder).strip()
    action = action.strip().lower() or "status"
    if action not in ("status", "on", "off", "clear", "set"):
        # Convenience: "/proxy <list…>" without the "set" keyword — when the
        # argument is proxy lines, it IS the list.
        action, remainder = "set", arg

    proxies = []
    if action == "set":
        raw_lines = []
        for chunk in remainder.replace(",", "\n").splitlines():
            # tolerate space-separated proxies on one line too
            raw_lines.extend(chunk.split())
        proxies = [l for l in raw_lines if l and not l.startswith("#")]
        if not proxies:
            await _send_html(msg, "⚠️ Empty list.\n\n" + _USAGE)
            return
        bad = []
        for line in proxies:
            try:
                parse_proxy(line)
            except ValueError as e:
                bad.append(f"• <code>{_esc(line)}</code> — {_esc(e)}")
        if bad:
            await _send_html(
                msg,
                "❌ Could not parse these lines (nothing applied):\n"
                + "\n".join(bad) + "\n\n" + _USAGE,
            )
            return

    bus = context.bot_data.get("bus")
    if bus is None:
        await _send_html(msg, "⚠️ Controller has no bus connection.")
        return

    status_msg = await msg.reply_text("⏳ Asking agents…")
    try:
        reports, pending = await _fanout(bus, action, proxies)
    except Exception as e:
        logger.exception("proxy fan-out failed")
        await status_msg.edit_text(f"❌ fan-out failed: {_esc(e)}",
                                   parse_mode="HTML")
        return
    text_out = _render(reports, pending, action)
    if not reports and not pending:
        text_out = "🌐 <b>SIP proxies</b>\nNo agents online."
    try:
        await status_msg.edit_text(
            text_out, parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception:
        await _send_html(msg, text_out)


def register_proxy_handlers(application):
    application.add_handler(CommandHandler("proxy", proxy_command))
    logger.info("Registered proxy handler")
