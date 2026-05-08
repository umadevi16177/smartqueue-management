"""Real-Time Queue Store.

Tracks live queue length, estimated wait, and availability per department.
Backed by Postgres (smartqueue schema). Updated by the Staff Dashboard
and consumed by the Conversation Flow Controller and Reroute Engine.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db import get_conn
from app.knowledge import all_test_codes


_SEEDED = False


def ensure_seeded() -> None:
    """Make sure every test in the catalogue has a department row.

    Idempotent — but the dashboard was calling it on every poll, which
    issued 10 INSERTs (one per test code) per request. With Neon's ~400ms
    cloud RTT that's ~4s of wasted work per poll. We now seed once per
    process and skip subsequent calls.
    """
    global _SEEDED
    if _SEEDED:
        return
    with get_conn() as conn:
        for code in all_test_codes():
            conn.execute(
                "INSERT INTO departments (code) VALUES (%s) ON CONFLICT (code) DO NOTHING",
                (code,),
            )
    _SEEDED = True


def list_departments() -> list[dict[str, Any]]:
    ensure_seeded()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM departments ORDER BY code").fetchall()
        return [dict(r) for r in rows]


def get_department(code: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM departments WHERE code = %s", (code,)
        ).fetchone()
        return dict(row) if row else None


def update_department(
    code: str,
    queue_length: int | None = None,
    estimated_wait_minutes: int | None = None,
    availability: str | None = None,
) -> dict[str, Any]:
    fields: list[str] = []
    params: list[Any] = []
    if queue_length is not None:
        fields.append("queue_length = %s")
        params.append(max(0, int(queue_length)))
    if estimated_wait_minutes is not None:
        fields.append("estimated_wait_minutes = %s")
        params.append(max(0, int(estimated_wait_minutes)))
    if availability is not None:
        if availability not in ("open", "maintenance", "closed"):
            raise ValueError(f"availability must be open|maintenance|closed, got {availability!r}")
        fields.append("availability = %s")
        params.append(availability)
    if not fields:
        return get_department(code)  # type: ignore[return-value]
    fields.append("updated_at = %s")
    params.append(datetime.utcnow().isoformat(timespec="seconds"))
    params.append(code)
    with get_conn() as conn:
        conn.execute(f"UPDATE departments SET {', '.join(fields)} WHERE code = %s", params)
    return get_department(code)  # type: ignore[return-value]


def department_unavailable(code: str) -> bool:
    d = get_department(code)
    return bool(d and d["availability"] != "open")
