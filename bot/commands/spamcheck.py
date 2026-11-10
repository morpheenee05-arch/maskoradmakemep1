"""
/spamcheck — profiles call each other (calls are NOT answered) and check
whether the caller's number is flagged as "Suspected spam" in the
receiver's Google Voice history.

Usage:
  /spamcheck              all profiles, parallel, 8s ring
  /spamcheck seq          pairs one after another
  /spamcheck p2           only 2 pairs (disables odd-helper)
  /spamcheck debug 12     all profiles, 12s ring, screenshots + full log
"""
import asyncio
import base64
import binascii
import html
import os
import time
from io import BytesIO

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from loguru import logger


def _esc(value) -> str:
    """Escape a value for Telegram HTML parse mode."""
    return html.escape(str(value if value is not None else "—"))


_VERDICT = {
    "spam":    ("🚨", "FLAGGED"),
    "clean":   ("✅", "CLEAN"),
    "absent":  ("⚠️", "NOT IN HISTORY"),
    "unknown": ("❓", "UNKNOWN"),
}


def _verdict_label(verdict: str) -> str:
    icon, name = _VERDICT.get(verdict, ("❓", "UNKNOWN"))
    return f"{icon} {name}"


async def _send_html(message, text: str):
    """Send (chunked) HTML text within Telegram's 4096-char limit."""
    for i in range(0, len(text), 3500):
        await message.reply_text(
            text[i:i + 3500], parse_mode="HTML",
            disable_web_page_preview=True,
        )


def _result_block(title: str, summary: dict) -> str:
    """Render one agent's report (slim results) as an HTML block."""
    results = summary.get("results", []) or []
    if summary.get("error") and not results:
        return f"\n<b>{_esc(title)}</b>\n  ⚠️ {_esc(summary['error'])}"

    flagged = [r for r in results if r.get("verdict") == "spam"]
    clean = [r for r in results if r.get("verdict") == "clean"]
    other = [r for r in results if r.get("verdict") not in ("spam", "clean")]

    def _ln(r):
        note = r.get("error")
        extra = f"\n  └ <i>{_esc(note)}</i>" if note else ""
        return (
            f"• <code>{_esc(r.get('caller_number'))}</code> — "
            f"{_esc(r.get('caller_email') or 'email unknown')}\n"
            f"  └ W{r.get('caller_worker')} → W{r.get('callee_worker')} "
            f"(<code>{_esc(r.get('callee_number'))}</code>){extra}"
        )

    head = (
        f"\n<b>{_esc(title)}</b> — checked {summary.get('checked', len(results))}"
        f" · 🚨 {len(flagged)} · ✅ {len(clean)} · ⚠️ {len(other)}"
    )
    parts = [head]
    if flagged:
        parts.append("🚨 <b>FLAGGED</b>\n" + "\n".join(_ln(r) for r in flagged))
    if clean:
        parts.append("✅ <b>CLEAN</b>\n" + "\n".join(_ln(r) for r in clean))
    if other:
        parts.append("⚠️ <b>REVIEW</b>\n" + "\n".join(_ln(r) for r in other))

    disabled = summary.get("disabled_workers") or []
    if disabled:
        dis_lines = [
            f"• W{d['worker_id']} — <code>{_esc(d.get('number'))}</code> "
            f"({_esc(d.get('email') or 'email unknown')})"
            for d in disabled
        ]
        parts.append(
            "🗑 <b>AUTO-DISABLED</b> — cookies cleared, waiting for /login\n"
            + "\n".join(dis_lines)
        )
    return "\n".join(parts)


