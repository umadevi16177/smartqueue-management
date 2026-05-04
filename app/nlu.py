from __future__ import annotations

from app.knowledge import test_catalogue


def detect_language(text: str) -> str:
    """Cheap script-based detection: Telugu, Devanagari, or English."""
    if not text:
        return "en"
    for ch in text:
        cp = ord(ch)
        if 0x0C00 <= cp <= 0x0C7F:
            return "te"
        if 0x0900 <= cp <= 0x097F:
            return "hi"
    return "en"


def extract_test_codes(text: str) -> list[str]:
    """Match free-form patient text against multilingual aliases.

    Order of returned codes follows the order they appear in the text — so the
    Sequence Engine can know what the patient typed vs. what the canonical
    medical order is.
    """
    if not text:
        return []
    lower = text.lower()
    found: list[tuple[int, str]] = []
    for entry in test_catalogue()["tests"]:
        code = entry["code"]
        for lang_aliases in entry["names"].values():
            for alias in lang_aliases:
                idx = lower.find(alias.lower())
                if idx >= 0:
                    found.append((idx, code))
                    break
    seen: set[str] = set()
    ordered: list[str] = []
    for _, code in sorted(found):
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered
