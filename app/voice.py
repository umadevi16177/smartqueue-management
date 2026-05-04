"""Voice Message Engine.

Renders bot replies as Telugu/Hindi/English audio using gTTS. Falls back to
text-only if gTTS isn't installed or network is unavailable. Returns an
in-memory bytes buffer suitable for `Bot.send_voice`.
"""
from __future__ import annotations

import asyncio
import io
import logging

log = logging.getLogger(__name__)


_LANG_MAP = {"te": "te", "hi": "hi", "en": "en"}


def _synthesize_sync(text: str, lang: str) -> bytes | None:
    try:
        from gtts import gTTS
    except ImportError:
        return None
    try:
        tts = gTTS(text=text, lang=_LANG_MAP.get(lang, "en"), slow=False)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue()
    except Exception:
        log.exception("gTTS synthesis failed")
        return None


async def synthesize(text: str, lang: str) -> bytes | None:
    """Async wrapper — gTTS is blocking, run in a thread."""
    if not text:
        return None
    return await asyncio.to_thread(_synthesize_sync, text, lang)
