"""One-shot migration: copy data from the old SQLite DB into Postgres.

Reads `smartqueue.db` at the project root and writes into the smartqueue
schema in the Postgres DB pointed at by DATABASE_URL. Preserves primary
key IDs and resets each sequence to MAX(id) so future inserts continue
without collision.

Idempotent-ish: clears the smartqueue tables first (fresh import), so re-running
overwrites whatever was in Postgres but doesn't touch other schemas. Run with:

    python3 scripts/migrate_sqlite_to_postgres.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn, init_db  # noqa: E402

SQLITE_PATH = ROOT / "smartqueue.db"

# Order matters: parents before children (FKs).
TABLES = [
    ("patients", [
        "id", "telegram_chat_id", "display_name", "language", "voice_mode",
        "patient_identifier", "sequence_number", "created_at",
    ]),
    ("journeys", [
        "id", "patient_id", "status", "requested_tests_json",
        "sequenced_tests_json", "current_index", "blood_test_completed_at",
        "created_at", "updated_at",
    ]),
    ("journey_steps", [
        "id", "journey_id", "step_index", "test_code", "queue_token",
        "department_status", "reserved_for_time", "findings_summary",
        "completed_at", "created_at",
    ]),
    ("departments", [
        "code", "queue_length", "estimated_wait_minutes", "availability",
        "updated_at",
    ]),
    ("sessions", [
        "chat_id", "state", "pending_data_json", "updated_at",
    ]),
    ("feedback", [
        "id", "journey_id", "rating", "raw_text", "sentiment", "tags_json",
        "priority", "created_at",
    ]),
]


def main() -> None:
    if not SQLITE_PATH.exists():
        print(f"No SQLite DB at {SQLITE_PATH} — nothing to migrate.")
        return

    init_db()
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row

    with get_conn() as pg:
        # Wipe Postgres-side smartqueue tables (only smartqueue, not the
        # unrelated public-schema tables in this DB).
        pg.execute(
            "TRUNCATE patients, journeys, journey_steps, departments, "
            "sessions, feedback RESTART IDENTITY CASCADE"
        )

        total = 0
        for table, cols in TABLES:
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            if not rows:
                print(f"  {table}: 0 rows")
                continue
            placeholders = ", ".join(["%s"] * len(cols))
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
            for r in rows:
                pg.execute(sql, tuple(r[c] for c in cols))
            print(f"  {table}: {len(rows)} rows")
            total += len(rows)

        # Reset every BIGSERIAL sequence so future inserts continue past the
        # max copied id. Tables without an `id` PK (departments uses code,
        # sessions uses chat_id) are skipped.
        for table in ("patients", "journeys", "journey_steps", "feedback"):
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence('smartqueue.{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT MAX(id) FROM {table}) IS NOT NULL)"
            )

        # Also resync the queue_seq counter to current MAX(sequence_number).
        pg.execute(
            "UPDATE counters SET value = GREATEST(value, "
            "  COALESCE((SELECT MAX(sequence_number) FROM patients), 0)) "
            "WHERE name = 'queue_seq'"
        )

        # Sanity-check counts on the Postgres side.
        for table, _ in TABLES:
            n = pg.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            print(f"  pg.{table}: {n} rows")

    src.close()
    print(f"\nMigrated {total} rows.")


if __name__ == "__main__":
    main()
