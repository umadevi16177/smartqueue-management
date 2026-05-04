"""LLM-backed NLU + sentiment analysis.

Three providers, selected via `LLM_PROVIDER` in .env:
  - ollama (default): local Ollama, JSON-mode chat
  - anthropic: Claude API
  - none: skip the LLM, use heuristics only

Every call degrades gracefully — if the chosen provider is unreachable, the
script/keyword fallbacks in `app.nlu` and `_fallback_sentiment` keep the
system working without external dependencies.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from app.config import settings
from app.knowledge import all_test_codes

log = logging.getLogger(__name__)


SYSTEM_PROMPT_NLU = """You are the Language Understanding Engine for a hospital triage bot.
The patient may write in Telugu, Hindi, or English (mixed scripts allowed).
They are listing the medical tests their doctor prescribed on paper.

Your job: identify which of these canonical test codes are mentioned, in any
language. Reply with a JSON object only, no prose:

{"language": "te|hi|en", "tests": ["BLOOD","ECG","ULTRASOUND","XRAY"]}

Only include codes from this list: BLOOD, ECG, ULTRASOUND, XRAY.
If unsure, leave tests empty."""


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


# ─── Provider implementations ─────────────────────────────────────────────────


def _call_ollama_json(system: str, user: str, max_tokens: int = 300) -> dict | None:
    """Call Ollama's /api/chat with format=json. Returns parsed JSON or None."""
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    payload = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {"num_predict": max_tokens, "temperature": 0.0},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        # 180s covers Mistral cold-start (~100s on first call after Ollama wakes
        # the model). Warm calls return in 10-20s.
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        log.warning("Ollama unreachable: %s", e)
        return None
    except Exception:
        log.exception("Ollama call failed")
        return None
    content = (data.get("message") or {}).get("content", "").strip()
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        log.warning("Ollama returned non-JSON content: %s", content[:200])
        return None


def _call_anthropic_json(system: str, user: str, max_tokens: int = 300) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.anthropic_model_fast,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        body = msg.content[0].text.strip().strip("`").removeprefix("json").strip()
        return json.loads(body)
    except Exception:
        log.exception("Anthropic call failed")
        return None


def _llm_json(system: str, user: str, max_tokens: int = 300) -> dict | None:
    provider = (settings.llm_provider or "").lower()
    if provider == "ollama":
        return _call_ollama_json(system, user, max_tokens)
    if provider == "anthropic":
        return _call_anthropic_json(system, user, max_tokens)
    return None


# ─── Public API ───────────────────────────────────────────────────────────────


def parse_test_request(text: str) -> dict[str, Any]:
    """Parse a free-text patient message into structured (language, tests)."""
    from app.nlu import detect_language, extract_test_codes

    parsed = _llm_json(SYSTEM_PROMPT_NLU, text, max_tokens=200) if text.strip() else None
    if parsed is None:
        return {"language": detect_language(text), "tests": extract_test_codes(text)}

    valid = set(all_test_codes())
    tests = [t for t in parsed.get("tests", []) if t in valid]
    lang = parsed.get("language", "en")
    if lang not in ("te", "hi", "en"):
        lang = "en"
    return {"language": lang, "tests": tests}


def analyse_feedback(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"sentiment": "neutral", "tags": [], "priority": "low", "summary_en": ""}

    parsed = _llm_json(SYSTEM_PROMPT_SENTIMENT, text, max_tokens=300)
    if parsed is None:
        return _fallback_sentiment(text)
    return {
        "sentiment": parsed.get("sentiment", "neutral"),
        "tags": parsed.get("tags", []),
        "priority": parsed.get("priority", "low"),
        "summary_en": parsed.get("summary_en", text[:120]),
    }


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