async def _run_distributed(update, context, bus, *, target, mode,
                           ring_sec, max_pairs, debug):
    """Controller: fan a spamcheck job out to agents and aggregate replies."""
    try:
        agents = [a for a in await bus.list_agents()
                  if a.get("role") == "agent"]
    except Exception as e:
        await update.message.reply_text(f"❌ Redis error: {_esc(e)}")
        return

    if target:
        agents = [a for a in agents if a.get("agent_id") == target]
        if not agents:
            await update.message.reply_text(
                f"⚠️ Agent <code>{_esc(target)}</code> is not online.",
                parse_mode="HTML",
            )
            return
    if not agents:
        await update.message.reply_text("⚠️ No agents online.")
        return

    aids = [a["agent_id"] for a in agents]
    timeout = int(os.getenv("GV_JOB_TIMEOUT_SEC", "900"))
    params = {"mode": mode, "ring_sec": ring_sec,
              "max_pairs": max_pairs, "debug": debug}

    # Per-run consumer group → isolated from any concurrent distributed run.
    group = await bus.new_results_group()
    job_owner = {}      # job_id -> agent_id
    pending = set()
    for aid in aids:
        jid = await bus.dispatch_job(
            aid, {"type": "spamcheck", "params": params}
        )
        job_owner[jid] = aid
        pending.add(aid)

    await update.message.reply_text(
        "🔎 <b>Spam Probe — dispatched</b>\n"
        f"Agents: <b>{len(aids)}</b> (<code>{_esc(', '.join(aids))}</code>)\n"
        f"Mode: <code>{mode}</code> · Ring: <code>{ring_sec:.0f}s</code> · "
        f"Debug: <code>{'on' if debug else 'off'}</code>\n"
        f"Waiting for replies (timeout {timeout}s)…",
        parse_mode="HTML",
    )

    summaries = {}                       # agent_id -> summary dict
    photos = []                          # (caption, image_b64)
    deadline = time.time() + timeout
    try:
        while pending and time.time() < deadline:
            msgs = await bus.consume_results(group, block_ms=2000)
            for entry_id, msg in msgs:
                try:
                    await bus.ack_result(group, entry_id)
                    if msg.get("job_id") not in job_owner:
                        continue  # belongs to another run
                    aid = msg.get("agent_id") or job_owner[msg["job_id"]]
                    kind = msg.get("kind")
                    if kind == "report":
                        summaries[aid] = msg.get("summary", {})
                    elif kind == "photo":
                        photos.append((msg.get("caption", ""),
                                       msg.get("image_b64", "")))
                    elif kind == "done":
                        pending.discard(aid)
                except Exception as e:
                    logger.warning(f"result handling error: {e}")
    finally:
        await bus.destroy_results_group(group)

    # ── Render aggregated report ──
    out = ["📊 <b>Spam Probe — Cluster Report</b>"]
    if pending:
        out.append(
            f"⏱️ No reply (timeout) from: "
            f"<code>{_esc(', '.join(sorted(pending)))}</code>"
        )
    for aid in aids:
        if aid in summaries:
            out.append(_result_block(aid, summaries[aid]))
        elif aid not in pending:
            out.append(f"\n<b>{_esc(aid)}</b>\n  (no report)")
    await _send_html(update.message, "\n".join(out))

    sent = 0
    for caption, b64 in photos:
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
        except (binascii.Error, ValueError):
            continue
        bio = BytesIO(raw)
        bio.name = "receiver.png"
        try:
            await update.message.reply_photo(photo=bio, caption=caption[:1024])
            sent += 1
        except Exception as e:
            logger.warning(f"reply_photo failed: {e}")
        await asyncio.sleep(0.2)  # gentle on Telegram rate limits
    logger.info(f"Distributed spamcheck: {len(summaries)} reports, {sent} photos")


