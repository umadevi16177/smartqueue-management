"""Patient Journey Tracker.

Records completed tests, knows current position, triggers next step.
Backed by Postgres (smartqueue schema) via app.db.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from typing import Any

import psycopg2

from app.db import get_conn
from app.knowledge import all_test_codes, get_test
from app.sequence_engine import sequence_tests


def _avg_minutes(test_code: str) -> int:
    """Per-test average duration from the catalogue (minutes). Used to keep
    `departments.estimated_wait_minutes` in step with `queue_length`."""
    t = get_test(test_code) or {}
    return int(t.get("average_minutes") or 5)


def _dept_queue_delta(conn: Any, test_code: str, count_delta: int) -> None:
    """Adjust `departments.queue_length` and `estimated_wait_minutes` for a
    single in-queue ↔ not-in-queue transition. Clamped at zero so we never
    show negative queues if staff has manually edited counters in parallel."""
    minutes_delta = _avg_minutes(test_code) * count_delta
    conn.execute(
        "UPDATE departments SET "
        "  queue_length = GREATEST(0, queue_length + %s), "
        "  estimated_wait_minutes = GREATEST(0, estimated_wait_minutes + %s), "
        "  updated_at = (NOW() AT TIME ZONE 'UTC')::text "
        "WHERE code = %s",
        (count_delta, minutes_delta, test_code),
    )


def reconcile_department_counters() -> None:
    """Rebuild `departments.queue_length` and `estimated_wait_minutes` from the
    actual `journey_steps` rows currently in `in_queue` state. Idempotent — safe
    to call on every startup. Clears drift from sessions that ran before the
    auto-tracking deltas were wired in."""
    with get_conn() as conn:
        for code in all_test_codes():
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM journey_steps "
                "WHERE test_code = %s AND department_status = 'in_queue'",
                (code,),
            ).fetchone()
            count = int(row["c"]) if row else 0
            conn.execute(
                "UPDATE departments SET "
                "  queue_length = %s, "
                "  estimated_wait_minutes = %s, "
                "  updated_at = (NOW() AT TIME ZONE 'UTC')::text "
                "WHERE code = %s",
                (count, count * _avg_minutes(code), code),
            )


def get_or_create_patient(chat_id: int, display_name: str | None, language: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE telegram_chat_id = %s", (chat_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE patients SET language = %s, "
                "display_name = COALESCE(%s, display_name) WHERE id = %s",
                (language, display_name, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO patients (telegram_chat_id, display_name, language) "
            "VALUES (%s, %s, %s) RETURNING id",
            (chat_id, display_name, language),
        )
        return cur.fetchone()[0]


def set_patient_language(chat_id: int, language: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET language = %s WHERE telegram_chat_id = %s",
            (language, chat_id),
        )


def get_patient_language(chat_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT language FROM patients WHERE telegram_chat_id = %s", (chat_id,)
        ).fetchone()
        return row["language"] if row else None


def get_patient_voice_mode(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT voice_mode FROM patients WHERE telegram_chat_id = %s", (chat_id,)
        ).fetchone()
        return bool(row and row["voice_mode"])


def set_patient_voice_mode(chat_id: int, voice_mode: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET voice_mode = %s WHERE telegram_chat_id = %s",
            (1 if voice_mode else 0, chat_id),
        )


def get_patient_identifier(chat_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT patient_identifier FROM patients WHERE telegram_chat_id = %s",
            (chat_id,),
        ).fetchone()
        return row["patient_identifier"] if row and row["patient_identifier"] else None


def set_patient_identifier(chat_id: int, identifier: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET patient_identifier = %s WHERE telegram_chat_id = %s",
            (identifier.strip(), chat_id),
        )


def staff_register_patient(name: str, patient_id: str | None = None) -> dict[str, Any]:
    """Hospital-staff-driven registration.

    - patient_id is None: create a new patient row, issue the next P-NNN
      permanent ID, assign the next FCFS queue number.
    - patient_id given and known: reuse the existing permanent ID, assign a
      fresh queue number for this visit. (Patient ID is permanent; queue
      number is per-visit.)
    - patient_id given but unknown: raise ValueError so the caller can prompt
      staff to leave it blank for a new registration.

    Race-safe via the atomic counter increment in `_try_staff_register` plus
    a retry loop that catches IntegrityError (UNIQUE collision on a parallel
    insert) and OperationalError (transient connection error). ValueError
    bypasses the retry — it surfaces immediately.

    Returns: {patient_id, sequence_number, display_name}.
    """
    name = (name or "").strip() or "Unnamed"
    pid = (patient_id or "").strip().upper() or None

    last_err: Exception | None = None
    for attempt in range(8):
        try:
            return _try_staff_register(name, pid)
        except psycopg2.IntegrityError as e:
            last_err = e
        except psycopg2.OperationalError as e:
            last_err = e
        time.sleep(0.02 * (2 ** attempt))
    assert last_err is not None
    raise last_err


def _try_staff_register(name: str, pid: str | None) -> dict[str, Any]:
    with get_conn() as conn:
        # Atomic increment-and-read on a single row — UPDATE acquires a row
        # write lock so concurrent transactions serialize. Each caller gets a
        # distinct number even when re-queueing the same patient.
        next_seq = conn.execute(
            "UPDATE counters SET value = value + 1 "
            "WHERE name = 'queue_seq' RETURNING value"
        ).fetchone()[0]
        if pid:
            existing = conn.execute(
                "SELECT id, display_name FROM patients WHERE patient_identifier = %s",
                (pid,),
            ).fetchone()
            if not existing:
                raise ValueError(f"Unknown Patient ID {pid}")
            conn.execute(
                "UPDATE patients SET sequence_number = %s, "
                "display_name = COALESCE(NULLIF(display_name, ''), %s) "
                "WHERE id = %s",
                (next_seq, name, existing["id"]),
            )
            return {
                "patient_id": pid,
                "sequence_number": next_seq,
                "display_name": existing["display_name"] or name,
            }
        new_pid = f"P-{next_seq:03d}"
        conn.execute(
            """INSERT INTO patients
                 (telegram_chat_id, display_name, patient_identifier, sequence_number)
               VALUES (NULL, %s, %s, %s)""",
            (name, new_pid, next_seq),
        )
        return {
            "patient_id": new_pid,
            "sequence_number": next_seq,
            "display_name": name,
        }


def unlink_user(chat_id: int) -> bool:
    """Unlink the Telegram user from their Patient ID and clear their session.
    The patient record (staff-issued ID) remains in the database.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM patients WHERE telegram_chat_id = %s", (chat_id,)
        ).fetchone()
        if not row:
            return False
        
        # 1. Clear the Telegram link from the patient record
        conn.execute(
            "UPDATE patients SET telegram_chat_id = NULL WHERE telegram_chat_id = %s",
            (chat_id,)
        )
        # 2. Delete the conversation session
        conn.execute("DELETE FROM sessions WHERE chat_id = %s", (chat_id,))
        return True


