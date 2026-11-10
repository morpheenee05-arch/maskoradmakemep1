"""Send the mixed P1 call recording to Telegram."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from loguru import logger
from telegram import InputFile


async def send_p1_call_recordings(
    bot,
    chat_id: int,
    *,
    phone: str,
    result: str,
    agent_id: str = "",
    recordings: Optional[dict],
) -> None:
    """Upload the stereo call after a hot P1 hit (result 1 or 2)."""
    if not recordings:
        return
    try:
        import config
        if not getattr(config, "P1_SEND_RECORDINGS", True):
            return
    except Exception:
        pass

    path = recordings.get("call") or recordings.get("remote") or recordings.get("local")
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    server = f" · <code>{agent_id}</code>" if agent_id else ""
    try:
        with p.open("rb") as fh:
            await bot.send_audio(
                chat_id=chat_id,
                audio=InputFile(fh, filename=p.name),
                caption=(
                    "🎧 Звонок · левый канал абонент, правый наш звук\n"
                    f"P1 <b>{result}</b> · <code>{phone}</code>{server}"
                ),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning(f"P1 recording send failed: {e}")


async def send_p1_audio_from_bus(bot, chat_id: int, msg: dict) -> None:
    """Controller: one Redis p1_audio message → Telegram audio file."""
    if msg.get("error"):
        logger.debug(f"p1_audio bus error: {msg.get('error')}")
        return
    b64 = msg.get("audio_b64")
    if not b64:
        return
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        logger.warning(f"p1_audio decode failed: {e}")
        return
    phone = msg.get("phone", "")
    result = msg.get("result", "?")
    agent_id = msg.get("agent_id", "")
    caption = msg.get("caption") or "🎧 Звонок · левый канал абонент, правый наш звук"
    server = f" · <code>{agent_id}</code>" if agent_id else ""
    filename = msg.get("filename") or "p1_call.wav"
    try:
        await bot.send_audio(
            chat_id=chat_id,
            audio=InputFile(raw, filename=filename),
            caption=(
                f"{caption}\n"
                f"P1 <b>{result}</b> · <code>{phone}</code>{server}"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"p1_audio telegram send failed: {e}")
