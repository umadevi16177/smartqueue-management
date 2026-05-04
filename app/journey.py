"""Patient Journey Tracker.

Records completed tests, knows current position, triggers next step.
Backed by SQLite via app.db.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from app.db import get_conn
from app.knowledge import all_test_codes, get_test
from app.sequence_engine import sequence_tests


def get_or_create_patient(chat_id: int, display_name: str | None, language: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE patients SET language = ?, display_name = COALESCE(?, display_name) WHERE id = ?",
                (language, display_name, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO patients (telegram_chat_id, display_name, language) VALUES (?, ?, ?)",
            (chat_id, display_name, language),
        )
        return cur.lastrowid


def set_patient_language(chat_id: int, language: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET language = ? WHERE telegram_chat_id = ?",
            (language, chat_id),
        )


def get_patient_language(chat_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT language FROM patients WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        return row["language"] if row else None


def get_patient_voice_mode(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT voice_mode FROM patients WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        return bool(row and row["voice_mode"])


def set_patient_voice_mode(chat_id: int, voice_mode: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET voice_mode = ? WHERE telegram_chat_id = ?",
            (1 if voice_mode else 0, chat_id),
        )


def get_patient_identifier(chat_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT patient_identifier FROM patients WHERE telegram_chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row["patient_identifier"] if row and row["patient_identifier"] else None


def set_patient_identifier(chat_id: int, identifier: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET patient_identifier = ? WHERE telegram_chat_id = ?",
            (identifier.strip(), chat_id),
        )


def get_active_journey(chat_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT j.id FROM journeys j
            JOIN patients p ON p.id = j.patient_id
            WHERE p.telegram_chat_id = ? AND j.status NOT IN ('done', 'cancelled')
            ORDER BY j.id DESC LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
    return get_journey(row["id"]) if row else None


def get_latest_journey(chat_id: int) -> dict[str, Any] | None:
    """Latest journey regardless of status — used for feedback collection."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT j.id FROM journeys j
            JOIN patients p ON p.id = j.patient_id
            WHERE p.telegram_chat_id = ?
            ORDER BY j.id DESC LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
    return get_journey(row["id"]) if row else None


def start_journey(chat_id: int, requested_tests: list[str]) -> dict[str, Any]:
    """Create a journey: requested -> sequenced + journey_steps rows."""
    sequenced = sequence_tests(requested_tests)
    pid = _patient_id(chat_id)
    with get_conn() as conn:
        # Cancel any prior in-progress journey for this patient.
        conn.execute(
            "UPDATE journeys SET status = 'cancelled' WHERE patient_id = ? AND status NOT IN ('done', 'cancelled')",
            (pid,),
        )
        cur = conn.execute(
            """
            INSERT INTO journeys (patient_id, status, requested_tests_json, sequenced_tests_json, current_index)
            VALUES (?, 'sequenced', ?, ?, 0)
            """,
            (pid, json.dumps(requested_tests), json.dumps(sequenced)),
        )
        jid = cur.lastrowid
        for idx, code in enumerate(sequenced):
            conn.execute(
                "INSERT INTO journey_steps (journey_id, step_index, test_code) VALUES (?, ?, ?)",
                (jid, idx, code),
            )
    if "BLOOD" in sequenced:
        from app.scheduler import schedule_fasting_reminder

        schedule_fasting_reminder(chat_id=chat_id, journey_id=jid)
    return get_journey(jid)


def get_journey(journey_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM journeys WHERE id = ?", (journey_id,)).fetchone()
        if not row:
            raise ValueError(f"journey {journey_id} not found")
        steps = conn.execute(
            "SELECT * FROM journey_steps WHERE journey_id = ? ORDER BY step_index",
            (journey_id,),
        ).fetchall()
        return {**dict(row), "steps": [dict(s) for s in steps]}


def issue_queue_token(journey_id: int, test_code: str) -> str:
    token = f"{test_code[:3]}-{secrets.token_hex(2).upper()}"
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE journey_steps SET queue_token = ?, department_status = 'in_queue'
            WHERE journey_id = ? AND test_code = ? AND department_status = 'pending'
            """,
            (token, journey_id, test_code),
        )
    return token


def mark_step_completed(journey_id: int, test_code: str, findings: str | None = None) -> dict[str, Any]:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_conn() as conn:
        # COALESCE preserves any findings already recorded by staff before completion.
        conn.execute(
            """
            UPDATE journey_steps
            SET department_status = 'completed',
                completed_at = ?,
                findings_summary = COALESCE(?, findings_summary)
            WHERE journey_id = ? AND test_code = ?
            """,
            (now, findings, journey_id, test_code),
        )
        if test_code == "BLOOD":
            conn.execute(
                "UPDATE journeys SET blood_test_completed_at = ? WHERE id = ?",
                (now, journey_id),
            )
        # Advance current_index past completed steps in order.
        steps = conn.execute(
            "SELECT step_index, department_status FROM journey_steps WHERE journey_id = ? ORDER BY step_index",
            (journey_id,),
        ).fetchall()
        new_index = 0
        for s in steps:
            if s["department_status"] == "completed":
                new_index = s["step_index"] + 1
            else:
                break
        status = "done" if new_index >= len(steps) else "in_progress"
        conn.execute(
            "UPDATE journeys SET current_index = ?, status = ?, updated_at = ? WHERE id = ?",
            (new_index, status, now, journey_id),
        )
    return get_journey(journey_id)


def apply_reroute(journey_id: int, new_sequence: list[str]) -> dict[str, Any]:
    """Replace the remaining steps with `new_sequence` (must include already-completed)."""
    j = get_journey(journey_id)
    completed_codes = [s["test_code"] for s in j["steps"] if s["department_status"] == "completed"]
    if new_sequence[: len(completed_codes)] != completed_codes:
        raise ValueError("Reroute cannot rewrite completed history.")
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM journey_steps WHERE journey_id = ? AND department_status != 'completed'",
            (journey_id,),
        )
        for idx, code in enumerate(new_sequence):
            if idx < len(completed_codes):
                conn.execute(
                    "UPDATE journey_steps SET step_index = ? WHERE journey_id = ? AND test_code = ?",
                    (idx, journey_id, code),
                )
            else:
                conn.execute(
                    "INSERT INTO journey_steps (journey_id, step_index, test_code) VALUES (?, ?, ?)",
                    (journey_id, idx, code),
                )
        conn.execute(
            "UPDATE journeys SET sequenced_tests_json = ? WHERE id = ?",
            (json.dumps(new_sequence), journey_id),
        )
    return get_journey(journey_id)


def reserve_slot(journey_id: int, test_code: str, reserved_time: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE journey_steps
            SET department_status = 'reserved', reserved_for_time = ?
            WHERE journey_id = ? AND test_code = ? AND department_status != 'completed'
            """,
            (reserved_time, journey_id, test_code),
        )
    chat_id = _chat_id_for_journey(journey_id)
    if chat_id is not None:
        slot_dt = _parse_slot_time(reserved_time)
        if slot_dt is not None:
            from app.scheduler import schedule_slot_alert

            schedule_slot_alert(
                chat_id=chat_id, journey_id=journey_id, test_code=test_code, slot_time=slot_dt
            )