def get_session(chat_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state, pending_data_json FROM sessions WHERE chat_id = %s",
            (chat_id,)
        ).fetchone()
        if not row:
            return {"state": "idle", "pending_data": {}}
        return {
            "state": row["state"],
            "pending_data": json.loads(row["pending_data_json"] or "{}")
        }


def set_session(chat_id: int, state: str, pending_data: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (chat_id, state, pending_data_json, updated_at)
            VALUES (%s, %s, %s, (NOW() AT TIME ZONE 'UTC')::text)
            ON CONFLICT (chat_id) DO UPDATE SET
                state = EXCLUDED.state,
                pending_data_json = EXCLUDED.pending_data_json,
                updated_at = EXCLUDED.updated_at
            """,
            (chat_id, state, json.dumps(pending_data))
        )


def toggle_test_selection(chat_id: int, test_code: str) -> list[str]:
    """Atomically flip a test in/out of the patient's pending selection.

    Returns the new full list of selected test codes.

    Two concurrent webhook calls (e.g. the user mashing two buttons in
    rapid succession) can race the read-then-write pattern of
    `get_session` + `set_session`: both reads see the same starting list,
    both writes save their own diff, and one tap's change is lost. The
    `SELECT ... FOR UPDATE` row lock here serialises concurrent toggles
    on the same chat_id so each tap reads the result of the previous one.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state, pending_data_json FROM sessions WHERE chat_id = %s FOR UPDATE",
            (chat_id,),
        ).fetchone()
        if row:
            state = row["state"]
            pending_data = json.loads(row["pending_data_json"] or "{}")
        else:
            state = "choosing_tests"
            pending_data = {}

        selected = list(pending_data.get("selected_tests", []))
        if test_code in selected:
            selected.remove(test_code)
        else:
            selected.append(test_code)
        pending_data["selected_tests"] = selected
        if state == "idle":
            state = "choosing_tests"

        conn.execute(
            """
            INSERT INTO sessions (chat_id, state, pending_data_json, updated_at)
            VALUES (%s, %s, %s, (NOW() AT TIME ZONE 'UTC')::text)
            ON CONFLICT (chat_id) DO UPDATE SET
                state = EXCLUDED.state,
                pending_data_json = EXCLUDED.pending_data_json,
                updated_at = EXCLUDED.updated_at
            """,
            (chat_id, state, json.dumps(pending_data)),
        )
        return selected