async def spamcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_args = context.args or []
    args = [a.lower() for a in raw_args]
    mode = "sequential" if "seq" in args else "parallel"
    debug = "debug" in args
    ring_sec = next(
        (float(a) for a in args if a.replace(".", "", 1).isdigit()), 8.0
    )
    max_pairs = next(
        (int(a[1:]) for a in args if a.startswith("p") and a[1:].isdigit()),
        None,
    )
    target = next((a[1:] for a in raw_args if a.startswith("@")), None)

    # Controller mode → dispatch over Redis to agents.
    bus = context.bot_data.get("bus")
    if bus is not None:
        await _run_distributed(
            update, context, bus, target=target, mode=mode,
            ring_sec=ring_sec, max_pairs=max_pairs, debug=debug,
        )
        return

    # Standalone mode → run locally (unchanged behaviour).
    pool = context.bot_data.get("gv_pool")
    if not pool:
        await update.message.reply_text("⏳ Telephony pool is not ready yet.")
        return

    scope = f"{max_pairs} pair(s)" if max_pairs else "all profiles"
    await update.message.reply_text(
        "🔎 <b>Spam Probe — started</b>\n"
        f"Mode: <code>{mode}</code> · Ring: <code>{ring_sec:.0f}s</code> · "
        f"Scope: <code>{_esc(scope)}</code> · "
        f"Debug: <code>{'on' if debug else 'off'}</code>\n\n"
        "Every number is tested as the <b>caller</b> (in a pair: A→B, then "
        "B→A; an odd profile is covered by a freed worker). "
        "Calls are placed but <b>not answered</b>.",
        parse_mode="HTML",
    )

    try:
        from telephony.spam_probe import run_spam_probe

        report = await run_spam_probe(
            pool, mode=mode, ring_sec=ring_sec, max_pairs=max_pairs, debug=debug
        )
    except Exception as e:
        logger.error(f"/spamcheck failed: {e}")
        await update.message.reply_text(f"❌ Error: {_esc(e)}", parse_mode="HTML")
        return

    if report.get("error"):
        await update.message.reply_text(
            f"⚠️ {_esc(report['error'])}", parse_mode="HTML"
        )
        return

    results = report.get("results", [])
    checked = report.get("checked", len(results))
    flagged = [r for r in results if r.get("verdict") == "spam"]
    clean = [r for r in results if r.get("verdict") == "clean"]
    other = [r for r in results if r.get("verdict") not in ("spam", "clean")]

    def _line(r: dict) -> str:
        cnum = _esc(r.get("caller_number"))
        email = _esc(r.get("caller_email") or "email unknown")
        cw, ce = r.get("caller_worker"), r.get("callee_worker")
        enum = _esc(r.get("callee_number"))
        note = r.get("error")
        extra = f"\n  └ <i>{_esc(note)}</i>" if note else ""
        return (
            f"• <code>{cnum}</code> — {email}\n"
            f"  └ route: W{cw} → W{ce} (<code>{enum}</code>){extra}"
        )

    out = [
        "📊 <b>Spam Probe — Report</b>",
        f"Numbers checked: <b>{checked}</b> · Pairs: <b>{report['pairs']}</b> · "
        f"🚨 {len(flagged)} · ✅ {len(clean)} · ⚠️ {len(other)}",
    ]
    if flagged:
        out.append(
            f"\n🚨 <b>FLAGGED — {len(flagged)}</b>\n"
            + "\n".join(_line(r) for r in flagged)
        )
    if clean:
        out.append(
            f"\n✅ <b>CLEAN — {len(clean)}</b>\n"
            + "\n".join(_line(r) for r in clean)
        )
    if other:
        out.append(
            f"\n⚠️ <b>NEEDS REVIEW — {len(other)}</b>\n"
            + "\n".join(_line(r) for r in other)
        )
    # Report auto-disabled workers
    disabled = report.get("disabled_workers") or []
    if disabled:
        dis_lines = []
        for d in disabled:
            dis_lines.append(
                f"• W{d['worker_id']} — <code>{_esc(d.get('number'))}</code> "
                f"({_esc(d.get('email') or 'email unknown')})"
            )
        out.append(
            f"\n🗑 <b>AUTO-DISABLED — {len(disabled)} worker(s)</b>\n"
            f"Cookies cleared. The Chrome profile is kept as an empty slot — "
            f"send /accounts then /login to put a new account on it.\n"
            + "\n".join(dis_lines)
        )

    if report.get("debug_log"):
        out.append(
            f"\n📄 Full log (on server): <code>{_esc(report['debug_log'])}</code>"
        )

    await _send_html(update.message, "\n".join(out))

    # Screenshots of the RECEIVER's Google Voice page: only for FLAGGED
    # numbers by default — one photo per account was flooding the chat.
    # `debug` sends them all.
    for r in results:
        if not debug and r.get("verdict") != "spam":
            continue
        shot = r.get("receiver_screenshot")  # strictly the receiver's screen
        cw, ce = r.get("caller_worker"), r.get("callee_worker")
        if not shot or not os.path.exists(shot):
            await update.message.reply_text(
                f"⚠️ No receiver screenshot for W{ce} "
                f"({_esc(r.get('callee_number'))}) — receiver check did not "
                f"complete.",
                parse_mode="HTML",
            )
            continue
        caption = (
            f"{_verdict_label(r.get('verdict'))}  "
            f"{r.get('caller_number')}\n"
            f"Receiver: W{ce} ({r.get('callee_number')}) · "
            f"Caller: W{cw}"
        )
        try:
            with open(shot, "rb") as ph:
                await update.message.reply_photo(photo=ph, caption=caption[:1024])
        except Exception as e:
            logger.warning(f"reply_photo failed for {shot}: {e}")
            await update.message.reply_text(f"📷 Receiver screenshot: {shot}")


def register_spamcheck_handlers(application):
    application.add_handler(CommandHandler("spamcheck", spamcheck_command))
    logger.info("Registered spamcheck handler")
