"""
/accounts and /login — Google account inventory and automatic sign-in.

/accounts asks every agent which of its Chrome profiles actually holds a live
Google Voice session, and reports the ones that do not. /login takes the
logins for those profiles and has the agents sign them in, several browsers at
a time.

Credentials travel operator → controller → Redis → agent. The message carrying
them is deleted from the chat as soon as it is parsed, and the job entry is
removed from the stream when the agent acks it, but they are plaintext in
transit — keep the Redis instance private and see README before turning
GV_SAVE_CREDENTIALS on.
"""
import html
import os
import time

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from loguru import logger

import config
from infra.gv_credentials import parse_credentials

_STATE_LABEL = {
    "ready": "✅ signed in",
    "needs_login": "🔑 needs login",
    "challenge": "🧩 2FA / captcha",
    "captcha": "🤖 captcha",
    "logging_in": "🔐 signing in…",
    "banned": "🚫 banned",
    "spam": "🚨 spam-flagged",
    "restarting": "♻️ restarting",
    "failed": "❌ failed",
    "initializing": "⏳ starting",
}

# Sign-in outcomes the operator has to act on, with the action spelled out.
_OUTCOME_HINT = {
    "no_credentials": "no password sent yet",
    "bad_password": "password rejected — check it and resend",
    "unknown_email": "no such Google account",
    "challenge_required": "Google wants a code — sign in by hand once",
    "captcha": "robot check — profile restarted onto the login page",
    "rate_limited": "Google is throttling — wait before retrying",
    "disabled": "account disabled by Google",
    "no_dialer": "signed in, but Voice did not open",
    "no_form": "sign-in page did not appear",
    "timeout": "sign-in timed out",
    "no_browser": "browser is down — it is being restarted",
}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


async def _send_html(target, text: str):
    """Send chunked HTML to a Message (reply) or a Chat, within the 4096 limit."""
    send = getattr(target, "reply_text", None) or target.send_message
    for i in range(0, len(text), 3500):
        await send(
            text[i:i + 3500], parse_mode="HTML", disable_web_page_preview=True
        )


def _account_line(row: dict) -> str:
    state = row.get("state") or row.get("login_state") or "unknown"
    label = _STATE_LABEL.get(state, f"❔ {state}")
    who = row.get("email") or "account unknown"
    num = row.get("phone_number") or "no number"
    tfa = ""
    if row.get("has_totp"):
        tfa += " · 🔢 TOTP"
    if row.get("backup_codes_left"):
        tfa += f" · 🗝 {row['backup_codes_left']} code(s)"
    line = (
        f"• <b>W{row.get('worker_id')}</b> "
        f"<code>{_esc(row.get('profile'))}</code> — {label}\n"
        f"  └ {_esc(who)} · <code>{_esc(num)}</code>{tfa}"
    )
    outcome = row.get("login_outcome")
    if state != "ready" and outcome:
        hint = _OUTCOME_HINT.get(outcome, outcome)
        line += f"\n  └ <i>{_esc(hint)}</i>"
    elif state == "needs_login" and not row.get("has_credentials"):
        line += "\n  └ <i>no password sent yet</i>"
    return line


# ── Redis fan-out helpers ─────────────────────────────────────────────────────


async def _agent_ids(bus, target=None):
    agents = [a for a in await bus.list_agents() if a.get("role") == "agent"]
    if target:
        agents = [a for a in agents if a.get("agent_id") == target]
    return [a["agent_id"] for a in agents]


async def _collect(bus, group, job_owner, kinds, timeout):
    """Gather one reply of any kind in `kinds` per agent."""
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
                if msg.get("kind") in kinds:
                    out[aid] = msg
                    pending.discard(aid)
            except Exception as e:
                logger.warning(f"account result handling error: {e}")
    return out, pending