def claim_patient_by_id(chat_id: int, patient_id: str) -> dict[str, Any] | None:
    """Patient enters their staff-issued ID via the bot. Look it up and
    bind their chat_id to the existing record.

    Returns the patient row if claimed (or already owned by this chat),
    or None on:
      - patient_id not found (staff hasn't registered it)
      - patient_id already claimed by a different chat_id
    """
    patient_id = patient_id.strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, telegram_chat_id, display_name, sequence_number "
            "FROM patients WHERE patient_identifier = %s",
            (patient_id,),
        ).fetchone()
        if not row:
            return None
        if row["telegram_chat_id"] is not None and row["telegram_chat_id"] != chat_id:
            return None  # already taken by another Telegram user
        # Free up any prior shell row this chat may have created via /start.
        conn.execute(
            "DELETE FROM patients WHERE telegram_chat_id = %s AND id != %s "
            "AND patient_identifier IS NULL",
            (chat_id, row["id"]),
        )
        conn.execute(
            "UPDATE patients SET telegram_chat_id = %s WHERE id = %s",
            (chat_id, row["id"]),
        )
    return {
        "patient_id": patient_id,
        "sequence_number": row["sequence_number"],
        "display_name": row["display_name"],
    }


def list_unclaimed_patients() -> list[dict[str, Any]]:
    """Staff-registered patients who haven't messaged the bot yet."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, patient_identifier, sequence_number, display_name, created_at "
            "FROM patients WHERE telegram_chat_id IS NULL "
            "ORDER BY sequence_number ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_active_journeys() -> list[dict[str, Any]]:
    """All in-flight patients for the staff dashboard.

    Uses TWO queries (journeys + all-their-steps), not 1 + N. The naive loop
    pattern was issuing one query per journey to fetch its steps, which on
    Neon (cloud Postgres ~380ms RTT) turned into ~4.5s for 11 journeys —
    enough to make the dashboard feel frozen. The single grouped query
    below collapses to ~700ms regardless of journey count.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT j.id              AS journey_id,
                   j.status,
                   j.current_index,
                   j.sequenced_tests_json,
                   j.created_at,
                   j.updated_at,
                   p.telegram_chat_id,
                   p.display_name,
                   p.patient_identifier,
                   p.sequence_number,
                   p.language
            FROM journeys j
            JOIN patients p ON p.id = j.patient_id
            WHERE j.status NOT IN ('cancelled')
            ORDER BY p.sequence_number ASC, j.id ASC
            LIMIT 50
            """
        ).fetchall()
        if not rows:
            return []

        journey_ids = [r["journey_id"] for r in rows]
        # Single query for ALL steps across ALL journeys, then group in Python.
        step_rows = conn.execute(
            "SELECT journey_id, step_index, test_code, queue_token, "
            "       department_status, reserved_for_time, completed_at "
            "FROM journey_steps WHERE journey_id = ANY(%s) "
            "ORDER BY journey_id, step_index",
            (journey_ids,),
        ).fetchall()

        steps_by_journey: dict[int, list[dict[str, Any]]] = {}
        for s in step_rows:
            steps_by_journey.setdefault(s["journey_id"], []).append({
                "step_index": s["step_index"],
                "test_code": s["test_code"],
                "queue_token": s["queue_token"],
                "department_status": s["department_status"],
                "reserved_for_time": s["reserved_for_time"],
                "completed_at": s["completed_at"],
            })

        result: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["steps"] = steps_by_journey.get(r["journey_id"], [])
            d["current_test"] = None
            for s in d["steps"]:
                if s["department_status"] != "completed":
                    d["current_test"] = s["test_code"]
                    d["current_token"] = s["queue_token"]
                    break
            result.append(d)
        return result


def get_active_journey(chat_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT j.id FROM journeys j
            JOIN patients p ON p.id = j.patient_id
            WHERE p.telegram_chat_id = %s AND j.status NOT IN ('done', 'cancelled')
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
            WHERE p.telegram_chat_id = %s
            ORDER BY j.id DESC LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
    return get_journey(row["id"]) if row else None


def start_journey(chat_id: int, requested_tests: list[str], symptoms: str | None = None) -> dict[str, Any]:
    """Create a journey: requested -> sequenced + journey_steps rows."""
    sequenced = sequence_tests(requested_tests)
    pid = _patient_id(chat_id)
    with get_conn() as conn:
        # Get patient info for denormalization
        p_row = conn.execute(
            "SELECT display_name, patient_identifier FROM patients WHERE id = %s",
            (pid,)
        ).fetchone()
        p_name = p_row["display_name"] if p_row else None
        p_id_str = p_row["patient_identifier"] if p_row else None

        # Cancel any prior in-progress journey for this patient.
        conn.execute(
            "UPDATE journeys SET status = 'cancelled' "
            "WHERE patient_id = %s AND status NOT IN ('done', 'cancelled')",
            (pid,),
        )
        cur = conn.execute(
            """
            INSERT INTO journeys (patient_id, patient_name, patient_id_string, symptoms, status, requested_tests_json, sequenced_tests_json, current_index)
            VALUES (%s, %s, %s, %s, 'sequenced', %s, %s, 0)
            RETURNING id
            """,
            (pid, p_name, p_id_str, symptoms, json.dumps(requested_tests), json.dumps(sequenced)),
        )
        jid = cur.fetchone()[0]
        for idx, code in enumerate(sequenced):
            conn.execute(
                "INSERT INTO journey_steps (journey_id, patient_name, patient_id_string, step_index, test_code) "
                "VALUES (%s, %s, %s, %s, %s)",
                (jid, p_name, p_id_str, idx, code),
            )
    if "BLOOD" in sequenced:
        from app.scheduler import schedule_fasting_reminder

        schedule_fasting_reminder(chat_id=chat_id, journey_id=jid)
    return get_journey(jid)


def get_journey(journey_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM journeys WHERE id = %s", (journey_id,)).fetchone()
        if not row:
            raise ValueError(f"journey {journey_id} not found")
        steps = conn.execute(
            "SELECT * FROM journey_steps WHERE journey_id = %s ORDER BY step_index",
            (journey_id,),
        ).fetchall()
        return {**dict(row), "steps": [dict(s) for s in steps]}


def issue_queue_token(journey_id: int, test_code: str) -> str:
    token = f"{test_code[:3]}-{secrets.token_hex(2).upper()}"
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE journey_steps SET queue_token = %s, department_status = 'in_queue'
            WHERE journey_id = %s AND test_code = %s AND department_status = 'pending'
            """,
            (token, journey_id, test_code),
        )
        # Only bump the live department counter if the step actually transitioned
        # pending → in_queue (rowcount==1). Re-issuing on an already-queued step
        # is a no-op so we don't double-count.
        if cur.rowcount > 0:
            _dept_queue_delta(conn, test_code, +1)
    return token


