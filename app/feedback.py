"""Patient Feedback Collector + Sentiment Reader.

Stores raw + analysed feedback. The Hospital Admin Review Panel
(see app/main.py /admin) reads from here.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.db import get_conn
from app.llm import analyse_feedback


def record_patient_feedback(journey_id: int, raw_text: str) -> dict[str, Any]:
    rating = _extract_rating(raw_text)
    analysis = analyse_feedback(raw_text) if raw_text.strip() else {
        "sentiment": "neutral",
        "tags": [],
        "priority": "low",
        "summary_en": "",
    }
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback (journey_id, rating, raw_text, sentiment, tags_json, priority)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                journey_id,
                rating,
                raw_text,
                analysis["sentiment"],
                json.dumps(analysis["tags"]),
                analysis["priority"],
            ),
        )
        return {"id": cur.lastrowid, "rating": rating, **analysis}


def list_feedback(limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT f.*, p.display_name, p.language
            FROM feedback f
            JOIN journeys j ON j.id = f.journey_id
            JOIN patients p ON p.id = j.patient_id
            ORDER BY f.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags_json") or "[]")
            except Exception:
                d["tags"] = []
            results.append(d)
        return results


def _extract_rating(text: str) -> int | None:
    m = re.search(r"\b([1-5])\b", text)
    return int(m.group(1)) if m else None
