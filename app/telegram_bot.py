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


def _get_bot(bot_type: str):
    token = None
    if bot_type == "registration":
        token = settings.registration_bot_token
    elif bot_type == "diagnostic":
        token = settings.diagnostic_bot_token
    elif bot_type == "hub":
        token = settings.hub_bot_token
    
    if not token:
        return None
    try:
        from telegram import Bot
        return Bot(token=token)
    except Exception:
        log.exception(f"Failed to construct Telegram Bot for {bot_type}")
        return None


async def _send_one_reply(bot, chat_id: int, reply, voice_mode: bool, lang: str, bot_type: str, edit_message_id: int | None = None) -> None:
    """Send or EDIT a message. For callback queries, we edit to keep the menu smooth."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import BadRequest

    if not reply or not reply.text:
        return
    markup = None
    if reply.buttons:
        rows = []
        for label, val in reply.buttons:
            if val.startswith("http"):
                rows.append([InlineKeyboardButton(text=label, url=val)])
            else:
                rows.append([InlineKeyboardButton(text=label, callback_data=val)])
        markup = InlineKeyboardMarkup(rows)
    
    try:
        # If we have an edit_message_id, try to edit the existing message instead of sending a new one
        if edit_message_id and not reply.photo:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=reply.text,
                    reply_markup=markup
                )
                return
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    return # ignore
                # fall through to send_message if edit fails
        
        if reply.photo:
            with open(reply.photo, "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id, photo=f, caption=reply.text, reply_markup=markup
                )
        else:
            await bot.send_message(chat_id=chat_id, text=reply.text, reply_markup=markup)
        
        if voice_mode:
            audio = await synthesize(reply.text, lang)
            if audio:
                await bot.send_voice(chat_id=chat_id, voice=audio)
    except Exception:
        log.exception(f"Failed to send Telegram message from {bot_type}")


async def process_update(update_dict: dict, bot_type: str = "diagnostic") -> None:
    """Translate a raw Telegram webhook payload into bot replies."""
    # 1. Handle regular messages
    msg = update_dict.get("message") or update_dict.get("edited_message")
    callback = update_dict.get("callback_query")

    chat_id = None
    text = ""
    sender_name = ""

    if msg:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = msg.get("text") or ""
        from_user = msg.get("from") or {}
        sender_name = (
            (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
            or from_user.get("username")
        )
    elif callback:
        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = callback.get("data") or ""
        from_user = callback.get("from") or {}
        sender_name = (
            (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
            or from_user.get("username")
        )
        # Acknowledge the callback to remove the spinner
        try:
            bot = _get_bot(bot_type)
            if bot:
                await bot.answer_callback_query(callback_query_id=callback.get("id"))
        except Exception:
            pass
    if not chat_id:
        return

    replies = handle_message(chat_id=chat_id, sender_name=sender_name, text=text, bot_type=bot_type)
    bot = _get_bot(bot_type)
    if bot is None:
        log.warning(f"Telegram bot {bot_type} not configured — would have sent: %s", replies)
        return
    
    voice_mode = get_patient_voice_mode(chat_id)
    lang = get_patient_language(chat_id) or settings.default_language
    
    # If this update was a callback, we want to edit the message it came from
    edit_id = callback.get("message", {}).get("message_id") if callback else None
    
    for reply in replies:
        await _send_one_reply(bot, chat_id, reply, voice_mode, lang, bot_type, edit_message_id=edit_id)
        # Only the first reply in a list can be an "edit"; subsequent ones must be new messages
        edit_id = None


async def push_replies(chat_id: int, replies, bot_type: str = "diagnostic") -> None:
    """Push a flow.handle_message() reply list out-of-band — same delivery path
    process_update() uses, so photos and inline-keyboard buttons survive even
    when the trigger came from the staff dashboard rather than a Telegram
    update."""
    bot = _get_bot(bot_type)
    if bot is None:
        log.warning(
            f"push_replies not delivered (no bot {bot_type}): %d replies skipped",
            len(replies or []),
        )
        return
    voice_mode = get_patient_voice_mode(chat_id)
    lang = get_patient_language(chat_id) or settings.default_language
    for reply in replies or []:
        await _send_one_reply(bot, chat_id, reply, voice_mode, lang, bot_type)


async def push_alert(chat_id: int, text: str, bot_type: str = "diagnostic") -> None:
    """Plain-text out-of-band notification (e.g. fasting reminder, slot-now-open).
    For staff-driven step completions use `push_replies` so floor-map photos
    survive."""
    bot = _get_bot(bot_type)
    if bot is None:
        log.warning(f"push_alert not delivered (no bot {bot_type}): %s", text)
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        log.exception(f"Failed to push alert from {bot_type}")


async def configure_webhooks() -> list[str]:
    import asyncio

    from telegram import Bot
    from telegram.error import RetryAfter

    results: list[str] = []
    if not settings.telegram_webhook_url:
        return ["skipped: no webhook url"]

    bot_configs = [
        ("registration", settings.registration_bot_token),
        ("diagnostic", settings.diagnostic_bot_token),
        ("hub", settings.hub_bot_token),
    ]

    base = settings.telegram_webhook_url.rstrip("/")
    for btype, token in bot_configs:
        if not token:
            continue
        url = f"{base}/{btype}"
        bot = Bot(token=token)
        # Telegram applies global flood control to set_webhook; on a noisy dev
        # restart it can rate-limit us mid-loop. Retry once after the requested
        # cooldown so a transient 429 doesn't leave the bot unhooked.
        for attempt in range(2):
            try:
                await bot.set_webhook(
                    url=url,
                    secret_token=settings.telegram_webhook_secret,
                )
                results.append(url)
                break
            except RetryAfter as e:
                wait = float(e.retry_after) + 0.5
                log.warning("Webhook %s flood-controlled, retrying in %.1fs", btype, wait)
                await asyncio.sleep(wait)
            except Exception:
                log.exception("Failed to configure webhook for %s", btype)
                break
        # Be polite between bots so we don't trip flood control on the next one.
        await asyncio.sleep(0.6)

    return results