def mark_step_completed(journey_id: int, test_code: str, findings: str | None = None) -> dict[str, Any]:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_conn() as conn:
        # Snapshot status BEFORE the UPDATE so we know whether to drop the live
        # department counter (only steps that were actually queued count).
        prev = conn.execute(
            "SELECT department_status FROM journey_steps "
            "WHERE journey_id = %s AND test_code = %s",
            (journey_id, test_code),
        ).fetchone()
        was_in_queue = bool(prev and prev["department_status"] == "in_queue")
        # COALESCE preserves any findings already recorded by staff before completion.
        conn.execute(
            """
            UPDATE journey_steps
            SET department_status = 'completed',
                completed_at = %s,
                findings_summary = COALESCE(%s, findings_summary)
            WHERE journey_id = %s AND test_code = %s
            """,
            (now, findings, journey_id, test_code),
        )
        if was_in_queue:
            _dept_queue_delta(conn, test_code, -1)
        if test_code == "BLOOD":
            conn.execute(
                "UPDATE journeys SET blood_test_completed_at = %s WHERE id = %s",
                (now, journey_id),
            )
        # Advance current_index past completed steps in order.
        steps = conn.execute(
            "SELECT step_index, department_status FROM journey_steps "
            "WHERE journey_id = %s ORDER BY step_index",
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
            "UPDATE journeys SET current_index = %s, status = %s, updated_at = %s WHERE id = %s",
            (new_index, status, now, journey_id),
        )
    return get_journey(journey_id)