async def _gather_accounts(bus, target=None, timeout=45):
    """Ask every agent for its account inventory."""
    aids = await _agent_ids(bus, target)
    if not aids:
        return {}, set(), []
    group = await bus.new_results_group()
    job_owner = {}
    try:
        for aid in aids:
            jid = await bus.dispatch_job(aid, {"type": "accounts", "params": {}})
            job_owner[jid] = aid
        reports, pending = await _collect(
            bus, group, job_owner, {"accounts"}, timeout
        )
    finally:
        await bus.destroy_results_group(group)
    return reports, pending, aids


# ── /accounts ─────────────────────────────────────────────────────────────────


def _local_report(pool):
    return {
        config.AGENT_ID: {
            "accounts": pool.accounts_report(),
            "summary": pool.get_status_snapshot(),
        }
    }


def _render_accounts(reports: dict, pending: set) -> tuple:
    """Build the report text and the ordered list of profiles needing a login."""
    out = ["🔐 <b>Google accounts</b>"]
    needs = []

    if pending:
        out.append(
            f"⏱️ No reply from: <code>{_esc(', '.join(sorted(pending)))}</code>"
        )

    total = ready = 0
    for aid in sorted(reports):
        msg = reports[aid] or {}
        rows = msg.get("accounts") or []
        summary = msg.get("summary") or {}
        total += len(rows)
        ready += sum(1 for r in rows if r.get("state") == "ready")
        head = f"\n<b>{_esc(aid)}</b>"
        if summary.get("state"):
            head += f"  <i>{_esc(summary['state'])}</i>"
        out.append(head)
        if msg.get("error"):
            out.append(f"  ⚠️ {_esc(msg['error'])}")
        if not rows:
            out.append("  (no workers)")
            continue
        for row in sorted(rows, key=lambda r: r.get("worker_id", 0)):
            out.append(_account_line(row))
            state = row.get("state")
            if row.get("banned"):
                continue
            if (
                state in ("needs_login", "failed", "spam", "challenge")
                or row.get("spam_flagged")
            ):
                needs.append({
                    "agent": aid,
                    "worker_id": row.get("worker_id"),
                    "profile": row.get("profile"),
                    "email": row.get("email"),
                    "has_credentials": bool(row.get("has_credentials")),
                })

    out.insert(1, f"Signed in: <b>{ready}/{total}</b> · needing a login: <b>{len(needs)}</b>")

    if needs:
        listing = "\n".join(
            f"{i}. <code>{_esc(n['agent'])}/w{n['worker_id']}</code> "
            f"<code>{_esc(n['profile'])}</code>"
            + (f" — {_esc(n['email'])}" if n.get("email") else "")
            for i, n in enumerate(needs, start=1)
        )
        out.append(
            f"\n🔑 <b>Waiting for a login — {len(needs)}</b>\n{listing}\n\n"
            "Send them in one message, one account per line:\n"
            "<code>/login\n"
            "user1@gmail.com password1\n"
            "user2@gmail.com password2</code>\n"
            "Lines are matched to the list above in order. To target a profile "
            "explicitly, prefix it: <code>agent-01/w3 user@gmail.com pass</code>. "
            "If Google asks for the backup address, append it: "
            "<code>user@gmail.com pass recovery@gmail.com</code>. "
            "2FA: append a 2fa.live link "
            "(<code>user@gmail.com pass https://2fa.live/tok/SEED</code>) "
            "or backup codes (<code>user@gmail.com pass 5lpg ul5m beax bjcy</code>); "
            "multi-line cards are fine too.\n"
            "<i>The message is deleted right after it is read.</i>"
        )
    return "\n".join(out), needs


async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = next(
        (a[1:] for a in (context.args or []) if a.startswith("@")), None
    )
    bus = context.bot_data.get("bus")

    if bus is not None:
        await update.message.reply_text("🔎 Polling agents for account state…")
        try:
            reports, pending, aids = await _gather_accounts(bus, target)
        except Exception as e:
            await update.message.reply_text(f"❌ Redis error: {_esc(e)}",
                                            parse_mode="HTML")
            return
        if not aids:
            await update.message.reply_text("⚠️ No agents online.")
            return
    else:
        pool = context.bot_data.get("gv_pool")
        if not pool:
            await update.message.reply_text("⏳ Telephony pool is not ready yet.")
            return
        reports, pending = _local_report(pool), set()

    text, needs = _render_accounts(reports, pending)
    context.bot_data["login_pending"] = needs
    await _send_html(update.message, text)


