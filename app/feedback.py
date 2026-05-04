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


def feedback_metrics() -> dict[str, Any]:
    """Sentiment counts, average rating, top tags — for the admin panel."""
    with get_conn() as conn:
        sentiments = conn.execute(
            "SELECT sentiment, COUNT(*) AS n FROM feedback GROUP BY sentiment"
        ).fetchall()
        priorities = conn.execute(
            "SELECT priority, COUNT(*) AS n FROM feedback GROUP BY priority"
        ).fetchall()
        avg_rating = conn.execute(
            "SELECT AVG(rating) AS r FROM feedback WHERE rating IS NOT NULL"
        ).fetchone()
        rows = conn.execute("SELECT tags_json FROM feedback").fetchall()
    tag_counts: dict[str, int] = {}
    for r in rows:
        try:
            for t in json.loads(r["tags_json"] or "[]"):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        except Exception:
            continue
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    return {
        "sentiment_counts": {r["sentiment"]: r["n"] for r in sentiments},
        "priority_counts": {r["priority"]: r["n"] for r in priorities},
        "avg_rating": round(avg_rating["r"], 2) if avg_rating and avg_rating["r"] is not None else None,
        "top_tags": top_tags,
    }


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