def apply_reroute(journey_id: int, new_sequence: list[str]) -> dict[str, Any]:
    """Replace the remaining steps with `new_sequence` (must include already-completed)."""
    j = get_journey(journey_id)
    completed_codes = [s["test_code"] for s in j["steps"] if s["department_status"] == "completed"]
    if new_sequence[: len(completed_codes)] != completed_codes:
        raise ValueError("Reroute cannot rewrite completed history.")
    # Any step currently in_queue is being removed by the reroute — the patient
    # is no longer waiting at that department, so drop its live counter.
    in_queue_codes = [s["test_code"] for s in j["steps"] if s["department_status"] == "in_queue"]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM journey_steps WHERE journey_id = %s AND department_status != 'completed'",
            (journey_id,),
        )
        for code in in_queue_codes:
            _dept_queue_delta(conn, code, -1)
        for idx, code in enumerate(new_sequence):
            if idx < len(completed_codes):
                conn.execute(
                    "UPDATE journey_steps SET step_index = %s WHERE journey_id = %s AND test_code = %s",
                    (idx, journey_id, code),
                )
            else:
                conn.execute(
                    "INSERT INTO journey_steps (journey_id, step_index, test_code) "
                    "VALUES (%s, %s, %s)",
                    (journey_id, idx, code),
                )
        conn.execute(
            "UPDATE journeys SET sequenced_tests_json = %s WHERE id = %s",
            (json.dumps(new_sequence), journey_id),
        )
    return get_journey(journey_id)


def reserve_slot(journey_id: int, test_code: str, reserved_time: str) -> None:
    with get_conn() as conn:
        prev = conn.execute(
            "SELECT department_status FROM journey_steps "
            "WHERE journey_id = %s AND test_code = %s AND department_status != 'completed'",
            (journey_id, test_code),
        ).fetchone()
        was_in_queue = bool(prev and prev["department_status"] == "in_queue")
        conn.execute(
            """
            UPDATE journey_steps
            SET department_status = 'reserved', reserved_for_time = %s
            WHERE journey_id = %s AND test_code = %s AND department_status != 'completed'
            """,
            (reserved_time, journey_id, test_code),
        )
        if was_in_queue:
            _dept_queue_delta(conn, test_code, -1)
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
        # Compute durations in SQL (`::timestamp` strips trailing tz info so
        # we don't depend on Python 3.9's strict fromisoformat — it rejects the
        # bare `+00` suffix Postgres' NOW()::text produces).
        completed = conn.execute(
            """
            SELECT EXTRACT(EPOCH FROM (
                       MAX(s.completed_at::timestamp) - j.created_at::timestamp
                   )) / 60.0 AS duration_min
            FROM journeys j
            JOIN journey_steps s ON s.journey_id = j.id
            WHERE j.status = 'done' AND s.completed_at IS NOT NULL
            GROUP BY j.id, j.created_at
            """
        ).fetchall()
        durations: list[float] = [float(r["duration_min"]) for r in completed if r["duration_min"] is not None]
        # "Delay points" = average time between successive step completions.
        # completed_at is stored as ISO-formatted text; cast to timestamptz so
        # EXTRACT(EPOCH ...) gives us seconds, then convert to minutes.
        delay_rows = conn.execute(
            """
            SELECT s.test_code,
                   AVG(EXTRACT(EPOCH FROM (s.completed_at::timestamp
                                           - prev.completed_at::timestamp))) / 60.0
                       AS avg_gap_min
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
                {"test_code": r["test_code"],
                 "avg_gap_minutes": round(float(r["avg_gap_min"]), 1)}
                for r in delay_rows
            ],
        }


def record_findings(journey_id: int, test_code: str, findings: str) -> None:
    """Attach a free-text findings note to the most recent (or in-progress) step
    of `test_code` on `journey_id`."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE journey_steps SET findings_summary = %s
            WHERE journey_id = %s AND test_code = %s
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
            WHERE s.test_code = %s AND s.findings_summary IS NOT NULL AND s.findings_summary != ''
            ORDER BY s.completed_at DESC, s.id DESC
            LIMIT 1
            """,
            (test_code,),
        ).fetchone()
        return dict(row) if row else None


def findings_on_journey(journey_id: int, test_code: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT findings_summary FROM journey_steps "
            "WHERE journey_id = %s AND test_code = %s",
            (journey_id, test_code),
        ).fetchone()
        return (row["findings_summary"] if row else None) or None


def _chat_id_for_journey(journey_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT p.telegram_chat_id FROM journeys j "
            "JOIN patients p ON p.id = j.patient_id WHERE j.id = %s",
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
            "SELECT id FROM patients WHERE telegram_chat_id = %s", (chat_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"No patient for chat_id={chat_id}")
        return row["id"]


# Sanity check on app start: ensure all referenced codes exist in catalogue.
def validate_knowledge() -> None:
    codes = set(all_test_codes())
    for c in codes:
        assert get_test(c), f"catalogue missing entry for {c}"
