"""PostgreSQL backend.

Connection-pooled psycopg2 with a thin sqlite3-compatible facade so the
existing `with get_conn() as conn: conn.execute(...).fetchone()` call
sites keep working unchanged.

All app tables live under a dedicated `smartqueue` schema so we don't
collide with anything else in the target database.

Differences from the old SQLite backend that callers must be aware of:
  - Placeholders are %s (psycopg2 default), not ?.
  - There is no `cur.lastrowid` for plain INSERTs. Use explicit
    `INSERT ... RETURNING id` and `cur.fetchone()[0]` if you need the new
    primary key.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg2
import psycopg2.extras
import psycopg2.pool

from app.config import settings


log = logging.getLogger(__name__)

# Schema name is overridable for test isolation — set SMARTQUEUE_SCHEMA in the
# environment before any app module imports `app.db` to redirect every table
# (and the connection-pool search_path) to a throwaway schema.
SCHEMA_NAME = os.environ.get("SMARTQUEUE_SCHEMA", "smartqueue")


def _build_schema_sql(schema: str) -> str:
    """Render the DDL for `schema`. Computed at call time (not module load)
    so test code that mutates SCHEMA_NAME between imports works correctly."""
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.counters (
    name TEXT PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS {schema}.patients (
    id BIGSERIAL PRIMARY KEY,
    telegram_chat_id BIGINT UNIQUE,
    display_name TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    voice_mode INTEGER NOT NULL DEFAULT 0,
    patient_identifier TEXT UNIQUE,
    sequence_number BIGINT,
    created_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text
);

CREATE TABLE IF NOT EXISTS {schema}.journeys (
    id BIGSERIAL PRIMARY KEY,
    patient_id BIGINT NOT NULL REFERENCES {schema}.patients(id),
    status TEXT NOT NULL DEFAULT 'registering',
    requested_tests_json TEXT NOT NULL,
    sequenced_tests_json TEXT,
    current_index INTEGER NOT NULL DEFAULT 0,
    blood_test_completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text,
    updated_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text
);

CREATE TABLE IF NOT EXISTS {schema}.journey_steps (
    id BIGSERIAL PRIMARY KEY,
    journey_id BIGINT NOT NULL REFERENCES {schema}.journeys(id),
    step_index INTEGER NOT NULL,
    test_code TEXT NOT NULL,
    queue_token TEXT,
    department_status TEXT NOT NULL DEFAULT 'pending',
    reserved_for_time TEXT,
    findings_summary TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text
);

CREATE TABLE IF NOT EXISTS {schema}.departments (
    code TEXT PRIMARY KEY,
    queue_length INTEGER NOT NULL DEFAULT 0,
    estimated_wait_minutes INTEGER NOT NULL DEFAULT 0,
    availability TEXT NOT NULL DEFAULT 'open',
    updated_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text
);

CREATE TABLE IF NOT EXISTS {schema}.sessions (
    chat_id BIGINT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'idle',
    pending_data_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text
);

CREATE TABLE IF NOT EXISTS {schema}.feedback (
    id BIGSERIAL PRIMARY KEY,
    journey_id BIGINT NOT NULL REFERENCES {schema}.journeys(id),
    rating INTEGER,
    raw_text TEXT,
    sentiment TEXT,
    tags_json TEXT,
    priority TEXT,
    created_at TEXT NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')::text
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_sequence_number
    ON {schema}.patients(sequence_number)
    WHERE sequence_number IS NOT NULL;
"""


def _normalize_url(raw: str) -> str:
    """Strip SQLAlchemy-style driver hints (`postgresql+psycopg2://...`) so
    psycopg2 accepts the URL unchanged."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", raw)


_POOL_MAX = 20
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
# Bounded semaphore so callers BLOCK when all connections are checked out,
# rather than getting PoolError. Sized to match the pool's maxconn.
_pool_semaphore: threading.BoundedSemaphore = threading.BoundedSemaphore(_POOL_MAX)


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=_POOL_MAX,
                    dsn=_normalize_url(settings.database_url),
                    options=f"-c search_path={SCHEMA_NAME},public",
                )
    return _pool


# ── sqlite3-compatible facade ─────────────────────────────────

class _PgConn:
    """Thin facade so existing `conn.execute(...)` / `conn.executescript(...)`
    call sites work unchanged. Each `execute()` opens its own DictCursor —
    rows support both index (`row[0]`) and key (`row["id"]`) access, like
    sqlite3.Row."""

    __slots__ = ("_conn",)

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql, params or ())
        return cur

    def executescript(self, sql: str) -> None:
        # psycopg2 happily runs multi-statement strings via execute().
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


@contextmanager
def get_conn() -> Iterator[_PgConn]:
    pool = _get_pool()
    # Block (don't raise) when the pool is fully checked out. Without this
    # gate, a burst of concurrent callers > maxconn raises PoolError.
    _pool_semaphore.acquire()
    try:
        raw = pool.getconn()
        try:
            yield _PgConn(raw)
            raw.commit()
        except Exception:
            try:
                raw.rollback()
            except Exception:
                pass
            raise
        finally:
            pool.putconn(raw)
    finally:
        _pool_semaphore.release()


# ── Init / migrations ─────────────────────────────────────────

def init_db() -> None:
    """Create schema + seed counter. Idempotent — safe to call repeatedly."""
    with get_conn() as conn:
        conn.executescript(_build_schema_sql(SCHEMA_NAME))
        _seed_queue_counter(conn)


def _seed_queue_counter(conn: _PgConn) -> None:
    conn.execute(
        "INSERT INTO counters (name, value) VALUES ('queue_seq', 0) "
        "ON CONFLICT (name) DO NOTHING"
    )
    conn.execute(
        "UPDATE counters SET value = GREATEST(value, "
        "  COALESCE((SELECT MAX(sequence_number) FROM patients), 0)) "
        "WHERE name = 'queue_seq'"
    )


# Re-exports so callers can keep their existing exception handling.
IntegrityError = psycopg2.IntegrityError
OperationalError = psycopg2.OperationalError
