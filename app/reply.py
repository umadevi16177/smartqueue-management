"""Bot reply types.

`flow.handle_message` returns a list of these. Each Reply is one Telegram
message — text, optional photo (floor map), and optional voice (gTTS payload).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class Reply:
    text: str
    photo: str | None = None  # absolute path to a PNG, sent via send_photo
    # Inline-keyboard buttons. Two accepted shapes:
    #   1. Flat list of (label, val) tuples — each tuple becomes its own row
    #      (legacy single-column form).
    #   2. List of rows, where each row is itself a list of (label, val)
    #      tuples — lets us lay out grids like the test-selection menu.
    # `val` starting with 'http' is rendered as a URL button, otherwise as
    # callback_data.
    buttons: list | None = None
    # When this Reply is the response to a callback_query (button tap), the
    # `toast` is shown to the user via `answer_callback_query(text=...)` —
    # an instant top-of-screen popup that doesn't depend on the slow
    # `edit_message_text` round-trip succeeding. Lets the user see "✅ Blood
    # Test added" within ~200ms even when the menu re-render is laggy.
    toast: str | None = None
    # If True and we're editing an existing message, use
    # `edit_message_reply_markup` (buttons-only) instead of
    # `edit_message_text` (text + buttons). Smaller payload, faster Telegram
    # ACK, faster ✅ visual update on the button. Only valid when the
    # message body hasn't changed.
    markup_only: bool = False

    @classmethod
    def text(cls, body: str) -> "Reply":
        return cls(text=body)

    @classmethod
    def with_photo(cls, body: str, photo_path: str) -> "Reply":
        return cls(text=body, photo=photo_path)


def texts(items: Iterable[str]) -> list[Reply]:
    return [Reply(text=t) for t in items if t]
