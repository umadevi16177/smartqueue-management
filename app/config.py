"""Configuration loader.

Reads from environment variables and an optional .env file. Pure stdlib —
no pydantic dependency so the engines run in a minimal environment.
"""
from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.isdigit() else default


class Settings:
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_url: str = _env("TELEGRAM_WEBHOOK_URL", "")
    telegram_webhook_secret: str = _env("TELEGRAM_WEBHOOK_SECRET", "dev-secret")

    llm_provider: str = _env("LLM_PROVIDER", "ollama")  # ollama | anthropic | none

    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")
    anthropic_model_fast: str = _env("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5")
    anthropic_model_reasoning: str = _env("ANTHROPIC_MODEL_REASONING", "claude-sonnet-4-6")

    ollama_base_url: str = _env("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = _env("OLLAMA_MODEL", "mistral")

    app_host: str = _env("APP_HOST", "0.0.0.0")
    app_port: int = _env_int("APP_PORT", 8000)
    # PostgreSQL only — no default. Set DATABASE_URL in .env (or via the
    # container's environment) to a postgresql:// or postgresql+psycopg2://
    # URL. Missing → fail fast with a clear message at startup.
    database_url: str = _env("DATABASE_URL", "")
    admin_password: str = _env("ADMIN_PASSWORD", "admin")

    hospital_name: str = _env("HOSPITAL_NAME", "City General Hospital")
    default_language: str = _env("DEFAULT_LANGUAGE", "en")


settings = Settings()

if not settings.database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. SmartQueue requires a PostgreSQL connection "
        "string, e.g. postgresql://postgres:<pw>@localhost:5433/smartqueue"
    )
