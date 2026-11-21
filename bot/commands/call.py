"""
/call command - Make outbound AI calls with live transcription.
Telegram ConversationHandler implementation.
"""
import re
import asyncio
import time
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters
)
from typing import Optional
from loguru import logger

from storage.prompts import get_prompt, get_prompt_names
from console_ui import dashboard


# Conversation states
ASK_NAME, ASK_EMAIL, ASK_PROMPT = range(3)


def _max_batch_size() -> int:
    """Bulk-upload cap from config/.env. 0 = unlimited (the default)."""
    try:
        import config as _cfg
        n = int(getattr(_cfg, "MAX_BATCH_SIZE", 0) or 0)
        if n > 0:
            return n
    except Exception:
        pass
    try:
        import os
        n = int((os.getenv("MAX_BATCH_SIZE") or "0").strip() or "0")
        if n > 0:
            return n
    except Exception:
        pass
    return 0


# Back-compat alias (some code may reference the name)
MAX_BATCH_SIZE = _max_batch_size()

PHONE_CANDIDATE_RE = re.compile(
    r"""
    (?<!\d)
    (?:
        \+\d[\d\s().-]{8,}\d
        |
        (?:1[\s.-]*)?(?:\(\d{3}\)|\d{3})[\s.-]*\d{3}[\s.-]*\d{4}
    )
    (?!\d)
    """,
    re.VERBOSE,
)


def _escape_md(text: str) -> str:
    """Escape Telegram Markdown (v1) special chars in a dynamic value."""
    return re.sub(r"([_*`\[])", r"\\\1", str(text or ""))


def validate_phone(phone: str) -> "tuple[bool, str]":
    """Validate phone number format. Returns (is_valid, cleaned_number)."""
    cleaned = re.sub(r'[\s\-\(\).]', '', phone)

    if cleaned.startswith('+'):
        if re.match(r'^\+\d{10,15}$', cleaned):
            return True, cleaned
    elif re.match(r'^\d{10}$', cleaned):
        return True, f"+1{cleaned}"
    elif re.match(r'^1\d{10}$', cleaned):
        return True, f"+{cleaned}"

    return False, phone


def _extract_phones_from_line(line: str) -> list[str]:
    """Extract all valid phone numbers from a single text line."""
    line = line.strip()
    if not line:
        return []

    extracted: list[str] = []
    seen: set[str] = set()

    for match in PHONE_CANDIDATE_RE.finditer(line):
        is_valid, cleaned = validate_phone(match.group(0))
        if is_valid and cleaned not in seen:
            extracted.append(cleaned)
            seen.add(cleaned)

    return extracted


def _extract_phone_from_line(line: str) -> Optional[str]:
    """Extract the first valid phone number from a single text line."""
    phones = _extract_phones_from_line(line)
    return phones[0] if phones else None


