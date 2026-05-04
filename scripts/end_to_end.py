"""End-to-end test: simulate Ravi's full journey through the conversation flow.

Requires app dependencies (pydantic-settings, fastapi-related deps may be needed).
Uses a temp SQLite DB so it doesn't pollute the real one.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a temp DB before any app modules import settings.
tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmpdb.close()
os.environ["DATABASE_URL"] = f"sqlite:///{tmpdb.name}"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
# Force heuristic NLU/sentiment so e2e doesn't depend on a running Ollama.
os.environ["LLM_PROVIDER"] = "none"

from app.db import init_db
from app.feedback import feedback_metrics
from app.flow import handle_message
from app.journey import journey_metrics, latest_findings_for, record_findings, get_active_journey
from app.queue_store import ensure_seeded, update_department


def step(label, replies):
    print(f"\n— {label} —")
    for r in replies:
        suffix = f"  📷 {Path(r.photo).name}" if getattr(r, "photo", None) else ""
        print(f"  bot: {r.text}{suffix}")


def main() -> None:
    init_db()
    ensure_seeded()

    chat_id = 42

    step("/start", handle_message(chat_id, "Ravi", "/start"))
    step("/telugu", handle_message(chat_id, "Ravi", "/telugu"))
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

    # Verify the floor map was attached to the first directions message.
    confirm_replies = handle_message(chat_id=99, sender_name="Test", text="/start")
    handle_message(chat_id=99, sender_name="Test", text="blood test, ECG, ultrasound, X-ray")
    confirm_replies = handle_message(chat_id=99, sender_name="Test", text="/confirm")
    assert any(getattr(r, "photo", None) and r.photo.endswith("blood.png") for r in confirm_replies), \
        "Expected blood.png floor map attached to first /confirm reply"
    print("  floor_map_attached: blood.png ✓")

    print("\n— Admin metrics —")
    print(f"  journey_metrics: {journey_metrics()}")
    print(f"  feedback_metrics: {feedback_metrics()}")

    print("\nDone. DB:", tmpdb.name)


if __name__ == "__main__":
    main()
