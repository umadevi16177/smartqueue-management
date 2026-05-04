"""Conversation Flow Controller.

Orchestrates Telegram messages with the AI core and data layer.
Stateless: all state lives in SQLite (sessions / journeys / journey_steps).
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.journey import (
    apply_reroute,
    current_step,
    get_active_journey,
    get_latest_journey,
    get_or_create_patient,
    get_patient_language,
    issue_queue_token,
    mark_step_completed,
    reserve_slot,
    set_patient_language,
    start_journey,
)
from app.knowledge import (
    directions_for,
    display_name,
    prep_instructions,
    render_message,
    rest_period,
)
from app.llm import parse_test_request
from app.queue_store import department_unavailable, ensure_seeded, get_department
from app.reroute_engine import decide_reroute


LANG_COMMANDS = {
    "/english": "en",
    "/hindi": "hi",
    "/telugu": "te",
}


def handle_message(chat_id: int, sender_name: str | None, text: str) -> list[str]:
    """Return a list of reply strings (the bot may emit multiple bubbles)."""
    ensure_seeded()
    text = (text or "").strip()
    lang = get_patient_language(chat_id) or settings.default_language

    if text in ("/start", "start"):
        get_or_create_patient(chat_id, sender_name, lang)
        return [
            render_message("welcome", lang, hospital=settings.hospital_name),
        ]

    if text in LANG_COMMANDS:
        new_lang = LANG_COMMANDS[text]
        get_or_create_patient(chat_id, sender_name, new_lang)
        set_patient_language(chat_id, new_lang)
        return [render_message("language_set", new_lang)]

    if text == "/help":
        return [render_message("help", lang)]

    if text == "/status":
        return _status_messages(chat_id, lang)

    if text == "/retry":
        return [render_message("language_set", lang)]

    if text == "/confirm":
        return _confirm_pending_journey(chat_id, lang)

    if text.startswith("/done"):
        return _handle_done_command(chat_id, lang, text)

    # Free-text path: either initial test registration or feedback.
    latest = get_latest_journey(chat_id)
    if latest and latest["status"] == "done":
        return _record_feedback(chat_id, latest, lang, text)
    if latest and latest["current_index"] >= len(latest["steps"]):
        return _record_feedback(chat_id, latest, lang, text)

    parsed = parse_test_request(text)
    detected_lang = parsed.get("language") or lang
    tests = parsed.get("tests") or []
    if not tests:
        return [render_message("tests_not_recognised", lang)]

    # Auto-set language if we detected something different and patient hasn't set one yet.
    if get_patient_language(chat_id) is None:
        get_or_create_patient(chat_id, sender_name, detected_lang)
        set_patient_language(chat_id, detected_lang)
        lang = detected_lang
    else:
        get_or_create_patient(chat_id, sender_name, lang)

    j = start_journey(chat_id, tests)
    sequence_codes = [s["test_code"] for s in j["steps"]]
    sequence_str = " → ".join(display_name(c, lang) for c in sequence_codes)
    typed_str = ", ".join(display_name(c, lang) for c in tests)

    confirmation = render_message(
        "tests_recognised", lang, tests=typed_str, sequence=sequence_str
    )
    return [confirmation]


def _confirm_pending_journey(chat_id: int, lang: str) -> list[str]:
    journey = get_active_journey(chat_id)
    if not journey:
        return [render_message("tests_not_recognised", lang)]
    return _send_first_step(chat_id, journey, lang)


def _send_first_step(chat_id: int, journey: dict[str, Any], lang: str) -> list[str]:
    step = current_step(journey)
    if not step:
        return [render_message("all_done", lang)]
    code = step["test_code"]
    floor, _room, dirs = directions_for(code, lang)
    token = issue_queue_token(journey["id"], code)
    msgs = [
        render_message(
            "sequence_locked",
            lang,
            first_test=display_name(code, lang),
            floor=floor,
            directions=dirs,
            token=token,
        )
    ]
    pre = prep_instructions().get(code, {}).get("pre_test", {}).get(lang) or \
          prep_instructions().get(code, {}).get("pre_test", {}).get("en", "")
    if pre:
        msgs.append(pre)
    return msgs


def _handle_done_command(chat_id: int, lang: str, text: str) -> list[str]:
    """Patient (or staff via patient's bot) reports the current step done.

    Format: /done  -> marks current step done, advances the journey.
    """
    journey = get_active_journey(chat_id)
    if not journey:
        return [render_message("unknown", lang)]
    step = current_step(journey)
    if not step:
        return [render_message("all_done", lang)]
    completed_code = step["test_code"]
    journey = mark_step_completed(journey["id"], completed_code)

    # Post-test message.
    msgs: list[str] = []
    post = prep_instructions().get(completed_code, {}).get("post_test", {}).get(lang) or \
           prep_instructions().get(completed_code, {}).get("post_test", {}).get("en", "")
    if post:
        msgs.append(post)

    if journey["status"] == "done":
        msgs.append(render_message("all_done", lang))
        return msgs

    # Determine the next step (with rest period and reroute checks).
    next_step = current_step(journey)
    if not next_step:
        msgs.append(render_message("all_done", lang))
        return msgs

    # Rest period?
    rp = rest_period(completed_code, next_step["test_code"])
    if rp:
        msgs.append(
            render_message(
                "rest_required",
                lang,
                minutes=rp["minutes"],
                next_test=display_name(next_step["test_code"], lang),
                reason=rp.get(f"reason_{lang}") or rp.get("reason_en", ""),
            )
        )

    # Department availability check -> Reroute Engine.
    next_code = next_step["test_code"]
    if department_unavailable(next_code):
        sequence_codes = [s["test_code"] for s in journey["steps"]]
        decision = decide_reroute(
            sequence_codes, journey["current_index"], next_code
        )
        if decision.action == "reordered":
            journey = apply_reroute(journey["id"], decision.new_sequence)
            new_seq_str = " → ".join(display_name(c, lang) for c in decision.new_sequence)
            dept = get_department(next_code)
            availability = dept["availability"] if dept else "unavailable"
            msgs.append(
                render_message(
                    "rerouted",
                    lang,
                    department=display_name(next_code, lang),
                    new_sequence=new_seq_str,
                    availability=availability,
                )
            )
        elif decision.action == "reserved_slot":
            reserve_slot(journey["id"], next_code, decision.reserved_for_time or "")
            msgs.append(
                render_message(
                    "slot_reserved",
                    lang,
                    department=display_name(next_code, lang),
                    time=decision.reserved_for_time,
                )
            )
            return msgs

    # Send next-step instructions.
    next_step = current_step(journey)
    if next_step:
        floor, room, dirs = directions_for(next_step["test_code"], lang)
        token = issue_queue_token(journey["id"], next_step["test_code"])
        dept = get_department(next_step["test_code"])
        wait = dept["estimated_wait_minutes"] if dept else 0
        msgs.append(
            render_message(
                "next_step",
                lang,
                test=display_name(next_step["test_code"], lang),
                floor=floor,
                room=room,
                directions=dirs,
                token=token,
                wait=wait,
            )
        )
        pre = prep_instructions().get(next_step["test_code"], {}).get("pre_test", {}).get(lang) or \
              prep_instructions().get(next_step["test_code"], {}).get("pre_test", {}).get("en", "")
        if pre:
            msgs.append(pre)
    return msgs


def _status_messages(chat_id: int, lang: str) -> list[str]:
    journey = get_active_journey(chat_id)
    if not journey:
        return [render_message("help", lang)]
    lines: list[str] = []
    for s in journey["steps"]:
        marker = {
            "completed": "✓",
            "in_queue": "•",
            "reserved": "⏳",
            "rerouted": "↻",
            "pending": "·",
        }.get(s["department_status"], "·")
        lines.append(
            f"{marker} {s['step_index']+1}. {display_name(s['test_code'], lang)}"
            + (f"  [{s['queue_token']}]" if s.get("queue_token") else "")
            + (f"  ({s['reserved_for_time']})" if s.get("reserved_for_time") else "")
        )
    return ["\n".join(lines)]


def _record_feedback(chat_id: int, journey: dict[str, Any], lang: str, text: str) -> list[str]:
    from app.feedback import record_patient_feedback

    record_patient_feedback(journey_id=journey["id"], raw_text=text)
    return [render_message("feedback_thanks", lang)]
