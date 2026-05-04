"""Claude API wrapper for NLU + sentiment.

Both functions degrade gracefully if no API key is configured — the system
falls back to script/keyword heuristics so the demo runs end-to-end without
network access.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.knowledge import all_test_codes


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic

        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return _client
    except Exception:
        return None


SYSTEM_PROMPT_NLU = """You are the Language Understanding Engine for a hospital triage bot.
The patient may write in Telugu, Hindi, or English (mixed scripts allowed).
They are listing the medical tests their doctor prescribed on paper.

Your job: identify which of these canonical test codes are mentioned, in any
language. Reply with a JSON object only, no prose:

{"language": "te|hi|en", "tests": ["BLOOD","ECG","ULTRASOUND","XRAY"]}

Only include codes from this list: BLOOD, ECG, ULTRASOUND, XRAY.
If unsure, leave tests empty."""


def parse_test_request(text: str) -> dict[str, Any]:
    """Use Claude (fast model) for robust multilingual extraction.

    Falls back to script/alias heuristics in app.nlu if Claude is unavailable.
    """
    from app.nlu import detect_language, extract_test_codes

    client = _get_client()
    if client is None:
        return {"language": detect_language(text), "tests": extract_test_codes(text)}

    try:
        msg = client.messages.create(
            model=settings.anthropic_model_fast,
            max_tokens=200,
            system=SYSTEM_PROMPT_NLU,
            messages=[{"role": "user", "content": text}],
        )
        body = msg.content[0].text.strip()
        body = body.strip("`").removeprefix("json").strip()
        parsed = json.loads(body)
        valid = set(all_test_codes())
        tests = [t for t in parsed.get("tests", []) if t in valid]
        lang = parsed.get("language", "en")
        if lang not in ("te", "hi", "en"):
            lang = "en"
        return {"language": lang, "tests": tests}
    except Exception:
        return {"language": detect_language(text), "tests": extract_test_codes(text)}


SYSTEM_PROMPT_SENTIMENT = """You are the Feedback Sentiment Reader for a hospital.
The patient just finished four diagnostic tests. They wrote a free-text comment
in Telugu, Hindi, or English.

Read their tone and tag the issues they mention. Reply with a JSON object only:

{
  "sentiment": "positive|neutral|negative",
  "tags": ["wait_time","staff","cleanliness","navigation","communication","other"],
  "priority": "low|medium|high",
  "summary_en": "one short English sentence"
}

priority=high if patient mentions safety, harm, severe distress, or being lost
for a long time. priority=medium if they mention significant frustration.
priority=low otherwise."""


def analyse_feedback(text: str) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        return _fallback_sentiment(text)
    try:
        msg = client.messages.create(
            model=settings.anthropic_model_fast,
            max_tokens=300,
            system=SYSTEM_PROMPT_SENTIMENT,
            messages=[{"role": "user", "content": text}],
        )
        body = msg.content[0].text.strip()
        body = body.strip("`").removeprefix("json").strip()
        parsed = json.loads(body)
        return {
            "sentiment": parsed.get("sentiment", "neutral"),
            "tags": parsed.get("tags", []),
            "priority": parsed.get("priority", "low"),
            "summary_en": parsed.get("summary_en", text[:120]),
        }
    except Exception:
        return _fallback_sentiment(text)


def _fallback_sentiment(text: str) -> dict[str, Any]:
    lower = text.lower()
    neg_words = ["bad", "slow", "wait", "long", "lost", "rude", "dirty", "confus", "delay"]
    pos_words = ["good", "great", "fast", "helpful", "clean", "thank", "excellent", "nice"]
    score = sum(1 for w in pos_words if w in lower) - sum(1 for w in neg_words if w in lower)
    sentiment = "positive" if score > 0 else "negative" if score < 0 else "neutral"
    tags: list[str] = []
    if any(w in lower for w in ["wait", "slow", "long", "delay"]):
        tags.append("wait_time")
    if any(w in lower for w in ["lost", "confus", "direction"]):
        tags.append("navigation")
    if any(w in lower for w in ["rude", "staff", "doctor", "nurse"]):
        tags.append("staff")
    return {
        "sentiment": sentiment,
        "tags": tags or ["other"],
        "priority": "medium" if sentiment == "negative" and tags else "low",
        "summary_en": text[:120],
    }