async def _telegram_call_with_retry(
    op_name: str,
    op,
    *,
    attempts: int = 4,
    base_backoff: float = 0.8,
    required: bool = False,
):
    """
    Execute a Telegram API call with retry on transient network errors.
    Returns None when the operation is non-required and still fails.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except RetryAfter as e:
            retry_after = getattr(e, "retry_after", 1.0)
            if hasattr(retry_after, "total_seconds"):
                retry_after = retry_after.total_seconds()
            delay = float(retry_after) + 0.2
            logger.warning(f"{op_name} rate-limited. retry={attempt}/{attempts} wait={delay:.1f}s")
        except BadRequest as e:
            msg = str(e).lower()
            # Common harmless edit races should not burn retry budget.
            if "message is not modified" in msg or "message to edit not found" in msg:
                return None
            if required:
                raise
            logger.warning(f"{op_name} bad request ignored: {e}")
            return None
        except (TimedOut, NetworkError) as e:
            delay = min(6.0, base_backoff * (2 ** (attempt - 1)))
            logger.warning(
                f"{op_name} transient error ({type(e).__name__}). "
                f"retry={attempt}/{attempts} wait={delay:.1f}s"
            )
        except TelegramError as e:
            if required:
                raise
            logger.warning(f"{op_name} telegram error ignored: {type(e).__name__}: {e}")
            return None

        if attempt >= attempts:
            break
        await asyncio.sleep(delay)

    if required:
        raise TimedOut(f"{op_name} failed after {attempts} attempts")
    logger.warning(f"{op_name} failed after {attempts} attempts (non-fatal)")
    return None


async def _safe_reply_text(message, text: str, *, required: bool = False, **kwargs):
    return await _telegram_call_with_retry(
        "reply_text",
        lambda: message.reply_text(text, **kwargs),
        required=required,
    )


async def _safe_edit_text(message, text: str, *, required: bool = False, **kwargs):
    return await _telegram_call_with_retry(
        "edit_text",
        lambda: message.edit_text(text, **kwargs),
        required=required,
    )


async def _safe_bot_send(
    bot,
    *,
    chat_id: int,
    text: str,
    required: bool = False,
    attempts: int = 4,
    base_backoff: float = 0.8,
    **kwargs,
):
    return await _telegram_call_with_retry(
        "send_message",
        lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs),
        required=required,
        attempts=attempts,
        base_backoff=base_backoff,
    )


async def _safe_bot_edit(bot, *, chat_id: int, message_id: int, text: str, required: bool = False, **kwargs):
    return await _telegram_call_with_retry(
        "edit_message_text",
        lambda: bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, **kwargs),
        required=required,
    )


async def _safe_query_answer(query, *, required: bool = False, **kwargs):
    return await _telegram_call_with_retry(
        "callback_answer",
        lambda: query.answer(**kwargs),
        required=required,
    )


async def call_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /call <phone>"""
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/call <phone_number>`\n"
            "Example: `/call +15551234567` or `/call 555-123-4567`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    phone = " ".join(context.args)
    is_valid, cleaned_phone = validate_phone(phone)
    if not is_valid:
        await update.message.reply_text(
            f"❌ Invalid phone number format: `{phone}`\n"
            "Use format: `+15551234567` or `555-123-4567`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Store in user data for the conversation
    context.user_data["call_phone"] = cleaned_phone
    await update.message.reply_text("📝 *What is the name of the target?*", parse_mode="Markdown")
    return ASK_NAME


async def ask_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received target name, ask for email."""
    context.user_data["call_target_name"] = update.message.text.strip()
    await update.message.reply_text("📧 *What is the target's email?*", parse_mode="Markdown")
    return ASK_EMAIL


async def ask_email_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received target email, show prompt selection keyboard."""
    context.user_data["call_target_email"] = update.message.text.strip()

    prompt_names = await get_prompt_names()
    if not prompt_names:
        await update.message.reply_text("❌ No prompts found. Create one with `/prompt_add` first.")
        return ConversationHandler.END

    # Build inline keyboard with prompt options
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"select_prompt:{name}")]
        for name in prompt_names
    ]
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("🎭 *Which prompt?*", reply_markup=keyboard, parse_mode="Markdown")
    return ASK_PROMPT


async def prompt_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt selected via inline button — initiate the call."""
    query = update.callback_query
    await _safe_query_answer(query)

    prompt_name = query.data.replace("select_prompt:", "")
    prompt_data = await get_prompt(prompt_name)
    if not prompt_data:
        await _safe_edit_text(query.message, f"❌ Prompt `{prompt_name}` not found. Defaulting to built-in prompt.")
        prompt_name = "default"
        prompt_data = await get_prompt(prompt_name)
        if not prompt_data:
            prompt_data = {"name": "default", "content": "You are a helpful AI phone assistant."}

    cleaned_phone = context.user_data["call_phone"]
    target_name = context.user_data["call_target_name"]
    target_email = context.user_data["call_target_email"]

    # Mask the phone number for display
    masked = cleaned_phone[:4] + "...." + cleaned_phone[-4:]

    # Generate call ID
    call_id = str(uuid.uuid4())[:8]

    # Build simplified status message
    status_text = (
        f"📞 *Initiating Call*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Call ID: `{call_id}`\n"
        f"📊 Progress: 0/1\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Status: 🔄 Connecting..."
    )
    status_msg = await _telegram_call_with_retry(
        "init_status", lambda: query.edit_message_text(status_text, parse_mode="Markdown"),
        required=True,
    )
    chat_id = query.message.chat_id
    msg_id = status_msg.message_id

    # Store call info in bot_data
    active_calls = context.bot_data["active_calls"]
    active_calls[call_id] = {
        "phone": cleaned_phone,
        "prompt": prompt_data,
        "status": "connecting",
        "chat_id": chat_id,
        "message_id": msg_id,
        "user_id": query.from_user.id,
        "transcript": [],
        "vars": {"name": target_name, "email": target_email},
    }

    # Clear user_data
    for key in ("call_phone", "call_target_name", "call_target_email"):
        context.user_data.pop(key, None)

    # Controller mode → dispatch the call to an agent + live bridge.
    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.call_distributed import start_remote_call
        active_calls.pop(call_id, None)  # remote_calls tracks it instead
        await start_remote_call(
            context, bus, call_id=call_id, phone=cleaned_phone,
            masked=masked, prompt_data=prompt_data,
            call_vars={"name": target_name, "email": target_email},
            chat_id=chat_id, msg_id=msg_id,
        )
        return ConversationHandler.END

    # Try to initiate the call via Worker Pool
    gv_pool = context.bot_data.get("gv_pool")
    gv_client = None
    # Set True when a connected-call monitor task takes ownership of releasing the worker.
    monitor_owns_worker = False
    try:
        if gv_pool:
            logger.info(f"Waiting for available worker to initiate call {call_id} to {masked}")

            status_text = status_text.replace("🔄 Connecting...", "🔄 Waiting for available line...")
            await _safe_bot_edit(
                context.bot, chat_id=chat_id, message_id=msg_id,
                text=status_text, parse_mode="Markdown",
            )

            gv_client = await gv_pool.acquire()
            if not gv_client:
                raise Exception("No available lines in the pool.")

            # If a warm P1 handler is holding this worker's audio path, park it first.
            warm_p1 = getattr(gv_client, "_p1_handler", None)
            if warm_p1 and getattr(gv_client, "_p1_handler_warm", False):
                try:
                    await warm_p1.stop()
                except Exception:
                    pass
                setattr(gv_client, "_p1_handler_warm", False)

            logger.info(f"Acquired worker Mic({gv_client.capture_device}). Initiating call {call_id} to {masked}")
            status_text = status_text.replace("🔄 Waiting for available line...", "🔄 Initiating...")
            await _safe_bot_edit(
                context.bot, chat_id=chat_id, message_id=msg_id,
                text=status_text, parse_mode="Markdown",
            )

            success = await gv_client.make_call(cleaned_phone, call_id)

            if success:
                status_text = status_text.replace("🔄 Initiating...", "🔔 Ringing...")
                await _safe_bot_edit(
                    context.bot, chat_id=chat_id, message_id=msg_id,
                    text=status_text, parse_mode="Markdown",
                )

                from audio.call_handler import CallHandler
                handler = CallHandler(
                    capture_device=gv_client.capture_device,
                    playback_device=gv_client.playback_device
                )
                init_ok = await handler.initialize()
                if not init_ok:
                    logger.error("Failed to initialize audio devices for /call handler")
                    raise Exception("Audio device initialization failed")

                system_prompt = prompt_data.get('content', 'You are a helpful AI phone assistant.')
                greeting = prompt_data.get('greeting', '')

                call_vars = active_calls[call_id].get('vars', {})
                for key, val in call_vars.items():
                    system_prompt = system_prompt.replace('{' + key + '}', val)
                    greeting = greeting.replace('{' + key + '}', val)

                prepare_ok = await handler.prepare(system_prompt, greeting)
                if not prepare_ok:
                    logger.error("Failed to prepare Deepgram + audio pipeline")
                    await handler.stop()
                    raise Exception("Audio/Deepgram preparation failed")
                active_calls[call_id]['audio_handler'] = handler
                active_calls[call_id]['worker'] = gv_client

                connected = await gv_client.wait_for_connect(call_id, timeout=60)

                if connected:
                    operator_keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Success", callback_data=f"op_success:{call_id}"),
                            InlineKeyboardButton("❌ Failed", callback_data=f"op_failed:{call_id}"),
                            InlineKeyboardButton("⌛ Stall", callback_data=f"op_stall:{call_id}"),
                        ]
                    ])

                    status_text = status_text.replace("🔔 Ringing...", "✅ Connected!")
                    status_text += "\n\n💬 *Live Transcript:*\n_Listening for speech..._"
                    await _safe_bot_edit(
                        context.bot, chat_id=chat_id, message_id=msg_id,
                        text=status_text, parse_mode="Markdown",
                        reply_markup=operator_keyboard,
                    )

                    await _start_live_transcription(
                        context, call_id, chat_id, msg_id
                    )

                    # Monitor task takes ownership of releasing the worker on call end.
                    monitor_owns_worker = True
                    asyncio.create_task(
                        _monitor_call(context, call_id, chat_id, msg_id)
                    )
                else:
                    # Not answered — stop handler to release audio + WS resources
                    try:
                        await handler.stop()
                    except Exception:
                        pass
                    # Hang up the telephony line so GV stops ringing
                    try:
                        await gv_client.hangup(call_id)
                    except Exception:
                        pass
                    status_text = status_text.replace("🔔 Ringing...", "📵 Not Answered")
                    await _safe_bot_edit(
                        context.bot, chat_id=chat_id, message_id=msg_id,
                        text=status_text, parse_mode="Markdown",
                    )
                    del active_calls[call_id]
            else:
                status_text = status_text.replace("🔄 Initiating...", "❌ Failed to connect")
                await _safe_bot_edit(
                    context.bot, chat_id=chat_id, message_id=msg_id,
                    text=status_text, parse_mode="Markdown",
                )
                del active_calls[call_id]
        else:
            status_text = status_text.replace("🔄 Connecting...", "⚠️ Demo Mode (Not connected)")
            await _safe_bot_edit(
                context.bot, chat_id=chat_id, message_id=msg_id,
                text=status_text, parse_mode="Markdown",
            )
            logger.warning("Telephony client not initialized — running in demo mode")

    except Exception as e:
        logger.error(f"Call failed: {e}")
        # Clean up handler if it was created (prevents audio/WS leak)
        call_info = active_calls.get(call_id, {})
        leaked_handler = call_info.get('audio_handler') or (handler if 'handler' in locals() else None)
        if leaked_handler:
            try:
                await leaked_handler.stop()
            except Exception:
                pass
        # Hang up the telephony line if a call was placed
        if gv_client:
            try:
                await gv_client.hangup(call_id)
            except Exception:
                pass
        if 'msg_id' in locals():
            await _safe_bot_edit(
                context.bot, chat_id=chat_id, message_id=msg_id,
                text=f"\u274c Call failed: `{str(e)[:100]}`",
                parse_mode="Markdown",
            )
        active_calls.pop(call_id, None)
    finally:
        # For failed/not-answered calls, release the worker immediately.
        # For connected calls, _monitor_call.on_call_end() releases after the call ends.
        if gv_pool and gv_client and not monitor_owns_worker:
            await gv_pool.release(gv_client)

    return ConversationHandler.END


async def call_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the /call conversation."""
    for key in ("call_phone", "call_target_name", "call_target_email"):
        context.user_data.pop(key, None)
    await update.message.reply_text("❌ Call cancelled.")
    return ConversationHandler.END


# ── Live transcription helpers ──

async def _start_live_transcription(context, call_id, chat_id, message_id):
    """Start audio capture after call connects."""
    try:
        active_calls = context.bot_data["active_calls"]
        handler = active_calls[call_id].get('audio_handler')
        if not handler:
            logger.error(f"No audio handler found for call {call_id}")
            return

        async def on_transcript(speaker: str, text: str):
            if speaker == "sniper":
                await _add_transcript(context, call_id, "🎯🎯🎯", text, chat_id, message_id)
            else:
                display_speaker = "Caller" if speaker == "user" else "AI"
                await _add_transcript(context, call_id, display_speaker, text, chat_id, message_id)

        handler.set_callbacks(on_transcript=on_transcript)
        success = await handler.start()

        if success:
            logger.info(f"✅ Voice Agent active for call {call_id}")
        else:
            logger.error(f"Failed to start voice agent for call {call_id}")

    except Exception as e:
        logger.error(f"Failed to start live transcription: {e}")


async def _add_transcript(context, call_id, speaker, text, chat_id, message_id):
    """Add transcript entry and update the Telegram message."""
    active_calls = context.bot_data["active_calls"]
    if call_id not in active_calls:
        return

    call_info = active_calls[call_id]
    # Sanitize text for Telegram Markdown (escape special chars)
    safe_text = text.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("`", "\\`").replace("[", "\\[")
    call_info['transcript'].append({"speaker": speaker, "text": safe_text})
    # Cap transcript list to prevent unbounded memory growth
    if len(call_info['transcript']) > 200:
        call_info['transcript'] = call_info['transcript'][-100:]

    try:
        # Build transcript text (last 5 entries)
        transcript = call_info['transcript'][-5:]
        transcript_text = "\n".join([f"*{t['speaker']}*: {t['text']}" for t in transcript])

        status_text = (
            f"📞 *Call Active*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Call ID: `{call_id}`\n"
            f"📊 Progress: 1/1\n"
            f"📊 Status: ✅ Connected\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💬 *Live Transcript:*\n{transcript_text}"
        )

        operator_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Success", callback_data=f"op_success:{call_id}"),
                InlineKeyboardButton("❌ Failed", callback_data=f"op_failed:{call_id}"),
                InlineKeyboardButton("⌛ Stall", callback_data=f"op_stall:{call_id}"),
            ]
        ])

        await _safe_bot_edit(
            context.bot, chat_id=chat_id, message_id=message_id,
            text=status_text, parse_mode="Markdown", reply_markup=operator_keyboard,
        )
    except Exception as e:
        logger.debug(f"Failed to update transcript: {e}")


async def _monitor_call(context, call_id, chat_id, message_id):
    """Monitor call and update Telegram with status changes."""
    active_calls = context.bot_data["active_calls"]
    call_info = active_calls.get(call_id, {})
    # Worker is stored in active_calls when the call is connected.
    gv_client = call_info.get('worker')
    if not gv_client:
        logger.warning(f"_monitor_call: no worker stored for call {call_id} — monitor disabled")
        return

    async def on_call_end(cid):
        logger.info(f"Call {cid} ended — cleaning up")
        info = active_calls.get(cid, {})
        handler = info.get('audio_handler')
        if handler:
            try:
                await handler.stop()
                logger.info(f"Stopped audio handler for {cid}")
            except Exception:
                pass

        transcript = info.get('transcript', [])
        transcript_text = "\n".join([f"*{t['speaker']}*: {t['text']}" for t in transcript[-5:]])

        status_text = (
            f"📞 *Call Ended*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Call ID: `{cid}`\n"
            f"📊 Status: 📴 Call Ended\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        if transcript_text:
            status_text += f"💬 *Transcript:*\n{transcript_text}"

        await _safe_bot_edit(
            context.bot, chat_id=chat_id, message_id=message_id,
            text=status_text, parse_mode="Markdown",
        )

        active_calls.pop(cid, None)
        # Return worker to pool
        gv_pool = context.bot_data.get("gv_pool")
        if gv_pool and gv_client:
            await gv_pool.release(gv_client)

    await gv_client.monitor_call(call_id, on_call_end)


# ── Operator button callbacks ──

async def operator_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle operator inline button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "op_success:abc12345"
    parts = data.split(":", 1)
    if len(parts) != 2:
        return

    action, call_id = parts

    # Controller mode → forward operator action to the agent.
    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.call_distributed import route_operator
        if await route_operator(context, bus, call_id, action):
            await _safe_query_answer(
                query, text="💉 Sent to agent", show_alert=True
            )
            return

    active_calls = context.bot_data["active_calls"]
    call_info = active_calls.get(call_id)

    if not call_info:
        await _safe_query_answer(query, text="❌ Call already ended.", show_alert=True)
        return

    handler = call_info.get('audio_handler')
    if not handler or not handler.agent:
        await _safe_query_answer(query, text="❌ Agent not connected.", show_alert=True)
        return

    inject_messages = {
        "op_success": "The security code just worked. Michael's account is secured. Finish the call professionally and warmly.",
        "op_failed": "The system rejected that code. It might be expired. Calmly ask the caller to check for the LATEST code and read it back.",
        "op_stall": "I'm just syncing the security tokens on my end Michael, stay with me—this only takes a second.",
    }

    text = inject_messages.get(action)
    if text:
        await handler.agent.inject_message(text)
        await query.answer(f"💉 Injected: {text[:50]}...", show_alert=True)


# ── /hangup command ──

async def hangup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List lines that picked up then hung up, or terminate a call.

    /hangup           — inline-paginated list of hangup lines (10 per page)
    /hangup <call_id> — hang up the active call with that id (legacy action)
    """
    if not context.args:
        from bot.commands.p1_hangups import hangup_list_command
        await hangup_list_command(update, context)
        return

    # Controller mode → forward hangup to the agent running the call.
    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.call_distributed import route_hangup
        call_id = context.args[0]
        if await route_hangup(context, bus, call_id):
            await update.message.reply_text(
                f"📴 Hangup sent for `{call_id}`.", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ Call `{call_id}` not found.", parse_mode="Markdown"
            )
        return

    active_calls = context.bot_data["active_calls"]

    if not active_calls:
        await update.message.reply_text("❌ No active calls found.")
        return

    call_id = context.args[0]
    logger.info(f"Hangup requested for: {call_id}")

    if call_id not in active_calls:
        await update.message.reply_text(f"❌ Call `{call_id}` not found or already ended.", parse_mode="Markdown")
        return

    # Stop the audio handler
    call_info = active_calls.get(call_id, {})
    phone_client = call_info.get('worker')
    audio_handler = call_info.get('audio_handler')
    if audio_handler:
        try:
            await audio_handler.stop()
            if phone_client and getattr(phone_client, "_p1_handler", None) is audio_handler:
                setattr(phone_client, "_p1_handler_warm", False)
            logger.debug(f"Stopped audio handler for {call_id}")
        except Exception as e:
            logger.warning(f"Error stopping audio handler: {e}")

    # End call
    success = False
    if phone_client:
        success = await phone_client.hangup(call_id)

    # Cleanup
    active_calls.pop(call_id, None)

    # Release worker back to pool (prevents permanent worker leak)
    gv_pool = context.bot_data.get("gv_pool")
    if gv_pool and phone_client:
        await gv_pool.release(phone_client)

    msg = f"📴 Call `{call_id}` has been terminated." if success else f"⚠️ Requested hangup for `{call_id}`, but button not found (it might be already ended)."
    await update.message.reply_text(msg, parse_mode="Markdown")
    logger.info(f"Call {call_id} cleaned up (Action by user)")


# ── /p1 command — Scripted IVR call (supports bulk queue) ──


def _get_result_display(result: str) -> "tuple[str, str]":
    """Get display text and emoji for a P1 call result."""
    if result == "voicemail":
        return "📵 *VOICEMAIL DETECTED* — Call aborted", "📵"
    elif result == "1":
        return "🔴 *PRESSED 1 (DTMF)* — Suspicious activity confirmed", "🔴"
    elif result == "2":
        return "🟢 *PRESSED 2 (DTMF)* — Login authorized", "🟢"
    elif result == "not_answered":
        return "📵 *NOT ANSWERED* — Declined", "📵"
    elif result == "hangup":
        return "📞 *HUNG UP* — Remote party disconnected", "📞"
    elif result == "failed":
        return "❌ *FAILED* — Could not connect", "❌"
    elif result == "cancelled":
        return "🚫 *CANCELLED* — Queue stopped", "🚫"
    else:
        return "⚪ *NO RESPONSE* — Timed out", "⚪"


def _build_queue_status(numbers: "list[str]", results: dict, current_index: int, running: bool) -> str:
    """Build the live queue status message."""
    total = len(numbers)
    done = sum(1 for r in results.values() if r.get("status") == "done")
    pressed_1 = sum(1 for r in results.values() if r.get("result") == "1")
    pressed_2 = sum(1 for r in results.values() if r.get("result") == "2")
    
    header = (
        f"📞 *Bulk Queue*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Progress: {done}/{total}\n"
    )
    if pressed_1 or pressed_2:
        header += f"🚨 Hot hits: 1={pressed_1} | 2={pressed_2}\n"
    
    return header + "\n" + ("🔄 *Running*" if running else "✅ *Complete*")


def _build_queue_completion_summary(results: dict, line_map: dict | None = None) -> str:
    """Build one final summary message with critical hits called out explicitly."""
    counts: dict[str, int] = {}
    hot_hits: list[tuple[str, str]] = []
    line_map = line_map or {}

    for phone, info in results.items():
        result = info.get("result")
        if not result:
            continue
        counts[result] = counts.get(result, 0) + 1
        if result in {"1", "2"}:
            hot_hits.append((phone, result))

    done_count = sum(1 for r in results.values() if r.get("status") == "done")
    cancelled_count = sum(1 for r in results.values() if r.get("status") == "cancelled")
    total = len(results)

    lines = [
        "✅ *P1 bulk завершён*",
        f"Обработано: {done_count}/{total}",
        f"Отменено: {cancelled_count}",
        f"Pressed 1: {counts.get('1', 0)}",
        f"Pressed 2: {counts.get('2', 0)}",
        f"Voicemail: {counts.get('voicemail', 0)}",
        f"Not answered: {counts.get('not_answered', 0)}",
        f"Timeout: {counts.get('timeout', 0)}",
        f"Hangup: {counts.get('hangup', 0)}",
        f"Failed: {counts.get('failed', 0)}",
    ]

    if hot_hits:
        lines.append("")
        lines.append("*Hot Results*")
        for phone, result in hot_hits[:20]:
            label = "Pressed 1" if result == "1" else "Pressed 2"
            display = line_map.get(phone, phone)
            lines.append(f"- `{display}` {label}")
        if len(hot_hits) > 20:
            lines.append(f"- +{len(hot_hits) - 20} more")

    return "\n".join(lines)


async def _execute_p1_call(context, cleaned_phone: str, chat_id: int, user_id: int, gv_client, on_status=None) -> str:
    """
    Execute a single P1 scripted IVR call using an ALREADY ACQUIRED PhoneClient.
    Returns result string: '1', '2', 'timeout', 'voicemail', 'not_answered', 'failed'.
    on_status: optional async callback(status_str) for live status updates.
    """
    if not gv_client:
        return "failed"

    call_id = str(uuid.uuid4())[:8]
    active_calls = context.bot_data["active_calls"]
    handler = None
    monitor_task = None
    prepare_task = None

    active_calls[call_id] = {
        "phone": cleaned_phone,
        "status": "connecting",
        "type": "p1",
        "chat_id": chat_id,
        "user_id": user_id,
    }

    dashboard.update_worker(gv_client.worker_id, phone=cleaned_phone, status="Calling...", stt="")

    async def _cancel_prepare_task():
        nonlocal prepare_task
        if not prepare_task:
            return
        prepare_task.cancel()
        try:
            await prepare_task
        except (asyncio.CancelledError, Exception):
            pass
        prepare_task = None

    async def _finish_prepare(timeout: float, context_label: str) -> bool:
        nonlocal prepare_task
        if not prepare_task:
            is_warm = bool(
                handler
                and getattr(handler, "_prepared", False)
                and getattr(handler, "_listening", False)
            )
            if is_warm:
                setattr(gv_client, "_p1_handler_warm", True)
            return is_warm
        try:
            await asyncio.wait_for(prepare_task, timeout=timeout)
            setattr(gv_client, "_p1_handler_warm", True)
            return True
        except asyncio.TimeoutError:
            await _cancel_prepare_task()
            setattr(gv_client, "_p1_handler_warm", False)
            logger.warning(f"P1 handler prepare timed out during {context_label}; leaving handler cold")
            return False
        except Exception as e:
            prepare_task = None
            setattr(gv_client, "_p1_handler_warm", False)
            logger.warning(f"P1 handler prepare failed during {context_label}: {e}")
            return False
        finally:
            if prepare_task and prepare_task.done():
                prepare_task = None

    async def _ensure_worker_ready(reason: str, wait_timeout: float = 1.25) -> bool:
        ready = await gv_client.wait_until_dialer_ready(timeout=wait_timeout)
        if ready:
            logger.debug(f"Worker {gv_client.worker_id}: dialer ready after {reason}")
            return True
        logger.warning(
            f"Worker {gv_client.worker_id}: dialer still busy after {reason}; recovering state"
        )
        recovered = await gv_client.recover_dialer_state(reason)
        if not recovered:
            logger.warning(f"Worker {gv_client.worker_id}: dialer recovery failed after {reason}")
            return False
        ready = await gv_client.wait_until_dialer_ready(timeout=2.0)
        if not ready:
            logger.warning(
                f"Worker {gv_client.worker_id}: dialer still not ready after recovery ({reason})"
            )
            return False
        logger.info(f"Worker {gv_client.worker_id}: dialer recovered after {reason}")
        return True

    try:
        # Reuse a warm per-worker scripted handler when possible (STT/audio stay hot).
        from audio.scripted_handler import ScriptedCallHandler
        from audio.script_profiles import resolve_profile
        active_profile = context.bot_data.get("script_profile", "suspicious")
        scripts = resolve_profile(
            active_profile, context.bot_data.get("script_bundle")
        )
        handler = getattr(gv_client, "_p1_handler", None)
        cached_profile = getattr(gv_client, "_p1_handler_profile", None)
        script_sig = (active_profile, (scripts.get("intro") or (None,))[0])

        if handler and cached_profile != script_sig:
            try:
                await handler.stop()
            except Exception:
                pass
            handler = None
            setattr(gv_client, "_p1_handler", None)
            setattr(gv_client, "_p1_handler_profile", None)
            setattr(gv_client, "_p1_handler_warm", False)

        if handler is None:
            handler = ScriptedCallHandler(
                profile=active_profile,
                capture_device=gv_client.capture_device,
                playback_device=gv_client.playback_device,
                worker_id=gv_client.worker_id,
                scripts=scripts,
            )
            init_ok = await handler.initialize()
            if not init_ok:
                dashboard.update_worker(gv_client.worker_id, status="Failed Audio Init", stt="[Audio init failed]")
                active_calls.pop(call_id, None)
                return "failed"
            setattr(gv_client, "_p1_handler", handler)
            setattr(gv_client, "_p1_handler_profile", script_sig)
            setattr(gv_client, "_p1_handler_warm", False)

        handler.retain_recording = True
        active_calls[call_id]["audio_handler"] = handler
        active_calls[call_id]["worker"] = gv_client

        # Warm the handler before dialing so STT is already live by answer time.
        handler_is_hot = bool(
            getattr(gv_client, "_p1_handler_warm", False)
            and getattr(handler, "_prepared", False)
            and getattr(handler, "_listening", False)
        )
        if not handler_is_hot:
            prepare_task = asyncio.create_task(handler.prepare())

        # Initiate call while the handler warms in the background.
        success = await gv_client.make_call(cleaned_phone, call_id)
        if not success:
            dashboard.update_worker(gv_client.worker_id, status="Failed Setup")
            await _finish_prepare(12, "failed setup")
            await _ensure_worker_ready("p1 make_call failed", wait_timeout=1.0)
            active_calls.pop(call_id, None)
            return "failed"

        dashboard.update_worker(gv_client.worker_id, status="Ringing...")

        # Notify: ringing
        if on_status:
            await on_status("ringing")

        connected = await gv_client.wait_for_connect(call_id, timeout=30)

        if not connected:
            dashboard.update_worker(gv_client.worker_id, status="Not Answered", stt="[No Answer]")
            # Hang up first, then recover the worker for the next call if needed.
            await gv_client.hangup(call_id)
            await _finish_prepare(8, "no-answer cleanup")
            await _ensure_worker_ready("p1 no-answer cleanup")
            active_calls.pop(call_id, None)
            return "not_answered"

        dashboard.update_worker(gv_client.worker_id, status="Connected")
        # Notify: connected
        if on_status:
            await on_status("connected")

        # Connected — ensure setup finished (or restore if warm state was lost)
        if prepare_task:
            if not await _finish_prepare(20, "connected call"):
                logger.error("P1 prepare did not finish after connect; aborting call")
                await gv_client.hangup(call_id)
                await _ensure_worker_ready("p1 prepare failure")
                active_calls.pop(call_id, None)
                return "failed"
        elif not getattr(handler, "_listening", False):
            try:
                await handler.prepare()
                setattr(gv_client, "_p1_handler_warm", True)
            except Exception as e:
                setattr(gv_client, "_p1_handler_warm", False)
                logger.error(f"Warm handler re-prepare failed; aborting call: {e}")
                await gv_client.hangup(call_id)
                await _ensure_worker_ready("p1 warm re-prepare failure")
                active_calls.pop(call_id, None)
                return "failed"

        # Background monitor: detect if the other party hangs up
        # VM detection is handled separately by STT keyword matching
        async def _hangup_monitor():
            loop = asyncio.get_running_loop()
            consecutive_end_states = 0
            last_end_state = None
            monitor_errors = 0
            tick = 0
            await asyncio.sleep(1.5)  # Let the call settle before monitoring
            while True:
                try:
                    status = await loop.run_in_executor(None, gv_client.check_call_status)
                    if status in ("ended", "disconnected", "force_ended"):
                        if status == last_end_state:
                            consecutive_end_states += 1
                        else:
                            last_end_state = status
                            consecutive_end_states = 1

                        if consecutive_end_states >= 2:
                            if status == "ended":
                                logger.info(f"Remote hangup detected during P1 IVR")
                            elif status == "disconnected":
                                logger.warning(f"Call disconnected during P1 IVR")
                            else:
                                logger.warning(f"Call force-ended by Google")
                            handler.force_result("hangup")
                            return
                    else:
                        consecutive_end_states = 0
                        last_end_state = None

                    # Mid-call ban check every other tick (it changes far
                    # slower than call state and costs a Selenium round-trip)
                    tick += 1
                    if tick % 2 == 0 and hasattr(gv_client, 'check_ban_status'):
                        is_banned, reason = await loop.run_in_executor(None, gv_client.check_ban_status)
                        if is_banned:
                            logger.warning(f"BAN detected mid-call: {reason}")
                            handler.force_result("hangup")
                            return
                except Exception as e:
                    monitor_errors += 1
                    if monitor_errors in (1, 5, 20):
                        logger.debug(
                            f"Hangup monitor probe error for {call_id}: "
                            f"{type(e).__name__}: {e}"
                        )
                await asyncio.sleep(1.0)

        monitor_task = asyncio.create_task(_hangup_monitor())

        # Run the IVR flow with hard timeout
        try:
            result = await asyncio.wait_for(handler.run(), timeout=120)
        except asyncio.TimeoutError:
            logger.warning(f"IVR timed out after 120s for {call_id}")
            result = "timeout"

        # Cancel the hangup monitor
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Cleanup call state while keeping the handler warm on this worker.
        try:
            await gv_client.hangup(call_id)
        except Exception:
            pass
        active_calls.pop(call_id, None)
        logger.info(f"♻️ Cleanup complete for {call_id}")

        # Poll for the dialer so a ready worker can move on immediately.
        ready_for_next = await _ensure_worker_ready("p1 call cleanup")
        logger.info(f"Worker {gv_client.worker_id}: ready_for_next={ready_for_next} after {call_id}")
        dashboard.update_worker(gv_client.worker_id, status=f"Done: {result}", stt=f"[Finished - {result}]")
        return result or "timeout"

    except Exception as e:
        if monitor_task:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        if gv_client:
            setattr(gv_client, "_p1_handler_warm", False)
            try:
                await gv_client.hangup(call_id)
            except Exception:
                pass
            await _cancel_prepare_task()
            await _ensure_worker_ready("p1 exception cleanup")
        if handler is not None:
            try:
                await handler.stop()
            except Exception:
                pass
        dashboard.update_worker(gv_client.worker_id, status="Failed (Error)", stt="[Crashed]")
        logger.error(f"P1 call failed: {e}")
        active_calls.pop(call_id, None)
        return "failed"


async def _run_bulk_queue_workers(context, numbers, results, queue_state, chat_id, user_id, status_message_id):
    """
    Helper function to process a list of phone numbers using a concurrent async worker pool.
    Replaces the legacy sequential for-loop to support Pool parallelism.
    """
    gv_pool = context.bot_data.get("gv_pool")
    if not gv_pool:
        logger.error("No Pool found. Cannot run bulk queue.")
        return

    call_queue = asyncio.Queue()
    for i, phone in enumerate(numbers):
        call_queue.put_nowait((i, phone))

    telegram_lock = asyncio.Lock()
    notification_tasks: set[asyncio.Task] = set()

    def _track_notification(task: asyncio.Task):
        notification_tasks.add(task)

        def _done(done_task: asyncio.Task):
            notification_tasks.discard(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                logger.warning(f"Bulk notification task failed: {exc}")

        task.add_done_callback(_done)

    async def _refresh_queue_status(*, force: bool = False):
        if not status_message_id:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        last_refresh = float(queue_state.get("_last_status_refresh", 0.0))
        if not force and (now - last_refresh) < 4.0:
            return

        async with telegram_lock:
            now = loop.time()
            last_refresh = float(queue_state.get("_last_status_refresh", 0.0))
            if not force and (now - last_refresh) < 4.0:
                return

            queue_state["_last_status_refresh"] = now
            status_text = _build_queue_status(
                numbers, results, queue_state["current_index"], queue_state["running"]
            )
            await _safe_bot_edit(
                context.bot,
                chat_id=chat_id,
                message_id=status_message_id,
                text=status_text,
                parse_mode="Markdown",
            )

    async def _send_post_call_update(phone: str):
        from audio.call_recorder import CallRecorder

        recordings = results[phone].get("recordings")
        try:
            await _refresh_queue_status()

            result = results[phone].get("result")
            try:
                import config as _cfg
                _rec_results = set(
                    getattr(_cfg, "P1_SEND_RECORDING_RESULTS", ("1", "2"))
                )
            except Exception:
                _rec_results = {"1", "2", "hangup"}
            if result not in _rec_results:
                return

            result_line, emoji = _get_result_display(results[phone]["result"])
            line_map = queue_state.get("line_map") or {}
            display = line_map.get(phone, phone)
            header = "Hot P1 Result" if result in ("1", "2") else "P1 Result"
            async with telegram_lock:
                await _safe_bot_send(
                    context.bot,
                    chat_id=chat_id,
                    text=(
                        f"{emoji} *{header}*\n"
                        f"`{display}`\n"
                        f"Result: {result_line}"
                    ),
                    parse_mode="Markdown",
                    attempts=8,
                    base_backoff=1.0,
                )
                if recordings:
                    try:
                        from bot.p1_audio import send_p1_call_recordings
                        await send_p1_call_recordings(
                            context.bot,
                            chat_id,
                            phone=display,
                            result=result,
                            recordings=recordings,
                        )
                    except Exception as e:
                        logger.warning(f"P1 recording send failed: {e}")
        finally:
            CallRecorder.delete_paths(recordings)
            results[phone]["recordings"] = None

    async def worker():
        while True:
            if not queue_state["running"]:
                break

            try:
                i, phone = call_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            
            queue_state["current_index"] = i
            results[phone]["status"] = "calling"

            # Best-effort queue UI refresh: do not block line acquisition on Telegram edits.
            _track_notification(
                asyncio.create_task(
                    _refresh_queue_status(),
                    name=f"p1_status_{i}",
                )
            )

            # Acquire a worker from the pool
            gv_client = await gv_pool.acquire()
            if not gv_client:
                results[phone]["status"] = "failed"
                results[phone]["result"] = "failed"
                call_queue.task_done()
                continue

            logger.info(f"📞 Parallel Queue: Calling {phone[:4]}....{phone[-4:]} on Worker Audio({gv_client.capture_device})")
            
            recordings = None
            try:
                result = await _execute_p1_call(context, phone, chat_id, user_id, gv_client)
                results[phone]["status"] = "done"
                results[phone]["result"] = result
                if result == "hangup":
                    from bot.commands.p1_hangups import record_local_hangup
                    record_local_hangup(
                        context, phone,
                        (queue_state.get("line_map") or {}).get(phone, phone),
                    )
                handler = getattr(gv_client, "_p1_handler", None)
                if handler:
                    recordings = handler.pop_call_recordings()
                try:
                    import config as _cfg
                    _rec_results = set(
                        getattr(_cfg, "P1_SEND_RECORDING_RESULTS", ("1", "2"))
                    )
                except Exception:
                    _rec_results = {"1", "2", "hangup"}
                if result in _rec_results:
                    results[phone]["recordings"] = recordings
                    recordings = None
            except Exception as e:
                logger.error(f"Worker {phone} failed: {e}")
                results[phone]["status"] = "failed"
                results[phone]["result"] = "failed"
                result = "failed"
            finally:
                if recordings:
                    from audio.call_recorder import CallRecorder
                    CallRecorder.delete_paths(recordings)
                # CRITICAL: Always release the hardware so the next call in the queue can use it
                await gv_pool.release(gv_client)

            call_queue.task_done()
            _track_notification(
                asyncio.create_task(
                    _send_post_call_update(phone),
                    name=f"p1_notify_{i}",
                )
            )

    try:
        worker_count = max(1, len(gv_pool.workers))
        logger.info(f"Spawning {worker_count} concurrent workers for bulk queue...")
        tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        for i, r in enumerate(results_list):
            if isinstance(r, Exception):
                logger.error(f"Bulk worker {i} crashed: {r}")

        if notification_tasks:
            await asyncio.gather(*list(notification_tasks), return_exceptions=True)
    finally:
        # -- Post-batch cleanup (runs even if workers crash) --
        queue_state["running"] = False

        for phone in numbers:
            if results[phone]["status"] == "pending":
                results[phone] = {"status": "cancelled", "result": "cancelled"}

        await _refresh_queue_status(force=True)

        # Session-shaped record so the menu's Campaign results / export /
        # redial buttons work for local (standalone) campaigns too.
        from bot.commands.p1_dashboard import completion_keyboard
        counts: dict = {}
        by_result: dict = {}
        done_count = 0
        for phone, info in results.items():
            if info.get("status") == "done":
                done_count += 1
            res = info.get("result")
            if not res:
                continue
            counts[res] = counts.get(res, 0) + 1
            if res in ("1", "2", "hangup", "not_answered", "voicemail"):
                by_result.setdefault(res, []).append(phone)
        run_id = uuid.uuid4().hex[:12]
        session = {
            "run_id": run_id,
            "campaign_name": queue_state.get("campaign_name") or "campaign",
            "total": len(numbers),
            "done": done_count,
            "counts": counts,
            "by_result": by_result,
            "line_map": queue_state.get("line_map") or {},
            "running": False,
            "started_at": queue_state.get("started_at") or time.time(),
            "script_label": context.bot_data.get("script_profile", "suspicious"),
            "local": True,
        }
        history = context.bot_data.setdefault("campaign_history", {})
        history[run_id] = session
        while len(history) > 20:
            history.pop(next(iter(history)))

        await _safe_bot_send(
            context.bot,
            chat_id=chat_id,
            text=_build_queue_completion_summary(
                results, queue_state.get("line_map"),
            ),
            parse_mode="Markdown",
            attempts=8,
            base_backoff=1.0,
            reply_markup=completion_keyboard(session),
        )

        context.bot_data["p1_queue"] = None
        logger.info("P1 queue finished")


async def p1_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start P1 scripted IVR call(s). Supports multiple numbers for bulk queue."""
    if not context.args:
        await _safe_reply_text(
            update.message,
            "Usage: `/p1 <phone_number> [phone2] [phone3] ...`\n"
            "Single: `/p1 +15551234567`\n"
            "Bulk: `/p1 +15551234567 +15559876543 +15551112222`",
            parse_mode="Markdown",
        )
        return

    raw_numbers = context.args
    valid_numbers = []
    invalid_numbers = []
    line_map: dict[str, str] = {}

    for raw in raw_numbers:
        is_valid, cleaned = validate_phone(raw)
        if is_valid:
            valid_numbers.append(cleaned)
            line_map[cleaned] = raw.strip()
        else:
            invalid_numbers.append(raw)

    if invalid_numbers:
        invalid_str = ", ".join(f"`{n}`" for n in invalid_numbers)
        await _safe_reply_text(
            update.message,
            f"Skipping invalid numbers: {invalid_str}",
            parse_mode="Markdown",
        )

    if not valid_numbers:
        await _safe_reply_text(update.message, "No valid phone numbers provided.")
        return

    # Controller mode → fan out across agents (standalone path unchanged).
    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.p1_distributed import p1_distributed
        script_profile = context.bot_data.get("script_profile", "suspicious")
        await p1_distributed(
            update, context, bus, valid_numbers, script_profile,
            line_map=line_map, campaign_name="manual",
        )
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    worker_pool = context.bot_data.get("gv_pool")
    if not worker_pool:
        await _safe_reply_text(
            update.message,
            "Demo mode: telephony not connected.",
            parse_mode="Markdown",
        )
        return

    if len(valid_numbers) == 1:
        cleaned_phone = valid_numbers[0]
        call_id_display = str(uuid.uuid4())[:8]
        active_profile = context.bot_data.get("script_profile", "suspicious")
        from audio.script_profiles import profile_label as _script_label
        profile_label = _script_label(
            active_profile, context.bot_data.get("script_bundle")
        )

        status_msg = await _safe_reply_text(
            update.message,
            f"*P1 Scripted Call*\n"
            f"Script: {profile_label}\n"
            f"Number: `{cleaned_phone}`\n"
            f"Call ID: `{call_id_display}`\n"
            f"Status: Connecting...",
            parse_mode="Markdown",
        )

        async def _update_status(status_str: str):
            status_map = {
                "ringing": "Ringing...",
                "connected": "Connected - Playing script...",
            }
            label = status_map.get(status_str, status_str)
            if status_msg:
                await _safe_edit_text(
                    status_msg,
                    f"*P1 Scripted Call*\n"
                    f"Script: {profile_label}\n"
                    f"Number: `{cleaned_phone}`\n"
                    f"Call ID: `{call_id_display}`\n"
                    f"Status: {label}",
                    parse_mode="Markdown",
                )

        phone_client = await worker_pool.acquire()
        if not phone_client:
            if status_msg:
                await _safe_edit_text(status_msg, "No available lines.")
            else:
                await _safe_reply_text(update.message, "No available lines.")
            return

        recordings = None
        try:
            result = await _execute_p1_call(
                context, cleaned_phone, chat_id, user_id, phone_client, on_status=_update_status
            )
            handler = getattr(phone_client, "_p1_handler", None)
            if handler:
                recordings = handler.pop_call_recordings()

            result_line, emoji = _get_result_display(result)
            if status_msg:
                await _safe_edit_text(
                    status_msg,
                    f"*P1 Call Complete*\n"
                    f"Script: {profile_label}\n"
                    f"Number: `{cleaned_phone}`\n"
                    f"Result: {result_line}",
                    parse_mode="Markdown",
                )
            else:
                await _safe_reply_text(
                    update.message,
                    f"*P1 Call Complete*\nNumber: `{cleaned_phone}`\nResult: {result_line}",
                    parse_mode="Markdown",
                )

            short_result = result_line.split("—")[0].split("-")[0].strip()
            await _safe_bot_send(
                context.bot,
                chat_id=chat_id,
                text=f"{emoji} `{cleaned_phone}`: {short_result}",
                parse_mode="Markdown",
            )
            if result in {"1", "2"} and recordings:
                try:
                    from bot.p1_audio import send_p1_call_recordings
                    await send_p1_call_recordings(
                        context.bot,
                        chat_id,
                        phone=cleaned_phone,
                        result=result,
                        recordings=recordings,
                    )
                except Exception as e:
                    logger.warning(f"P1 recording send failed: {e}")
        finally:
            if recordings is None:
                handler = getattr(phone_client, "_p1_handler", None)
                if handler:
                    recordings = handler.pop_call_recordings()
            if recordings:
                from audio.call_recorder import CallRecorder
                CallRecorder.delete_paths(recordings)
            await worker_pool.release(phone_client)
        return

    p1_queue = context.bot_data.get("p1_queue")
    if p1_queue and p1_queue.get("running"):
        await _safe_reply_text(
            update.message,
            "A P1 queue is already running. Use `/p1stop` first.",
            parse_mode="Markdown",
        )
        return

    results = {phone: {"status": "pending"} for phone in valid_numbers}
    status_text = _build_queue_status(valid_numbers, results, 0, True)
    status_msg = await _safe_reply_text(update.message, status_text, parse_mode="Markdown")

    status_message_id = status_msg.message_id if status_msg else None
    queue_state = {
        "numbers": valid_numbers,
        "results": results,
        "current_index": 0,
        "running": True,
        "chat_id": chat_id,
        "status_message_id": status_message_id,
        "line_map": line_map,
        "campaign_name": "manual",
        "started_at": time.time(),
    }
    context.bot_data["p1_queue"] = queue_state

    # Fire batch as background task -- handler returns immediately
    # so the Telegram bot stays responsive to other commands.
    asyncio.create_task(
        _run_bulk_queue_workers(
            context, valid_numbers, results, queue_state, chat_id, user_id, status_message_id
        ),
        name="p1_batch",
    )

async def p1stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the P1 bulk queue immediately and hang up all active workers."""
    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.p1_distributed import p1stop_distributed
        await p1stop_distributed(update, context, bus)
        return

    p1_queue = context.bot_data.get("p1_queue")

    if not p1_queue or not p1_queue.get("running"):
        await _safe_reply_text(update.message, "No P1 queue is currently running.")
        return

    p1_queue["running"] = False
    await _safe_reply_text(
        update.message,
        "*P1 queue stopping...* terminating all active calls now.",
        parse_mode="Markdown",
    )
    logger.info("P1 queue stop requested: aborting active workers")

    active_calls = context.bot_data.get("active_calls", {})
    gv_pool = context.bot_data.get("gv_pool")

    calls_to_kill = [call_id for call_id, info in active_calls.items() if info.get("type") == "p1"]

    for call_id in calls_to_kill:
        handler = active_calls[call_id].get("audio_handler")
        if handler:
            try:
                await handler.stop()
            except Exception:
                pass

        if gv_pool:
            for worker in gv_pool.workers:
                if call_id in worker._active_calls:
                    try:
                        await worker.hangup(call_id)
                    except Exception:
                        pass

        active_calls.pop(call_id, None)
        logger.info(f"Force-killed active call {call_id} during queue abort")

async def p1file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle .txt file uploads and extract phone numbers for P1 bulk queue."""
    doc = update.message.document

    if not doc or not doc.file_name:
        return
    if not doc.file_name.lower().endswith(".txt"):
        return

    p1_queue = context.bot_data.get("p1_queue")
    if p1_queue and p1_queue.get("running"):
        await _safe_reply_text(
            update.message,
            "A P1 queue is already running. Use `/p1stop` first.",
            parse_mode="Markdown",
        )
        return

    processing_msg = await _safe_reply_text(update.message, "Processing file...")

    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = await file.download_as_bytearray()
        content = file_bytes.decode("utf-8", errors="ignore")

        lines = content.strip().splitlines()
        extracted: list[str] = []
        line_map: dict[str, str] = {}
        seen: set[str] = set()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            for phone in _extract_phones_from_line(line):
                if phone not in seen:
                    extracted.append(phone)
                    line_map[phone] = line
                    seen.add(phone)

        if not extracted:
            if processing_msg:
                await _safe_edit_text(processing_msg, "No phone numbers found in the file.")
            else:
                await _safe_reply_text(update.message, "No phone numbers found in the file.")
            return

        total_found = len(extracted)
        batch_cap = _max_batch_size()
        logger.info(
            f"P1 file upload: found={total_found} MAX_BATCH_SIZE={batch_cap or 'off'}"
        )
        if batch_cap > 0 and total_found > batch_cap:
            extracted = extracted[:batch_cap]

        # Campaign name comes from the file: Bobby.txt → "Bobby".
        campaign_name = (doc.file_name or "").rsplit(".", 1)[0].strip() or "campaign"

        context.bot_data["p1file_pending"] = {
            "numbers": extracted,
            "line_map": line_map,
            "chat_id": update.effective_chat.id,
            "user_id": update.effective_user.id,
            "campaign_name": campaign_name,
        }

        preview_lines = []
        for i, phone in enumerate(extracted[:20]):
            preview_lines.append(f"  {i + 1}. `{line_map.get(phone, phone)}`")
        if len(extracted) > 20:
            preview_lines.append(f"  _...and {len(extracted) - 20} more_")

        cap_note = ""
        if batch_cap > 0 and total_found > batch_cap:
            cap_note = f"\nFound {total_found} numbers, capped to {batch_cap}\n"

        preview_body = "\n".join(preview_lines)
        preview_text = (
            f"📞 *Campaign {_escape_md(campaign_name)}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*{len(extracted)} numbers* auto-formatted from the file\n"
            f"{cap_note}"
            f"{preview_body}\n\n"
            f"Start this campaign?"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Start Calling", callback_data="p1file_confirm"),
                InlineKeyboardButton("Cancel", callback_data="p1file_cancel"),
            ]
        ])

        if processing_msg:
            await _safe_edit_text(
                processing_msg,
                preview_text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await _safe_reply_text(
                update.message,
                preview_text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

    except Exception as e:
        logger.error(f"File processing error: {e}")
        err_text = f"Error processing file: `{str(e)[:100]}`"
        if processing_msg:
            await _safe_edit_text(processing_msg, err_text, parse_mode="Markdown")
        else:
            await _safe_reply_text(update.message, err_text, parse_mode="Markdown")

async def p1file_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirm/cancel actions for P1 file upload."""
    query = update.callback_query
    await _safe_query_answer(query)

    action = query.data
    pending = context.bot_data.get("p1file_pending")
    if not pending:
        await _telegram_call_with_retry(
            "query_edit_message_text",
            lambda: query.edit_message_text("No pending file upload found."),
        )
        return

    if action == "p1file_cancel":
        context.bot_data["p1file_pending"] = None
        await _telegram_call_with_retry(
            "query_edit_message_text",
            lambda: query.edit_message_text("Cancelled. No calls will be made."),
        )
        return

    numbers = pending["numbers"]
    line_map = pending.get("line_map") or {}
    chat_id = pending["chat_id"]
    user_id = pending["user_id"]
    campaign_name = pending.get("campaign_name") or "campaign"
    context.bot_data["p1file_pending"] = None

    bus = context.bot_data.get("bus")
    if bus is not None:
        from bot.commands.p1_distributed import p1_distributed
        script_profile = context.bot_data.get("script_profile", "suspicious")
        await _telegram_call_with_retry(
            "query_edit_message_text",
            lambda: query.edit_message_text(
                f"📞 Campaign {campaign_name} — dispatching "
                f"{len(numbers)} numbers to agents…"
            ),
        )
        await p1_distributed(
            update, context, bus, numbers, script_profile,
            line_map=line_map, campaign_name=campaign_name,
        )
        return

    p1_queue = context.bot_data.get("p1_queue")
    if p1_queue and p1_queue.get("running"):
        await _telegram_call_with_retry(
            "query_edit_message_text",
            lambda: query.edit_message_text(
                "A P1 queue started while waiting. Use `/p1stop` first.",
                parse_mode="Markdown",
            ),
        )
        return

    results = {phone: {"status": "pending"} for phone in numbers}
    status_text = _build_queue_status(numbers, results, 0, True)
    await _telegram_call_with_retry(
        "query_edit_message_text",
        lambda: query.edit_message_text(status_text, parse_mode="Markdown"),
    )
    status_message_id = query.message.message_id if query.message else None

    queue_state = {
        "numbers": numbers,
        "results": results,
        "current_index": 0,
        "running": True,
        "chat_id": chat_id,
        "status_message_id": status_message_id,
        "line_map": line_map,
        "campaign_name": campaign_name,
        "started_at": time.time(),
    }
    context.bot_data["p1_queue"] = queue_state

    # Fire as background task -- bot stays responsive
    asyncio.create_task(
        _run_bulk_queue_workers(
            context, numbers, results, queue_state, chat_id, user_id, status_message_id
        ),
        name="p1_file_batch",
    )

# ── Register handlers ──

def register_call_handlers(application):
    """Register all call-related handlers."""
    # Conversation handler for /call flow
    call_conv = ConversationHandler(
        entry_points=[CommandHandler("call", call_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name_received)],
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email_received)],
            ASK_PROMPT: [CallbackQueryHandler(prompt_selected, pattern=r"^select_prompt:")],
        },
        fallbacks=[CommandHandler("cancel", call_cancel)],
        per_message=False,
    )

    application.add_handler(call_conv)
    application.add_handler(CommandHandler("hangup", hangup_command))
    application.add_handler(CommandHandler("p1", p1_command))
    application.add_handler(CommandHandler("p1stop", p1stop_command))
    from bot.commands.p1_distributed import flushall_command
    application.add_handler(CommandHandler("flushall", flushall_command))
    application.add_handler(CallbackQueryHandler(operator_button_handler, pattern=r"^op_"))
    application.add_handler(CallbackQueryHandler(p1file_confirm_handler, pattern=r"^p1file_"))
    from bot.commands.p1_distributed import p1_backend_pick_handler
    application.add_handler(CallbackQueryHandler(p1_backend_pick_handler, pattern=r"^p1backend:"))
    application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), p1file_handler))

    logger.info("Registered call handlers")