def journey_metrics() -> dict[str, Any]:
    """SQL aggregates for the admin panel: avg journey duration, longest delays."""
    with get_conn() as conn:
        completed = conn.execute(
            """
            SELECT j.id, j.created_at, MAX(s.completed_at) AS last_completed_at
            FROM journeys j
            JOIN journey_steps s ON s.journey_id = j.id
            WHERE j.status = 'done' AND s.completed_at IS NOT NULL
            GROUP BY j.id
            """
        ).fetchall()
        durations: list[float] = []
        for row in completed:
            try:
                start = datetime.fromisoformat(row["created_at"])
                end = datetime.fromisoformat(row["last_completed_at"])
                durations.append((end - start).total_seconds() / 60.0)
            except (ValueError, TypeError):
                continue
        # "Delay points" = average time between successive step completions.
        delay_rows = conn.execute(
            """
            SELECT s.test_code,
                   AVG(julianday(s.completed_at) - julianday(prev.completed_at)) * 24 * 60 AS avg_gap_min
            FROM journey_steps s
            JOIN journey_steps prev
              ON prev.journey_id = s.journey_id AND prev.step_index = s.step_index - 1
            WHERE s.completed_at IS NOT NULL AND prev.completed_at IS NOT NULL
            GROUP BY s.test_code
            ORDER BY avg_gap_min DESC
            """
        ).fetchall()
        return {
            "completed_journeys": len(durations),
            "avg_journey_minutes": round(sum(durations) / len(durations), 1) if durations else None,
            "longest_journey_minutes": round(max(durations), 1) if durations else None,
            "delay_points": [
                {"test_code": r["test_code"], "avg_gap_minutes": round(r["avg_gap_min"], 1)}
                for r in delay_rows
            ],
        }


def record_findings(journey_id: int, test_code: str, findings: str) -> None:
    """Attach a free-text findings note to the most recent (or in-progress) step
    of `test_code` on `journey_id`."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE journey_steps SET findings_summary = ?
            WHERE journey_id = ? AND test_code = ?
            """,
            (findings, journey_id, test_code),
        )


def latest_findings_for(test_code: str) -> dict[str, Any] | None:
    """Return the most recent non-empty findings note for `test_code`,
    paired with patient name, for the staff dashboard."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.findings_summary, p.display_name, p.telegram_chat_id, s.completed_at
            FROM journey_steps s
            JOIN journeys j ON j.id = s.journey_id
            JOIN patients p ON p.id = j.patient_id
            WHERE s.test_code = ? AND s.findings_summary IS NOT NULL AND s.findings_summary != ''
            ORDER BY s.completed_at DESC, s.id DESC
            LIMIT 1
            """,
            (test_code,),
        ).fetchone()
        return dict(row) if row else None


def findings_on_journey(journey_id: int, test_code: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT findings_summary FROM journey_steps WHERE journey_id = ? AND test_code = ?",
            (journey_id, test_code),
        ).fetchone()
        return (row["findings_summary"] if row else None) or None


def _chat_id_for_journey(journey_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT p.telegram_chat_id FROM journeys j "
            "JOIN patients p ON p.id = j.patient_id WHERE j.id = ?",
            (journey_id,),
        ).fetchone()
        return row["telegram_chat_id"] if row else None


def _parse_slot_time(reserved_time: str) -> "datetime | None":
    """Parse strings like '11:34 AM' into a datetime today (or tomorrow if past)."""
    from datetime import datetime, timedelta

    if not reserved_time:
        return None
    try:
        t = datetime.strptime(reserved_time.strip(), "%I:%M %p").time()
    except ValueError:
        return None
    candidate = datetime.combine(datetime.now().date(), t)
    if candidate < datetime.now():
        candidate += timedelta(days=1)
    return candidate


def current_step(journey: dict[str, Any]) -> dict[str, Any] | None:
    idx = journey["current_index"]
    for s in journey["steps"]:
        if s["step_index"] == idx and s["department_status"] != "completed":
            return s
    return None


def _patient_id(chat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"No patient for chat_id={chat_id}")
        return row["id"]


# Sanity check on app start: ensure all referenced codes exist in catalogue.
def validate_knowledge() -> None:
    codes = set(all_test_codes())
    for c in codes:
        assert get_test(c), f"catalogue missing entry for {c}"
