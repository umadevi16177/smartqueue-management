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

    @classmethod
    def text(cls, body: str) -> "Reply":
        return cls(text=body)

    @classmethod
    def with_photo(cls, body: str, photo_path: str) -> "Reply":
        return cls(text=body, photo=photo_path)


def texts(items: Iterable[str]) -> list[Reply]:
    return [Reply(text=t) for t in items if t]
