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
        conn.execute(
            """
            UPDATE journey_steps
            SET department_status = 'completed', completed_at = ?, findings_summary = ?
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
