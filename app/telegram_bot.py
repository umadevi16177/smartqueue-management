"""Telegram Bot Engine + Push Alert Dispatcher.

Uses python-telegram-bot in webhook mode. The Conversation Flow Controller
(`app.flow`) does the actual work; this layer is just I/O.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.flow import handle_message
from app.journey import get_patient_language, get_patient_voice_mode
from app.voice import synthesize

log = logging.getLogger(__name__)


# Per-bot-type Bot cache. Each Bot owns an httpx.AsyncClient with its own
# connection pool to api.telegram.org. Reusing the same Bot across webhook
# calls keeps connections warm — building a fresh Bot per request was
# triggering cold-connect TimedOut errors under burst load (button mashing),
# which silently dropped replies even though the webhook returned 200.
_BOTS: dict[str, object] = {}


def _get_bot(bot_type: str):
    if bot_type in _BOTS:
        return _BOTS[bot_type]

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
        from telegram.request import HTTPXRequest
        # Bump httpx timeouts above the 5s default. Telegram's API can be
        # slow from some networks (especially the first call after idle), and
        # retries below give us a second chance — but only if the first
        # attempt didn't already exceed the connect budget.
        # Tight timeouts — fail fast and let `_retrying` warm the pool. Long
        # connect timeouts (15s+) make taps feel dead on slow networks because
        # `edit_message_text` sits blocked while the user mashes more buttons.
        request = HTTPXRequest(connect_timeout=5.0, read_timeout=8.0, write_timeout=8.0, pool_timeout=3.0)
        bot = Bot(token=token, request=request)
        _BOTS[bot_type] = bot
        return bot
    except Exception:
        log.exception(f"Failed to construct Telegram Bot for {bot_type}")
        return None


async def _retrying(fn, *args, **kwargs):
    """Run a Bot API call with one retry on transient timeouts. Most
    `TimedOut` errors are cold-connect blips; a single retry on a warmed pool
    almost always succeeds."""
    from telegram.error import TimedOut, NetworkError
    last: Exception | None = None
    for attempt in range(2):
        try:
            return await fn(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            last = e
            if attempt == 0:
                await asyncio.sleep(0.3)
                continue
            raise
    if last is not None:
        raise last


async def _send_one_reply(bot, chat_id: int, reply, voice_mode: bool, lang: str, bot_type: str, edit_message_id: int | None = None) -> None:
    """Send or EDIT a message. For callback queries, we edit to keep the menu smooth."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import BadRequest

    if not reply or not reply.text:
        return
    markup = None
    if reply.buttons:
        rows = []
        for item in reply.buttons:
            # `item` is either (label, val) — a one-button row in legacy
            # callers — or a list[(label, val), ...] — a row of N buttons in
            # callers that lay out grids (test menu, symptom picker, etc.).
            row_items = [item] if isinstance(item, tuple) else list(item)
            row = []
            for label, val in row_items:
                if val.startswith("http"):
                    row.append(InlineKeyboardButton(text=label, url=val))
                else:
                    row.append(InlineKeyboardButton(text=label, callback_data=val))
            rows.append(row)
        markup = InlineKeyboardMarkup(rows)
    
    try:
        # If we have an edit_message_id, try to edit the existing message instead of sending a new one
        if edit_message_id and not reply.photo:
            # Buttons-only edit: when the body text hasn't changed (test-menu
            # toggles, language switches), `edit_message_reply_markup` is
            # ~3x faster than `edit_message_text` because it sends a smaller
            # payload and Telegram doesn't re-render the message body.
            if getattr(reply, "markup_only", False) and markup is not None:
                try:
                    await _retrying(
                        bot.edit_message_reply_markup,
                        chat_id=chat_id,
                        message_id=edit_message_id,
                        reply_markup=markup,
                    )
                    return
                except BadRequest as e:
                    if "Message is not modified" in str(e):
                        return
                    # Fall through to text-edit if markup-only fails

            try:
                await _retrying(
                    bot.edit_message_text,
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=reply.text,
                    reply_markup=markup,
                )
                return
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    return # ignore
                # fall through to send_message if edit fails

        if reply.photo:
            with open(reply.photo, "rb") as f:
                await _retrying(
                    bot.send_photo,
                    chat_id=chat_id, photo=f, caption=reply.text, reply_markup=markup,
                )
        else:
            await _retrying(
                bot.send_message,
                chat_id=chat_id, text=reply.text, reply_markup=markup,
            )

        if voice_mode:
            audio = await synthesize(reply.text, lang)
            if audio:
                await _retrying(bot.send_voice, chat_id=chat_id, voice=audio)
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
    if not chat_id:
        return

    replies = handle_message(chat_id=chat_id, sender_name=sender_name, text=text, bot_type=bot_type)
    bot = _get_bot(bot_type)
    if bot is None:
        log.warning(f"Telegram bot {bot_type} not configured — would have sent: %s", replies)
        return

    # Skip voice TTS on button-tap responses — synthesis adds 1-3s of
    # latency per reply and the toast already confirmed the action.
    voice_mode = False if callback else get_patient_voice_mode(chat_id)
    lang = get_patient_language(chat_id) or settings.default_language

    edit_id = callback.get("message", {}).get("message_id") if callback else None

    # For callback responses, run the toast and the message edit
    # CONCURRENTLY via asyncio.gather. Sequentially they cost
    # toast_time + edit_time; in parallel they cost max(toast_time,
    # edit_time) — typically ~300-500ms instead of ~1-2s. This is what
    # makes the green ✅ on the test button appear "immediately" after a tap.
    if callback and replies:
        toast_text = getattr(replies[0], "toast", None)

        async def _do_toast():
            try:
                await _retrying(
                    bot.answer_callback_query,
                    callback_query_id=callback.get("id"),
                    text=toast_text,
                )
            except Exception:
                pass

        async def _do_edit():
            await _send_one_reply(bot, chat_id, replies[0], voice_mode, lang, bot_type, edit_message_id=edit_id)

        await asyncio.gather(_do_toast(), _do_edit())

        # Send any tail replies (rare — most callbacks return one reply)
        for reply in replies[1:]:
            await _send_one_reply(bot, chat_id, reply, voice_mode, lang, bot_type)
        return

    # Non-callback path (typed messages): plain sequential send
    for reply in replies:
        await _send_one_reply(bot, chat_id, reply, voice_mode, lang, bot_type, edit_message_id=edit_id)
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
        await _retrying(bot.send_message, chat_id=chat_id, text=text)
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