# ── /login ────────────────────────────────────────────────────────────────────


def _route_entries(entries: list, needs: list) -> tuple:
    """Attach each credential line to an agent.

    Explicitly targeted lines keep their agent. The rest fill the profiles from
    the last /accounts listing, in order — so line N goes to item N.
    """
    per_agent = {}
    unrouted = []
    queue = [n for n in needs]

    for entry in entries:
        agent = entry.get("agent")
        worker = entry.get("worker")
        profile = entry.get("profile")

        if agent is None:
            match = None
            if worker is not None or profile:
                match = next(
                    (
                        n for n in queue
                        if (worker is not None and n["worker_id"] == worker)
                        or (profile and n["profile"] == profile)
                    ),
                    None,
                )
            elif entry.get("email"):
                match = next(
                    (
                        n for n in queue
                        if (n.get("email") or "").lower() == entry["email"].lower()
                    ),
                    None,
                )
            if match is None and worker is None and not profile:
                match = queue[0] if queue else None
            if match is None:
                unrouted.append(entry)
                continue
            queue.remove(match)
            agent = match["agent"]
            entry = dict(entry)
            entry["worker"] = match["worker_id"]
            entry["profile"] = match["profile"]
        else:
            match = next(
                (
                    n for n in queue
                    if n["agent"] == agent
                    and (worker is None or n["worker_id"] == worker)
                    and (not profile or n["profile"] == profile)
                ),
                None,
            )
            if match is not None:
                queue.remove(match)

        per_agent.setdefault(agent, []).append({
            "email": entry["email"],
            "password": entry["password"],
            "recovery_email": entry.get("recovery_email"),
            "totp_seed": entry.get("totp_seed"),
            "backup_codes": entry.get("backup_codes"),
            "worker": entry.get("worker"),
            "profile": entry.get("profile"),
        })

    return per_agent, unrouted


_STAGE_EMOJI = {
    "queued": "⏳",
    "starting Chrome": "🌐",
    "checking session": "🔎",
    "opening Sign in": "👉",
    "entering email": "✉️",
    "entering password": "🔑",
    "picking account": "👤",
    "skipping passkey (Not now)": "⏭️",
    "skipping passkey (Try another way)": "⏭️",
    "recovery email": "📧",
    "entering 2FA code": "🔢",
    "Google Authenticator": "🔢",
    "backup code": "🔢",
    "2FA method list": "🔢",
    "opening Voice dialer": "📞",
    "signing in…": "🔐",
    "captcha — restarting onto the login page": "🤖",
    "2FA / extra verification": "🧩",
    "banned by Google": "🚫",
}


def _login_account_line(row: dict) -> str:
    """One live line: worker, email, what it is doing."""
    email = row.get("email") or "account unknown"
    wid = row.get("worker_id")
    if row.get("ok"):
        num = row.get("phone_number") or "no number yet"
        return (
            f"  ✅ W{wid} <code>{_esc(email)}</code> — "
            f"signed in · <code>{_esc(num)}</code>"
        )
    outcome = row.get("outcome")
    if outcome and outcome not in (None, "ok") and not row.get("ok"):
        # Finished this attempt (error) — keep it visible.
        if outcome != "captcha" or "restarting" not in (row.get("stage") or ""):
            hint = _OUTCOME_HINT.get(outcome, outcome)
            stage = row.get("stage")
            extra = f" — {_esc(stage)}" if stage and stage != hint else ""
            return (
                f"  ❌ W{wid} <code>{_esc(email)}</code> — "
                f"{_esc(hint)}{extra}"
            )
    stage = row.get("stage") or row.get("detail") or row.get("state") or "waiting"
    emoji = "⏳"
    for key, em in _STAGE_EMOJI.items():
        if key in str(stage):
            emoji = em
            break
    return f"  {emoji} W{wid} <code>{_esc(email)}</code> — {_esc(stage)}"


