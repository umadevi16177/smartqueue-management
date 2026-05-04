from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def test_catalogue() -> dict[str, Any]:
    return json.loads((DATA_DIR / "test_catalogue.json").read_text())


@lru_cache(maxsize=1)
def clinical_rules() -> dict[str, Any]:
    return json.loads((DATA_DIR / "clinical_rules.json").read_text())


@lru_cache(maxsize=1)
def prep_instructions() -> dict[str, Any]:
    return json.loads((DATA_DIR / "prep_instructions.json").read_text())


@lru_cache(maxsize=1)
def message_templates() -> dict[str, Any]:
    return json.loads((DATA_DIR / "messages.json").read_text())


def get_test(code: str) -> dict[str, Any] | None:
    for t in test_catalogue()["tests"]:
        if t["code"] == code:
            return t
    return None


def all_test_codes() -> list[str]:
    return [t["code"] for t in test_catalogue()["tests"]]


def render_message(key: str, lang: str, **vars: Any) -> str:
    tpl = message_templates().get(key, {}).get(lang) or message_templates().get(key, {}).get("en", "")
    try:
        return tpl.format(**vars)
    except KeyError:
        return tpl


def display_name(code: str, lang: str) -> str:
    t = get_test(code)
    if not t:
        return code
    return t["display"].get(lang) or t["display"].get("en") or code


def directions_for(code: str, lang: str) -> tuple[str, str, str]:
    """Return (floor, room, walking directions) in given language."""
    t = get_test(code) or {}
    return (
        t.get("floor", ""),
        t.get("room", ""),
        (t.get("directions") or {}).get(lang) or (t.get("directions") or {}).get("en", ""),
    )


def rest_period(after_test: str, before_test: str) -> dict[str, Any] | None:
    for r in clinical_rules().get("rest_periods", []):
        if r["after_test"] == after_test and r["before_test"] == before_test:
            return r
    return None


def reroute_permission(code: str) -> dict[str, bool]:
    return clinical_rules().get("reroute_permissions", {}).get(code, {})


def must_be_last() -> set[str]:
    return set(clinical_rules().get("must_be_last", []))


def precedence_pairs() -> list[tuple[str, str]]:
    """Return (before_code, after_code) ordering constraints."""
    return [(p["before"], p["after"]) for p in clinical_rules().get("must_precede", [])]
