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

from app.db import init_db
from app.flow import handle_message
from app.queue_store import ensure_seeded, update_department


def step(label, replies):
    print(f"\n— {label} —")
    for r in replies:
        print(f"  bot: {r}")


def main() -> None:
    init_db()
    ensure_seeded()

    chat_id = 42

    step("/start", handle_message(chat_id, "Ravi", "/start"))
    step("/telugu", handle_message(chat_id, "Ravi", "/telugu"))
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

    step("/done (Ultrasound)", handle_message(chat_id, "Ravi", "/done"))

    print("\n— Staff: X-Ray room closes BEFORE ECG /done —")
    update_department("XRAY", availability="closed")

    step("/done (ECG — system tries to advance to X-Ray, must reserve)",
         handle_message(chat_id, "Ravi", "/done"))

    print("\n— X-Ray reopens, patient finally completes —")
    update_department("XRAY", availability="open")
    step("/done (X-Ray)", handle_message(chat_id, "Ravi", "/done"))
    step(
        "Patient feedback",
        handle_message(chat_id, "Ravi", "5 — staff was very helpful, thank you"),
    )

    print("\nDone. DB:", tmpdb.name)


if __name__ == "__main__":
    main()