def _render_login_live(
    by_agent: dict,
    pending: set,
    done: dict,
    total_accounts: int,
) -> str:
    """Per-account live board while agents are still signing in."""
    out = ["🔐 <b>Auto-login</b>"]
    ok_n = fail_n = wait_n = 0
    seen_agents = sorted(set(list(by_agent) + list(done) + list(pending)))
    body = []
    for aid in seen_agents:
        final = done.get(aid)
        rows = (final or {}).get("results") if final else by_agent.get(aid)
        rows = rows or by_agent.get(aid) or []
        tag = "done" if aid in done else ("waiting" if aid in pending else "")
        body.append(f"\n<b>{_esc(aid)}</b>" + (f"  <i>{tag}</i>" if tag == "waiting" else ""))
        if (final or {}).get("error"):
            body.append(f"  ⚠️ {_esc(final['error'])}")
        if not rows:
            body.append("  ⏳ starting…")
            wait_n += 1
            continue
        for row in sorted(rows, key=lambda r: r.get("worker_id") or 0):
            body.append(_login_account_line(row))
            if row.get("ok"):
                ok_n += 1
            elif row.get("outcome") and aid in done:
                fail_n += 1
            else:
                wait_n += 1
        for un in (final or {}).get("unassigned") or []:
            body.append(
                f"  ⚠️ {_esc(un.get('email'))} — {_esc(un.get('reason'))}"
            )
    out.append(
        f"✅ {ok_n}  ❌ {fail_n}  ⏳ {wait_n}"
        + (f"  ·  {total_accounts} account(s)" if total_accounts else "")
    )
    if pending:
        out.append(
            f"⏱️ Agents still working: <code>{_esc(', '.join(sorted(pending)))}</code>"
        )
    out.extend(body)
    return "\n".join(out)


def _render_login_results(results: dict, pending: set) -> str:
    out = ["🔐 <b>Auto-login — result</b>"]
    if pending:
        out.append(
            f"⏱️ No reply from: <code>{_esc(', '.join(sorted(pending)))}</code>"
        )
    total = ok_total = 0
    for aid in sorted(results):
        msg = results[aid] or {}
        rows = msg.get("results") or []
        total += len(rows)
        ok_total += sum(1 for r in rows if r.get("ok"))
        out.append(f"\n<b>{_esc(aid)}</b>")
        if msg.get("error"):
            out.append(f"  ⚠️ {_esc(msg['error'])}")
        for row in sorted(rows, key=lambda r: r.get("worker_id", 0)):
            if row.get("ok"):
                out.append(
                    f"  ✅ W{row.get('worker_id')} {_esc(row.get('email'))} — "
                    f"<code>{_esc(row.get('phone_number') or 'no number yet')}</code>"
                )
            else:
                outcome = row.get("outcome") or "failed"
                hint = _OUTCOME_HINT.get(outcome, outcome)
                stage = row.get("stage") or row.get("detail")
                line = (
                    f"  ❌ W{row.get('worker_id')} {_esc(row.get('email'))} — "
                    f"{_esc(hint)}"
                )
                if stage and str(stage) not in hint:
                    line += f"\n     └ {_esc(stage)}"
                out.append(line)
        for un in msg.get("unassigned") or []:
            out.append(
                f"  ⚠️ {_esc(un.get('email'))} — {_esc(un.get('reason'))}"
            )
    out.insert(1, f"Signed in: <b>{ok_total}/{total}</b>")
    return "\n".join(out)


