"""End-to-end test: simulate Ravi's full journey through the conversation flow.

Isolates itself by using a per-PID Postgres schema (`smartqueue_e2e_<pid>`)
that's dropped via try/finally — the production smartqueue schema is never
touched. Requires the same DATABASE_URL the app uses.

Heuristic NLU/sentiment is forced (LLM_PROVIDER=none) so this doesn't depend
on a running Ollama or a Claude API key.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pick a unique schema for this run, BEFORE any app module imports `app.db`
# (so it captures SCHEMA_NAME from the env).
E2E_SCHEMA = f"smartqueue_e2e_{os.getpid()}"
os.environ["SMARTQUEUE_SCHEMA"] = E2E_SCHEMA
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("LLM_PROVIDER", "none")  # heuristic NLU/sentiment

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.feedback import feedback_metrics  # noqa: E402
from app.flow import handle_message  # noqa: E402
from app.journey import (  # noqa: E402
    get_active_journey,
    journey_metrics,
    latest_findings_for,
    record_findings,
    staff_register_patient,
)
from app.queue_store import ensure_seeded, update_department  # noqa: E402

import psycopg2  # noqa: E402


def step(label, replies):
    print(f"\n— {label} —")
    for r in replies:
        suffix = f"  📷 {Path(r.photo).name}" if getattr(r, "photo", None) else ""
        print(f"  bot: {r.text}{suffix}")


def _drop_schema() -> None:
    """Tear down the per-run schema. Safe to call even if init_db() never ran."""
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", settings.database_url)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {E2E_SCHEMA} CASCADE")
    finally:
        conn.close()


def main() -> None:
    init_db()
    ensure_seeded()

    # The bot now requires a staff-issued Patient ID before accepting a
    # prescription. Register Ravi (and a second test patient) ahead of time,
    # then have the bot claim each ID via the message flow.
    ravi = staff_register_patient("Ravi")
    second = staff_register_patient("Test Floor Map")
    print(f"Pre-registered: Ravi → {ravi['patient_id']} (queue #{ravi['sequence_number']})")
    print(f"Pre-registered: Floor-map test → {second['patient_id']} (queue #{second['sequence_number']})")

    chat_id = 42

    step("/start", handle_message(chat_id, "Ravi", "/start"))
    step("/telugu", handle_message(chat_id, "Ravi", "/telugu"))
    step(f"Patient claims their ID ({ravi['patient_id']})",
         handle_message(chat_id, "Ravi", ravi["patient_id"]))
    step("/voice (turn on voice mode)", handle_message(chat_id, "Ravi", "/voice"))
    step(
        "Patient types tests in Telugu",
        handle_message(
            chat_id, "Ravi", "నాకు blood test, ECG, ultrasound, X-Ray కావాలి"
        ),
    )
    step("/confirm", handle_message(chat_id, "Ravi", "/confirm"))

    print("\n— Staff: ECG goes under maintenance BEFORE Ravi finishes Blood —")
    update_department("ECG", availability="maintenance")

    step("/done (Blood — system should reroute around ECG)",
         handle_message(chat_id, "Ravi", "/done"))

    print("\n— ECG reopens (Ultrasound is the next step after reroute) —")
    update_department("ECG", availability="open")

    print("\n— Staff: record ECG findings before patient reaches Ultrasound —")
    j = get_active_journey(chat_id)
    assert j is not None, "Ravi's journey should be active here"
    record_findings(j["id"], "ECG", "Sinus rhythm, mild bradycardia. Focus on left ventricle.")

    step("/done (Ultrasound)", handle_message(chat_id, "Ravi", "/done"))

    print("\n— Staff: X-Ray room closes BEFORE ECG /done —")
    update_department("XRAY", availability="closed")

    step("/done (ECG — system tries to advance to X-Ray, must reserve)",
         handle_message(chat_id, "Ravi", "/done"))

    print("\n— X-Ray reopens, patient finally completes —")
    update_department("XRAY", availability="open")
    step("/done (X-Ray)", handle_message(chat_id, "Ravi", "/done"))

    print("\n— Patient feedback —")
    step("Patient says '5 — staff was very helpful, thank you'",
         handle_message(chat_id, "Ravi", "5 — staff was very helpful, thank you"))

    print("\n— Verifying new features —")
    findings = latest_findings_for("ECG")
    print(f"  latest_ecg_findings: {findings['findings_summary'] if findings else None}")
    assert findings and "Sinus" in findings["findings_summary"]

    # Floor map: a second patient claims their ID then sends a prescription;
    # the /confirm reply should attach blood.png.
    handle_message(chat_id=99, sender_name="Test", text="/start")
    handle_message(chat_id=99, sender_name="Test", text=second["patient_id"])
    handle_message(chat_id=99, sender_name="Test", text="blood test, ECG, ultrasound, X-ray")
    confirm_replies = handle_message(chat_id=99, sender_name="Test", text="/confirm")
    assert any(getattr(r, "photo", None) and r.photo.endswith("blood.png") for r in confirm_replies), \
        "Expected blood.png floor map attached to first /confirm reply"
    print("  floor_map_attached: blood.png ✓")

    print("\n— Admin metrics —")
    print(f"  journey_metrics: {journey_metrics()}")
    print(f"  feedback_metrics: {feedback_metrics()}")

    print(f"\nDone. Schema: {E2E_SCHEMA} (will be dropped)")


if __name__ == "__main__":
    try:
        main()
    finally:
        _drop_schema()
