"""Conversation Flow Controller.

Orchestrates Telegram messages with the AI core and data layer.
Stateless: all state lives in PostgreSQL (sessions / journeys / journey_steps).
"""
from __future__ import annotations

import os
from typing import Any

from app.config import settings
from app.journey import (
    apply_reroute,
    claim_patient_by_id,
    current_step,
    unlink_user,
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
    toggle_test_selection,
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
from app.journey import get_session, set_session

AVAILABLE_TESTS = ["BLOOD", "ECG", "XRAY", "ULTRASOUND", "MRI", "CT", "PFT", "TMT", "URINE", "EYE"]


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


def handle_message(chat_id: int, sender_name: str | None, text: str, bot_type: str = "diagnostic") -> list[Reply]:
    """Return a list of Reply objects (one per Telegram message bubble)."""
    ensure_seeded()
    text = (text or "").strip()
    lang = get_patient_language(chat_id) or settings.default_language

    if bot_type == "hub":
        return _handle_hub_bot(chat_id, sender_name, text, lang)
    elif bot_type == "registration":
        return _handle_registration_bot(chat_id, sender_name, text, lang)
    else:
        return _handle_diagnostic_bot(chat_id, sender_name, text, lang)


def _handle_hub_bot(chat_id: int, sender_name: str | None, text: str, lang: str) -> list[Reply]:
    """Front-desk bot: routes patients to either Registration or Diagnostic via
    inline-keyboard buttons. State check is purely DB-driven — no command parsing."""
    patient_id = get_patient_identifier(chat_id)
    reg_bot_username = os.getenv("REGISTRATION_BOT_USERNAME", "SmartQueueRegistrationBot")
    diag_bot_username = os.getenv("DIAGNOSTIC_BOT_USERNAME", "SmartQueueDiagnosticBot")

    if patient_id:
        # Returning patient — deep-link the diagnostic bot with their actual ID
        # so the claim is auto-confirmed on click.
        diag_link = f"https://t.me/{diag_bot_username}?start={patient_id}"
        return [Reply(
            text=render_message("hub_already_registered", lang, patient_id=patient_id),
            buttons=[(render_message("btn_manage_tests", lang), diag_link)],
        )]
    reg_link = f"https://t.me/{reg_bot_username}"
    return [Reply(
        text=render_message("welcome_hub", lang),
        buttons=[(render_message("btn_register_now", lang), reg_link)],
    )]


def _handle_registration_bot(chat_id: int, sender_name: str | None, text: str, lang: str) -> list[Reply]:
    if text in ("/start", "start"):
        get_or_create_patient(chat_id, sender_name, lang)
        return [Reply(render_message("welcome", lang, hospital=settings.hospital_name))]

    # Language selection
    if text in LANG_COMMANDS:
        new_lang = LANG_COMMANDS[text]
        get_or_create_patient(chat_id, sender_name, new_lang)
        set_patient_language(chat_id, new_lang)
        # Use a new message or repurpose language_set
        return [Reply(render_message("language_set", new_lang)), 
                Reply(render_message("registration_prompt", new_lang))]

    alias_lang = LANG_ALIASES.get(text.lower())
    if alias_lang:
        get_or_create_patient(chat_id, sender_name, alias_lang)
        set_patient_language(chat_id, alias_lang)
        return [Reply(render_message("language_set", alias_lang)),
                Reply(render_message("registration_prompt", alias_lang))]

    # Actual registration (asking for ID or just acknowledging)
    if _looks_like_id(text):
        claimed = claim_patient_by_id(chat_id, text)
        if claimed:
            bot_username = os.getenv("DIAGNOSTIC_BOT_USERNAME", "SmartQueueDiagnosticBot")
            deep_link = f"https://t.me/{bot_username}?start={claimed['patient_id']}"
            return [Reply(
                text=render_message(
                    "registration_complete", lang, name=claimed["display_name"] or "patient"
                ),
                buttons=[(render_message("btn_go_to_tests", lang), deep_link)],
            )]
        else:
            return [Reply(render_message("id_not_found", lang, patient_id=text))]

    if text == "/reset":
        unlink_user(chat_id)
        return [Reply(render_message("reset_complete", lang))]

    return [Reply(render_message("welcome", lang, hospital=settings.hospital_name))]


def _handle_diagnostic_bot(chat_id: int, sender_name: str | None, text: str, lang: str) -> list[Reply]:
    # 1. Handle Start and Registration Check
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            patient_id = parts[1]
            claimed = claim_patient_by_id(chat_id, patient_id)
            if claimed:
                # Fresh /start = fresh test selection. Without this, leftover
                # ✅ marks from a previous /start show up looking like the bot
                # pre-selected tests on the patient's behalf.
                set_session(chat_id, "choosing_tests", {})
                welcome = Reply(text=render_message("claimed", lang, name=claimed["display_name"] or "patient", patient_id=claimed["patient_id"], sequence_number=claimed["sequence_number"]))
                return [welcome] + _render_test_menu(chat_id, lang, [])

        if not get_patient_identifier(chat_id):
            reg_bot_username = os.getenv("REGISTRATION_BOT_USERNAME", "SmartQueueRegistrationBot")
            reg_link = f"https://t.me/{reg_bot_username}"
            return [Reply(render_message("please_register_first", lang, link=reg_link))]

        set_session(chat_id, "choosing_tests", {})
        welcome = Reply(text=render_message("welcome_diagnostic", lang))
        return [welcome] + _render_test_menu(chat_id, lang, [])

    # 2. Global Commands
    if text in LANG_COMMANDS:
        new_lang = LANG_COMMANDS[text]
        set_patient_language(chat_id, new_lang)
        lang_label = {"en": "English", "hi": "हिंदी", "te": "తెలుగు"}.get(new_lang, new_lang)
        toast = f"✓ Language: {lang_label}"
        if get_patient_identifier(chat_id):
            pending = get_session(chat_id).get("pending_data", {}).get("selected_tests", [])
            replies = _render_test_menu(chat_id, new_lang, pending)
            if replies:
                replies[0].toast = toast
            return replies
        r = Reply(render_message("language_set", new_lang))
        r.toast = toast
        return [r]

    if text == "/voice":
        set_patient_voice_mode(chat_id, True)
        return [Reply(render_message("voice_on", lang))]

    if text == "/text":
        set_patient_voice_mode(chat_id, False)
        return [Reply(render_message("voice_off", lang))]

    if text == "/help":
        return [Reply(render_message("help", lang))]

    if text == "/reset":
        unlink_user(chat_id)
        return [Reply(render_message("reset_complete", lang))]

    # 2b. Registration gate — every state-mutating path below assumes a
    # patient row exists for this chat_id (start_journey, set_session writes
    # tied to a journey, etc.). Without this, an unregistered user typing free
    # text crashes start_journey with "No patient for chat_id=…", which surfaces
    # to Telegram as 500 Internal Server Error.
    if not get_patient_identifier(chat_id):
        reg_bot_username = os.getenv("REGISTRATION_BOT_USERNAME", "SmartQueueRegistrationBot")
        reg_link = f"https://t.me/{reg_bot_username}"
        return [Reply(render_message("please_register_first", lang, link=reg_link))]

    # 3. Load Session
    pending_tests = get_session(chat_id).get("pending_data", {}).get("selected_tests", [])

    # 4. Handle Interactive Test Buttons. Toggling via the atomic
    # `toggle_test_selection` helper avoids the race where two near-
    # simultaneous taps each read the old list and clobber each other.
    # We attach a toast so the user gets instant feedback ("✅ Blood Test
    # added") via `answer_callback_query` — independent of the slow
    # `edit_message_text` round-trip that updates the menu markers.
    if text.startswith("select:"):
        test_code = text.split(":")[1]
        new_pending = toggle_test_selection(chat_id, test_code)
        test_name = display_name(test_code, lang)
        if test_code in new_pending:
            toast = f"✅ {test_name} added ({len(new_pending)} selected)"
        else:
            toast = f"⬜ {test_name} removed ({len(new_pending)} selected)"
        replies = _render_test_menu(chat_id, lang, new_pending)
        if replies:
            replies[0].toast = toast
            # Body is unchanged on toggle — only the ✅/⬜ markers in the
            # buttons differ. `edit_message_reply_markup` is ~3x faster than
            # `edit_message_text` and gives the user near-instant visual
            # confirmation that their tap registered.
            replies[0].markup_only = True
        return replies

    if text == "confirm_tests":
        if not pending_tests:
            r = Reply(render_message("send_tests_first", lang))
            r.toast = "Please select at least one test first"
            return [r]
        set_session(chat_id, "idle", {})
        j = start_journey(chat_id, pending_tests)
        sequence_codes = [s["test_code"] for s in j["steps"]]
        sequence_str = " → ".join(display_name(c, lang) for c in sequence_codes)
        typed_str = ", ".join(display_name(c, lang) for c in pending_tests)
        first = Reply(render_message("tests_recognised", lang, tests=typed_str, sequence=sequence_str))
        first.toast = f"✅ {len(pending_tests)} tests confirmed"
        return [
            first,
            Reply(render_message("confirm_prompt", lang), buttons=[(render_message("btn_confirm", lang), "/confirm")])
        ]

    # 6. Journey Status Commands
    if text == "/status":
        return _status_messages(chat_id, lang)
    if text == "/confirm":
        return _confirm_pending_journey(chat_id, lang)
    if text.startswith("/done"):
        return _handle_done_command(chat_id, lang, text)

    # 6b. Post-journey feedback — once the most recent journey is done, free
    # text from the patient is treated as the feedback comment, not as a new
    # test prompt. Without this branch, "5 — staff was very helpful" falls
    # through to test parsing and the bot serves the test menu instead of a
    # thank-you, and feedback never lands in the admin metrics.
    latest = get_latest_journey(chat_id)
    if latest and (latest["status"] == "done" or latest["current_index"] >= len(latest["steps"])):
        return _record_feedback(chat_id, latest, lang, text)

    # 7. Active Journey or Menu Fallback
    active = get_active_journey(chat_id)
    if active:
        return _status_messages(chat_id, lang)

    parsed = parse_test_request(text)
    tests = parsed.get("tests") or []
    if tests:
        j = start_journey(chat_id, tests)
        sequence_codes = [s["test_code"] for s in j["steps"]]
        sequence_str = " → ".join(display_name(c, lang) for c in sequence_codes)
        typed_str = ", ".join(display_name(c, lang) for c in tests)
        return [
            Reply(render_message("tests_recognised", lang, tests=typed_str, sequence=sequence_str)),
            Reply(render_message("confirm_prompt", lang), buttons=[(render_message("btn_confirm", lang), "/confirm")])
        ]

    return _render_test_menu(chat_id, lang, pending_tests)


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
    dept = get_department(code)
    ahead = dept["queue_length"] if dept else 0
    body = render_message(
        "sequence_locked",
        lang,
        first_test=display_name(code, lang),
        floor=floor,
        directions=dirs,
        token=token,
        ahead=ahead,
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
        ahead = dept["queue_length"] if dept else 0
        body = render_message(
            "next_step",
            lang,
            test=display_name(nc, lang),
            floor=floor,
            room=room,
            directions=dirs,
            token=token,
            wait=wait,
            ahead=ahead,
        )
        replies.append(Reply(text=body, photo=floor_map_path(nc)))
        pre = (
            prep_instructions().get(nc, {}).get("pre_test", {}).get(lang)
            or prep_instructions().get(nc, {}).get("pre_test", {}).get("en", "")
        )
        if pre:
            replies.append(Reply(pre))
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


_ID_PATTERN = __import__("re").compile(r"^[A-Za-z0-9._/\-]{1,50}$")


def _looks_like_id(text: str) -> bool:
    """Hospital-issued patient IDs are short single-token alphanumeric
    strings (e.g. P-001, MRN12345, OPD/2026/A1). If the text contains
    spaces, punctuation, or question marks, it's a question or free-text —
    not an ID — and should fall through to the `invalid_id` reply, which
    points the patient at /help."""
    if not text or text.startswith("/"):
        return False
    return bool(_ID_PATTERN.match(text))

def _language_buttons() -> list[list[tuple[str, str]]]:
    """One row, three columns. Labels stay in their own script so the patient
    recognises their language even before the bot switches — callback_data
    routes through LANG_COMMANDS."""
    return [[
        ("🇬🇧 English", "/english"),
        ("🇮🇳 हिंदी", "/hindi"),
        ("🇮🇳 తెలుగు", "/telugu"),
    ]]


def _render_test_menu(chat_id: int, lang: str, selected_tests: list[str]) -> list[Reply]:
    """Multi-select test menu rendered as a 2-column grid.

    Layout (top → bottom):
        Row 1:  language picker (3 columns)
        Rows 2-N: test buttons in pairs of 2
        Last row: Confirm Selection (full width)

    The language picker lives on this message — not the welcome — so when
    the patient switches language, the callback edits this same message in
    place rather than creating a new menu beside the old one. That stops
    the "two menus, both editable" confusion that produces phantom
    selections.

    Each tap on a test toggles its membership in `selected_tests` via the
    atomic `toggle_test_selection` helper. ✅/⬜ markers re-render after
    every tap.
    """
    rows: list[list[tuple[str, str]]] = []
    rows.extend(_language_buttons())
    pair: list[tuple[str, str]] = []
    for code in AVAILABLE_TESTS:
        marker = "✅" if code in selected_tests else "⬜"
        pair.append((f"{marker} {display_name(code, lang)}", f"select:{code}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([(render_message("btn_confirm_selection", lang), "confirm_tests")])

    # Body stays static — the buttons themselves show ✅/⬜ and the toast
    # gives the running count. Static body lets callers use
    # `edit_message_reply_markup` (buttons-only edit), which is ~3x faster
    # than `edit_message_text` because it sends a smaller payload.
    body = render_message("test_selection_menu", lang)
    return [Reply(text=body, buttons=rows)]