async def _usage(message, needs: list):
    text = [
        "🔐 <b>/login — automatic Google sign-in</b>",
        "",
        "Send the accounts in a single message, one per line:",
        "<code>/login\nuser1@gmail.com password1\nuser2@gmail.com password2</code>",
        "",
        "Also accepted: <code>user@gmail.com:password</code>, and an explicit "
        "target prefix <code>agent-01/w3 user@gmail.com password</code>.",
        "If Google asks to confirm the backup address, add it after the "
        "password: <code>user@gmail.com password recovery@gmail.com</code>.",
        "",
        "<b>2FA</b> — two ways, both optional and combinable:",
        "• authenticator: paste the 2fa.live link or the bare seed after the "
        "password: <code>user@gmail.com password https://2fa.live/tok/SEED</code>",
        "• backup codes: <code>user@gmail.com password 5lpg ul5m beax bjcy "
        "fhzz hj7h 6lts bfge</code>",
        "A card spread over several lines (email / password / phone / 2FA "
        "link each on its own line) is understood too — lines without an "
        "email continue the account above.",
        "",
        "<i>The message is deleted as soon as it is parsed.</i>",
    ]
    if needs:
        text += [
            "",
            f"Profiles waiting for a login ({len(needs)}):",
            "\n".join(
                f"{i}. <code>{_esc(n['agent'])}/w{n['worker_id']}</code> "
                f"<code>{_esc(n['profile'])}</code>"
                for i, n in enumerate(needs, start=1)
            ),
        ]
    else:
        text += ["", "Run /accounts first to see which profiles need one."]
    await _send_html(message, "\n".join(text))


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    raw = message.text or ""
    body = raw.split("\n", 1)[1] if "\n" in raw else ""
    # Allow a single account on the command line: /login user@gmail.com pass
    if not body.strip() and context.args:
        body = " ".join(context.args)

    needs = context.bot_data.get("login_pending") or []

    if not body.strip():
        await _usage(message, needs)
        return

    entries, errors = parse_credentials(body)

    # Get the secret out of the chat before doing anything slow with it.
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"/login: could not delete the credentials message: {e}")
        await message.reply_text(
            "⚠️ I could not delete your message — delete it by hand. "
            "(Give the bot 'Delete messages' rights in this chat.)"
        )

    chat = message.chat
    if errors:
        await chat.send_message(
            "⚠️ <b>Skipped lines</b>\n" + "\n".join(_esc(e) for e in errors),
            parse_mode="HTML",
        )
    if not entries:
        await chat.send_message("❌ No usable credentials found.")
        return

    bus = context.bot_data.get("bus")

    # ── Standalone: sign in on the local pool ──
    if bus is None:
        pool = context.bot_data.get("gv_pool")
        if not pool:
            await chat.send_message("⏳ Telephony pool is not ready yet.")
            return
        applied = pool.apply_credentials([
            {
                "email": e["email"], "password": e["password"],
                "recovery_email": e.get("recovery_email"),
                "totp_seed": e.get("totp_seed"),
                "backup_codes": e.get("backup_codes"),
                "worker": e.get("worker"), "profile": e.get("profile"),
            }
            for e in entries
        ])
        targets = [r["worker_id"] for r in applied["assigned"]]
        if not targets:
            await chat.send_message("⚠️ No local profile matched those accounts.")
            return
        live = {
            config.AGENT_ID: [
                {
                    "worker_id": r["worker_id"],
                    "email": r.get("email"),
                    "stage": "queued",
                    "ok": False,
                }
                for r in applied["assigned"]
            ]
        }
        status = await chat.send_message(
            _render_login_live(live, set(), {}, len(targets)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        last_edit = [0.0]

        async def _progress(rows):
            live[config.AGENT_ID] = rows
            now = time.time()
            if now - last_edit[0] < 1.0:
                return
            last_edit[0] = now
            try:
                await status.edit_text(
                    _render_login_live(live, set(), {}, len(targets)),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

        rows = await pool.auto_login_workers(targets, on_progress=_progress)
        final = _render_login_results(
            {config.AGENT_ID: {
                "results": rows, "unassigned": applied["unassigned"]
            }},
            set(),
        )
        try:
            await status.edit_text(
                final, parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception:
            await _send_html(chat, final)
        return

    # ── Controller: route to the agents that own the profiles ──
    try:
        online = await _agent_ids(bus)
    except Exception as e:
        await chat.send_message(f"❌ Redis error: {_esc(e)}", parse_mode="HTML")
        return
    if not online:
        await chat.send_message("⚠️ No agents online.")
        return

    if not needs:
        # No fresh inventory: ask the agents now so lines can be routed.
        await chat.send_message("🔎 No recent /accounts data — polling agents…")
        try:
            reports, _pending, _aids = await _gather_accounts(bus)
            _text, needs = _render_accounts(reports, set())
            context.bot_data["login_pending"] = needs
        except Exception as e:
            await chat.send_message(f"❌ Redis error: {_esc(e)}", parse_mode="HTML")
            return

    per_agent, unrouted = _route_entries(entries, needs)
    per_agent = {a: c for a, c in per_agent.items() if a in online}
    if not per_agent:
        await chat.send_message(
            "⚠️ Could not match any account to an online agent. "
            "Run /accounts, then resend."
        )
        return

    concurrency = int(getattr(config, "GV_LOGIN_CONCURRENCY", 2))
    timeout = int(os.getenv("GV_LOGIN_JOB_TIMEOUT_SEC", "900"))
    total_accounts = sum(len(c) for c in per_agent.values())
    live = {
        aid: [
            {
                "worker_id": c.get("worker"),
                "email": c.get("email"),
                "stage": "queued",
                "ok": False,
            }
            for c in creds
        ]
        for aid, creds in per_agent.items()
    }
    group = await bus.new_results_group()
    job_owner = {}
    status = None
    try:
        for aid, creds in per_agent.items():
            jid = await bus.dispatch_job(aid, {
                "type": "login",
                "params": {"credentials": creds, "concurrency": concurrency},
            })
            job_owner[jid] = aid

        pending_agents = set(job_owner.values())
        done = {}
        status = await chat.send_message(
            _render_login_live(live, pending_agents, done, total_accounts),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        last_edit = 0.0
        deadline = time.time() + timeout
        while pending_agents and time.time() < deadline:
            for entry_id, msg in await bus.consume_results(group, block_ms=1500):
                try:
                    await bus.ack_result(group, entry_id)
                    if msg.get("job_id") not in job_owner:
                        continue
                    aid = msg.get("agent_id") or job_owner[msg["job_id"]]
                    kind = msg.get("kind")
                    if kind == "login_progress":
                        live[aid] = msg.get("results") or live.get(aid) or []
                    elif kind == "login_done":
                        done[aid] = msg
                        live[aid] = msg.get("results") or live.get(aid) or []
                        pending_agents.discard(aid)
                except Exception as e:
                    logger.warning(f"login result handling error: {e}")
            now = time.time()
            if status is not None and now - last_edit >= 1.0:
                last_edit = now
                try:
                    await status.edit_text(
                        _render_login_live(
                            live, pending_agents, done, total_accounts
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
        results, pending = done, pending_agents
    finally:
        await bus.destroy_results_group(group)

    text = _render_login_results(results, pending)
    if unrouted:
        text += "\n\n⚠️ <b>Not routed</b>\n" + "\n".join(
            f"• {_esc(e['email'])} — no matching profile" for e in unrouted
        )
    edited = False
    if status is not None:
        try:
            await status.edit_text(
                text, parse_mode="HTML", disable_web_page_preview=True
            )
            edited = True
        except Exception:
            edited = False
    if not edited:
        await _send_html(chat, text)

    # Inventory is stale now; the next /login should re-poll.
    context.bot_data["login_pending"] = []


def register_accounts_handlers(application):
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("login", login_command))
    logger.info("Registered accounts/login handlers")
