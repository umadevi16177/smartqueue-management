import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id INTEGER UNIQUE NOT NULL,
    display_name TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    voice_mode INTEGER NOT NULL DEFAULT 0,
    patient_identifier TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journeys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    status TEXT NOT NULL DEFAULT 'registering',
    -- registering | sequenced | in_progress | done | cancelled
    requested_tests_json TEXT NOT NULL,
    sequenced_tests_json TEXT,
    current_index INTEGER NOT NULL DEFAULT 0,
    blood_test_completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS journey_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journey_id INTEGER NOT NULL REFERENCES journeys(id),
    step_index INTEGER NOT NULL,
    test_code TEXT NOT NULL,
    queue_token TEXT,
    department_status TEXT NOT NULL DEFAULT 'pending',
    -- pending | in_queue | completed | rerouted | reserved
    reserved_for_time TEXT,
    findings_summary TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS departments (
    code TEXT PRIMARY KEY,
    queue_length INTEGER NOT NULL DEFAULT 0,
    estimated_wait_minutes INTEGER NOT NULL DEFAULT 0,
    availability TEXT NOT NULL DEFAULT 'open',
    -- open | maintenance | closed
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'idle',
    pending_data_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journey_id INTEGER NOT NULL REFERENCES journeys(id),
    rating INTEGER,
    raw_text TEXT,
    sentiment TEXT,
    tags_json TEXT,
    priority TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_MIGRATIONS = [
    "ALTER TABLE patients ADD COLUMN voice_mode INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE patients ADD COLUMN patient_identifier TEXT",
]


def init_db() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column/table already present — idempotent
