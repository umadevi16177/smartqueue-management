"""Telegram Bot Engine + Push Alert Dispatcher.

Uses python-telegram-bot in webhook mode. The Conversation Flow Controller
(`app.flow`) does the actual work; this layer is just I/O.
"""
from __future__ import annotations

import logging

from app.config import settings
from app.flow import handle_message
from app.journey import get_patient_language, get_patient_voice_mode
from app.voice import synthesize

log = logging.getLogger(__name__)


def _client():
    if not settings.telegram_bot_token:
        return None
    try:
        from telegram import Bot

        return Bot(token=settings.telegram_bot_token)
    except Exception:
        log.exception("Failed to construct Telegram Bot")
        return None


async def process_update(update_dict: dict) -> None:
    """Translate a raw Telegram webhook payload into bot replies."""
    msg = update_dict.get("message") or update_dict.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""
    from_user = msg.get("from") or {}
    sender_name = (
        (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
        or from_user.get("username")
    )
    if not chat_id:
        return

    replies = handle_message(chat_id=chat_id, sender_name=sender_name, text=text)
    bot = _client()
    if bot is None:
        log.warning("Telegram bot not configured — would have sent: %s", replies)
        return
    voice_mode = get_patient_voice_mode(chat_id)
    lang = get_patient_language(chat_id) or settings.default_language
    for reply in replies:
        if not reply or not reply.text:
            continue
        try:
            if reply.photo:
                with open(reply.photo, "rb") as f:
                    await bot.send_photo(chat_id=chat_id, photo=f, caption=reply.text)
            else:
                await bot.send_message(chat_id=chat_id, text=reply.text)
            if voice_mode:
                audio = await synthesize(reply.text, lang)
                if audio:
                    await bot.send_voice(chat_id=chat_id, voice=audio)
        except Exception:
            log.exception("Failed to send Telegram message")


async def push_alert(chat_id: int, text: str) -> None:
    bot = _client()
    if bot is None:
        log.warning("push_alert not delivered (no bot): %s", text)
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        log.exception("Failed to push alert")


async def configure_webhook() -> str:
    bot = _client()
    if bot is None or not settings.telegram_webhook_url:
        return "skipped"
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
    )
    return settings.telegram_webhook_url
