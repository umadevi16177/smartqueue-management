"""Conversation Flow Controller.

Orchestrates Telegram messages with the AI core and data layer.
Stateless: all state lives in SQLite (sessions / journeys / journey_steps).
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.journey import (
    apply_reroute,
    claim_patient_by_id,
    current_step,
    findings_on_journey,
    get_active_journey,
    get_latest_journey,
    get_or_create_patient,
    get_patient_identifier,
    get_patient_language,
    issue_queue_token,
    mark_step_completed,
    reserve_slot,
    set_patient_language,
    set_patient_voice_mode,
    start_journey,
)
from app.knowledge import (
    directions_for,
    display_name,
    floor_map_path,
    prep_instructions,
    render_message,
    rest_period,
)
from app.llm import parse_test_request
from app.queue_store import department_unavailable, ensure_seeded, get_department
from app.reply import Reply
from app.reroute_engine import decide_reroute


LANG_COMMANDS = {
    "/english": "en",
    "/hindi": "hi",
    "/telugu": "te",
}

# Patients in the wild type the language name without the slash. Accept
# common forms in three scripts so the welcome step doesn't dead-end.
LANG_ALIASES = {
    "english": "en", "eng": "en",
    "hindi": "hi", "हिंदी": "hi", "हिन्दी": "hi",
    "telugu": "te", "తెలుగు": "te",
}


def handle_message(chat_id: int, sender_name: str | None, text: str) -> list[Reply]:
    """Return a list of Reply objects (one per Telegram message bubble)."""
    ensure_seeded()
    text = (text or "").strip()
    lang = get_patient_language(chat_id) or settings.default_language

    if text in ("/start", "start"):
        get_or_create_patient(chat_id, sender_name, lang)
        return [Reply(render_message("welcome", lang, hospital=settings.hospital_name))]

    if text in LANG_COMMANDS:
        new_lang = LANG_COMMANDS[text]
        get_or_create_patient(chat_id, sender_name, new_lang)
        set_patient_language(chat_id, new_lang)
        return [Reply(render_message(_post_language_key(chat_id), new_lang))]

    # Lower-case alias (no slash) — only triggers BEFORE a journey is started,
    # so a patient typing "english" mid-flow doesn't reset their language.
    alias_lang = LANG_ALIASES.get(text.lower())
    if alias_lang and not get_active_journey(chat_id):
        get_or_create_patient(chat_id, sender_name, alias_lang)
        set_patient_language(chat_id, alias_lang)
        return [Reply(render_message(_post_language_key(chat_id), alias_lang))]

    if text == "/voice":
        get_or_create_patient(chat_id, sender_name, lang)
        set_patient_voice_mode(chat_id, True)
        return [Reply(render_message("voice_on", lang))]

    if text == "/text":
        get_or_create_patient(chat_id, sender_name, lang)
        set_patient_voice_mode(chat_id, False)
        return [Reply(render_message("voice_off", lang))]

    if text == "/help":
        return [Reply(render_message("help", lang))]

    if text == "/status":
        return _status_messages(chat_id, lang)

    if text == "/retry":
        return [Reply(render_message("language_set", lang))]

    if text == "/confirm":
        return _confirm_pending_journey(chat_id, lang)

    if text.startswith("/done"):
        return _handle_done_command(chat_id, lang, text)

    # Free-text path: feedback (after journey done), patient ID intake
    # (before any journey), or test registration.
    latest = get_latest_journey(chat_id)
    if latest and latest["status"] == "done":
        return _record_feedback(chat_id, latest, lang, text)
    if latest and latest["current_index"] >= len(latest["steps"]):
        return _record_feedback(chat_id, latest, lang, text)

    # Patient claims a staff-issued ID. The patient record was created by
    # the hospital staff via the dashboard; this just binds the Telegram
    # chat_id to it.
    if get_patient_identifier(chat_id) is None and not get_active_journey(chat_id):
        candidate = text.strip()
        if not _looks_like_id(candidate):
            return [Reply(render_message("invalid_id", lang))]
        claimed = claim_patient_by_id(chat_id, candidate)
        if claimed is None:
            return [Reply(render_message("id_not_found", lang, patient_id=candidate))]
        # Re-apply language/sender_name on the merged patient row.
        get_or_create_patient(chat_id, sender_name, lang)
        return [
            Reply(
                render_message(
                    "claimed",
                    lang,
                    name=claimed["display_name"] or "patient",
                    patient_id=claimed["patient_id"],
                    sequence_number=claimed["sequence_number"],
                )
            )
        ]

    parsed = parse_test_request(text)
    detected_lang = parsed.get("language") or lang
    tests = parsed.get("tests") or []
    if not tests:
        return [Reply(render_message("tests_not_recognised", lang))]

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
    return [
        Reply(
            render_message("tests_recognised", lang, tests=typed_str, sequence=sequence_str)
        )
    ]


def _confirm_pending_journey(chat_id: int, lang: str) -> list[Reply]:
    journey = get_active_journey(chat_id)
    if not journey:
        # Distinguish "you haven't told me your tests yet" from "I couldn't
        # parse what you typed". /confirm before any prescription = the
        # former.
        return [Reply(render_message("send_tests_first", lang))]
    return _send_first_step(chat_id, journey, lang)


def _send_first_step(chat_id: int, journey: dict[str, Any], lang: str) -> list[Reply]:
    step = current_step(journey)
    if not step:
        return [Reply(render_message("all_done", lang))]
    code = step["test_code"]
    floor, _room, dirs = directions_for(code, lang)
    token = issue_queue_token(journey["id"], code)
    body = render_message(
        "sequence_locked",
        lang,
        first_test=display_name(code, lang),
        floor=floor,
        directions=dirs,
        token=token,
    )
    map_path = floor_map_path(code)
    replies: list[Reply] = [Reply(text=body, photo=map_path)]
    pre = (
        prep_instructions().get(code, {}).get("pre_test", {}).get(lang)
        or prep_instructions().get(code, {}).get("pre_test", {}).get("en", "")
    )
    if pre:
        replies.append(Reply(pre))
    return replies


def _handle_done_command(chat_id: int, lang: str, text: str) -> list[Reply]:
    """Patient (or staff via patient's bot) reports the current step done.

    Format: /done  -> marks current step done, advances the journey.
    """
    journey = get_active_journey(chat_id)
    if not journey:
        return [Reply(render_message("unknown", lang))]
    step = current_step(journey)
    if not step:
        return [Reply(render_message("all_done", lang))]
    completed_code = step["test_code"]
    journey = mark_step_completed(journey["id"], completed_code)

    replies: list[Reply] = []
    post = (
        prep_instructions().get(completed_code, {}).get("post_test", {}).get(lang)
        or prep_instructions().get(completed_code, {}).get("post_test", {}).get("en", "")
    )
    if post:
        replies.append(Reply(post))

    if journey["status"] == "done":
        replies.append(Reply(render_message("all_done", lang)))
        return replies

    next_step = current_step(journey)
    if not next_step:
        replies.append(Reply(render_message("all_done", lang)))
        return replies

    rp = rest_period(completed_code, next_step["test_code"])
    if rp:
        replies.append(
            Reply(
                render_message(
                    "rest_required",
                    lang,
                    minutes=rp["minutes"],
                    next_test=display_name(next_step["test_code"], lang),
                    reason=rp.get(f"reason_{lang}") or rp.get("reason_en", ""),
                )
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
            replies.append(
                Reply(
                    render_message(
                        "rerouted",
                        lang,
                        department=display_name(next_code, lang),
                        new_sequence=new_seq_str,
                        availability=availability,
                    )
                )
            )
        elif decision.action == "reserved_slot":
            reserve_slot(journey["id"], next_code, decision.reserved_for_time or "")
            replies.append(
                Reply(
                    render_message(
                        "slot_reserved",
                        lang,
                        department=display_name(next_code, lang),
                        time=decision.reserved_for_time,
                    )
                )
            )
            return replies

    # Send next-step instructions with the floor map.
    next_step = current_step(journey)
    if next_step:
        nc = next_step["test_code"]
        floor, room, dirs = directions_for(nc, lang)
        token = issue_queue_token(journey["id"], nc)
        dept = get_department(nc)
        wait = dept["estimated_wait_minutes"] if dept else 0
        body = render_message(
            "next_step",
            lang,
            test=display_name(nc, lang),
            floor=floor,
            room=room,
            directions=dirs,
            token=token,
            wait=wait,
        )
        replies.append(Reply(text=body, photo=floor_map_path(nc)))
        pre = (
            prep_instructions().get(nc, {}).get("pre_test", {}).get(lang)
            or prep_instructions().get(nc, {}).get("pre_test", {}).get("en", "")
        )
        if pre:
            replies.append(Reply(pre))
        # Hand off ECG findings when the patient is heading to Ultrasound.
        if nc == "ULTRASOUND":
            findings = findings_on_journey(journey["id"], "ECG")
            if findings:
                replies.append(
                    Reply(
                        render_message(
                            "ecg_findings_for_ultrasound", lang, findings=findings
                        )
                    )
                )
    return replies


def _status_messages(chat_id: int, lang: str) -> list[Reply]:
    journey = get_active_journey(chat_id)
    if not journey:
        return [Reply(render_message("help", lang))]
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
    return [Reply("\n".join(lines))]


def _record_feedback(chat_id: int, journey: dict[str, Any], lang: str, text: str) -> list[Reply]:
    from app.feedback import record_patient_feedback

    record_patient_feedback(journey_id=journey["id"], raw_text=text)
    return [Reply(render_message("feedback_thanks", lang))]


def _post_language_key(chat_id: int) -> str:
    """After language pick: prompt the patient to claim their staff-issued
    ID if they haven't already, otherwise jump to the prescription prompt."""
    return "language_set" if get_patient_identifier(chat_id) else "ask_for_assigned_id"


def _looks_like_id(text: str) -> bool:
    """Hospital-issued patient IDs vary widely — short numbers, MRN strings,
    OPD numbers — so we just reject empty/slash-command/too-long inputs."""
    if not text or text.startswith("/"):
        return False
    return 1 <= len(text) <= 50
