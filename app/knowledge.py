from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
FLOOR_MAPS_DIR = Path(__file__).resolve().parent / "static" / "floor_maps"

_FLOOR_MAP_FILES = {
    "BLOOD": "blood.png",
    "ECG": "ecg.png",
    "ULTRASOUND": "ultrasound.png",
    "XRAY": "xray.png",
}


def floor_map_path(code: str) -> str | None:
    """Absolute path to the floor-map PNG for `code`, or None if missing."""
    name = _FLOOR_MAP_FILES.get(code)
    if not name:
        return None
    p = FLOOR_MAPS_DIR / name
    return str(p) if p.exists() else None


REQUIRED_RULE_KEYS = {
    "canonical_order",
    "must_precede",
    "must_be_last",
    "rest_periods",
    "data_handoffs",
    "reroute_permissions",
}


def validate_clinical_rules(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors. Empty list = valid."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["Top-level JSON must be an object."]
    missing = REQUIRED_RULE_KEYS - data.keys()
    if missing:
        errors.append(f"Missing required keys: {sorted(missing)}")
        return errors
    canonical = data["canonical_order"]
    if not isinstance(canonical, list) or not all(isinstance(c, str) for c in canonical):
        errors.append("`canonical_order` must be a list of strings.")
        return errors
    valid_codes = set(canonical)
    catalogue_codes = set(all_test_codes())
    unknown_in_canonical = valid_codes - catalogue_codes
    if unknown_in_canonical:
        errors.append(
            f"`canonical_order` references unknown test codes (not in catalogue): {sorted(unknown_in_canonical)}"
        )
    if not isinstance(data["must_be_last"], list):
        errors.append("`must_be_last` must be a list.")
    else:
        for c in data["must_be_last"]:
            if c not in valid_codes:
                errors.append(f"`must_be_last` code {c!r} not in canonical_order.")
    if not isinstance(data["must_precede"], list):
        errors.append("`must_precede` must be a list.")
    else:
        for i, p in enumerate(data["must_precede"]):
            if not isinstance(p, dict) or "before" not in p or "after" not in p:
                errors.append(f"`must_precede`[{i}] must have `before` and `after` keys.")
                continue
            if p["before"] not in valid_codes:
                errors.append(f"`must_precede`[{i}].before {p['before']!r} not in canonical_order.")
            if p["after"] not in valid_codes:
                errors.append(f"`must_precede`[{i}].after {p['after']!r} not in canonical_order.")
            if p.get("before") == p.get("after"):
                errors.append(f"`must_precede`[{i}] has the same code on both sides.")
    perms = data["reroute_permissions"]
    if not isinstance(perms, dict):
        errors.append("`reroute_permissions` must be an object.")
    else:
        unknown_perms = set(perms.keys()) - valid_codes
        if unknown_perms:
            errors.append(
                f"`reroute_permissions` keys not in canonical_order: {sorted(unknown_perms)}"
            )
    return errors


def save_clinical_rules(data: dict[str, Any]) -> None:
    """Validate, write to disk, bust the in-memory cache.

    Raises ValueError with all validation errors joined if the data is invalid.
    """
    errors = validate_clinical_rules(data)
    if errors:
        raise ValueError("Invalid clinical rules:\n  - " + "\n  - ".join(errors))
    path = DATA_DIR / "clinical_rules.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    clinical_rules.cache_clear()


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
