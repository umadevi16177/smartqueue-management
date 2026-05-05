# Smart Hospital Diagnostic System

Implementation of the architecture in `final_system_design_v2.html`. A
Telegram bot front-end + FastAPI back-end that medically sequences a
patient's prescribed tests, guides them step-by-step, dynamically reroutes
when a department is unavailable, and collects feedback after the final test.

Mapped to the diagram zones:

| Zone | Files |
|------|-------|
| Patient Entry | (Telegram client) |
| Telegram Bot Layer | `app/telegram_bot.py`, `app/flow.py` |
| Artificial Intelligence Core | `app/nlu.py`, `app/llm.py`, `app/sequence_engine.py`, `app/journey.py`, `app/reroute_engine.py` |
| Data and Knowledge | `app/knowledge.py`, `app/data/*.json`, `app/db.py`, `app/queue_store.py` |
| Hospital Floor | `app/templates/staff.html`, `/staff` routes in `app/main.py` |
| Feedback and Admin | `app/feedback.py`, `app/templates/admin*.html`, `/admin` routes |

## Setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — at minimum set TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
```

To get a Telegram bot token: DM `@BotFather` → `/newbot`.

## Run

### Local dev (hot reload)

```sh
./dev.sh
```

Starts backend (uvicorn `:8000`) + frontend (Vite `:8080`) together with
hot reload. Ctrl-C stops both. Pre-flight checks venv, bun, Ollama, and
free ports.

### Docker (prod-like, single command)

```sh
docker compose up -d --build
```

Starts two containers:
- `smartqueue-backend` — Python 3.11 + uvicorn on `:8000`, talks to host's PostgreSQL via `host.docker.internal:5433`
- `smartqueue-frontend` — nginx serving the built React SPA on `:8080`, proxies `/api` and `/static` to the backend

Backend also talks to **host's Ollama** via `host.docker.internal:11434` (works
on macOS/Windows; Linux users — the compose file includes the host-gateway
mapping). For the container to reach the host's PostgreSQL, the host's PG
must allow connections from the Docker bridge — see comment in `docker-compose.yml`.

Stop: `docker compose down`.

For Telegram in production: `ngrok http 8000`, put that URL in
`TELEGRAM_WEBHOOK_URL` in `.env`, restart. The webhook auto-registers on app startup.

## Try it without Telegram

The `/debug/message` endpoint simulates a patient message:

```sh
curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 1, "name": "Ravi", "text": "/start"}'

curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 1, "text": "/telugu"}'

curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 1, "text": "నాకు blood test, ECG, ultrasound, X-Ray కావాలి"}'

curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 1, "text": "/confirm"}'

# Mark current step done (cycle through Blood → ECG → Ultrasound → X-Ray):
curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 1, "text": "/done"}'
```

## Smoke tests

```sh
python3 scripts/smoke_test.py        # core engines (no deps required)
python3 scripts/end_to_end.py        # full conversation flow (needs deps)
```

## Dashboards

- **Staff Queue Dashboard**: <http://localhost:8000/staff>
  Set `availability=maintenance` on ECG and complete a Blood Test step to
  see the Reroute Engine fire.
- **Admin Review Panel**: <http://localhost:8000/admin?password=admin>

## Clinical rule authority

The Clinical Rules Store (`app/data/clinical_rules.json`) is the single
source of truth for ordering. Two kinds of constraints:

- `must_be_last` (HARD): X-Ray cannot be moved.
- `reroute_permissions.can_move_later` (OVERRIDE): when true (e.g. ECG),
  the Reroute Engine may defer the test past its preferred position. The
  data handoff (ECG → Ultrasound) still happens once both are completed.
