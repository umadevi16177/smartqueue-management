# SmartQueue — Smart Hospital Diagnostic System

A Telegram bot + FastAPI backend that medically sequences a patient's prescribed tests, guides them step-by-step through a hospital, dynamically reroutes when a department is unavailable, and collects feedback after the final test. Multilingual: English, Hindi, Telugu.

---

## Problem Statement

In Indian government hospitals, outpatients with multiple prescribed tests (Blood, ECG, Ultrasound, X-Ray, etc.) routinely:

- Wander between floors looking for the right department
- Wait in queues for tests that aren't optimally ordered
- Miss critical fasting / rest preparation between tests
- Get stuck when a department is on maintenance with no rerouting
- Have no language support — signage and staff often default to English

SmartQueue fixes all of this through a Telegram-first patient experience that requires no app install.

---

## Solution Architecture

A **three-bot architecture** built on a single shared FastAPI backend:

| Bot | Purpose | Telegram Handle |
|---|---|---|
| **Hub Bot** | Front desk / router. Sends new patients to Registration, returning patients straight to Diagnostic. | `@cityhospital_smartbot` |
| **Registration Bot** | Patient claims their hospital-issued Patient ID, picks language, gets a deep link to Diagnostic. | `@Registration_token_bot` |
| **Diagnostic Bot** | The clinical workhorse. Multi-test selection, sequence locking, step-by-step navigation, feedback. | `@Diagnostic_token_bot` |

All three run on one FastAPI process, distinguished by route: `/telegram/webhook/{hub,registration,diagnostic}`. Each bot has its own token; none share session state at the bot layer (state lives in Postgres).

### Six logical zones (from `final_system_design_v2.html`)

| Zone | Files |
|---|---|
| **Patient Entry** | Telegram client (no native app required) |
| **Telegram Bot Layer** | `app/telegram_bot.py`, `app/flow.py` |
| **AI Core** | `app/nlu.py`, `app/llm.py`, `app/sequence_engine.py`, `app/reroute_engine.py`, `app/journey.py` |
| **Data + Knowledge** | `app/knowledge.py`, `app/data/*.json`, `app/db.py`, `app/queue_store.py` |
| **Hospital Floor** | `app/templates/staff.html`, `/staff` routes, React control center |
| **Feedback + Admin** | `app/feedback.py`, `app/templates/admin*.html`, `/admin` routes |

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | **Python 3.9+ / FastAPI / uvicorn** | Async webhooks, type hints, lightweight |
| Bot framework | **python-telegram-bot 21.x** (webhook mode) | Production-grade, handles inline keyboards / callbacks |
| Database | **PostgreSQL** (Neon cloud) via psycopg2 with connection pooling | Race-safe queue assignment, schema isolation per environment |
| LLM (optional) | **Anthropic Claude Haiku** for NLU + sentiment, with offline heuristic fallback | Multilingual test name parsing |
| Frontend | **React + Vite + shadcn/ui + TanStack Query** | Real-time staff control center |
| Templating | **Jinja2** | Server-rendered `/staff` and `/admin` fallback pages |
| Voice (optional) | **gTTS** | Audio replies for low-literacy patients |

---

## Key Features

### 1. Clinical Sequence Engine (`app/sequence_engine.py`)

Topological sort over `clinical_rules.json:must_precede`, with `must_be_last` constraint enforced post-sort.

- **Hard constraint**: X-Ray must be last (radiation contamination affects other tests).
- **Soft constraints**: `must_precede` pairs (Blood → ECG, ECG → Ultrasound).
- **Deterministic tie-break**: canonical_order index when multiple tests are eligible.

Currently supports 10 tests: BLOOD, URINE, ECG, TMT, ULTRASOUND, PFT, EYE, MRI, CT, XRAY.

### 2. Reroute Engine (`app/reroute_engine.py`)

When a department is unavailable mid-journey, decides between two strategies:

- **Reorder** — if the test has `can_move_later: true` (e.g. ECG can defer until after Ultrasound).
- **Reserve slot** — if the test is `must_be_last` or has `can_move_later: false` (X-Ray closed → reserve a future time slot, alert when open).

Resolves the diagram's apparent contradiction ("ECG can move later" vs "X-Ray reads all 3 prior results") without breaking either rule. Data handoffs (ECG findings → Ultrasound) happen post-hoc once both tests complete.

### 3. Multi-select Test Picker

Inline keyboard with all 10 tests in a 2-column grid + language picker row + Confirm button. Each tap toggles ✅/⬜ via `toggle_test_selection()` — atomic SQL `SELECT ... FOR UPDATE` so concurrent taps can't clobber each other. Instant toast feedback (`"✅ Blood Test added (3 selected)"`) via `answer_callback_query` so users get feedback in ~200ms regardless of network speed.

### 4. Multilingual Support

- All patient-facing copy is in `app/data/messages.json` keyed by language (en/hi/te), so non-developers can edit translations.
- Test names, floor directions, prep instructions all multilingual.
- Language picker is part of the test menu — one tap re-renders the entire message in the new language.

### 5. Floor Map Continuity

Per-department PNGs in `app/static/floor_maps/` are attached to "Next: X" replies via Telegram `send_photo`. Generated by `python3 scripts/generate_floor_maps.py` (Pillow). Patient sees both written directions and a visual map.

### 6. Staff Control Center

- **React + Vite dashboard** at `:8080` — real-time view of unclaimed patients, active journeys, department availability, queue tokens, journey progress per patient.
- Staff can:
  - Issue Patient IDs (`POST /api/patients`)
  - Toggle department `availability` (open / closed / maintenance) — triggers reroute live
  - Mark a patient's current step done (`POST /api/journeys/{id}/complete-current`)
  - Record findings (e.g. ECG findings shared with Ultrasound team)
- Server-rendered Jinja `/staff` page is a fallback for staff on legacy browsers.

### 7. Admin Review Panel (`/admin?password=admin`)

- Journey metrics: avg duration, longest delays, delay-points per test
- Feedback metrics: sentiment counts, priority counts, avg rating, top tags
- Live edit of `clinical_rules.json` with validation — clinical staff can adjust ordering rules without a deploy.

### 8. Optional LLM Layer

`LLM_PROVIDER` in `.env` accepts `ollama`, `anthropic`, or `none`. All three return the same shape from `parse_test_request()`. If the chosen provider is unreachable, `app.nlu` script-based heuristic fallback kicks in automatically — the bot keeps working.

### 9. Voice Mode

`/voice` toggle on per-chat basis. When on, every text reply is also synthesized via gTTS and sent as a Telegram voice message. Skipped on button-tap callbacks (the toast already confirms the action and TTS adds 1-3s of latency).

---

## Project Structure

```
smartqueue_1/
├── app/
│   ├── main.py                  # FastAPI entrypoint, all HTTP routes
│   ├── flow.py                  # Conversation Flow Controller (3 bots dispatch)
│   ├── telegram_bot.py          # Telegram I/O — cached Bot pool, retry logic, toasts
│   ├── journey.py               # Patient state, sessions, atomic test toggles
│   ├── sequence_engine.py       # Clinical ordering (topological sort)
│   ├── reroute_engine.py        # Reorder vs Reserve decision
│   ├── knowledge.py             # JSON loaders (cached) + render_message
│   ├── nlu.py                   # Script-based test name parsing fallback
│   ├── llm.py                   # Pluggable LLM (ollama / anthropic / none)
│   ├── feedback.py              # Sentiment + tagging from patient feedback
│   ├── queue_store.py           # Department availability + queue lengths
│   ├── db.py                    # Postgres pool with stale-connection retry
│   ├── reply.py                 # Reply dataclass (text, photo, buttons, toast)
│   ├── voice.py                 # gTTS synthesis
│   ├── data/
│   │   ├── clinical_rules.json     # Sequence + reroute rules (single source of truth)
│   │   ├── test_catalogue.json     # 10 tests + multilingual aliases + directions
│   │   └── messages.json           # All patient-facing copy in 3 languages
│   ├── static/floor_maps/       # Per-department PNGs for Telegram send_photo
│   └── templates/               # Jinja /staff and /admin pages
├── frontend/frontend/
│   └── src/pages/Index.tsx      # React staff control center (TanStack Query polling)
├── scripts/
│   ├── end_to_end.py            # Full Ravi journey simulation + assertions
│   └── generate_floor_maps.py   # Regenerate PNG maps (Pillow)
├── dev.sh                       # Starts uvicorn :8000 + Vite :8080 together
├── requirements.txt
└── .env.example                 # All required environment variables
```

---

## Database Schema

PostgreSQL schema (`smartqueue` by default, configurable via `SMARTQUEUE_SCHEMA`):

| Table | Purpose |
|---|---|
| `patients` | Hospital-issued Patient ID, sequence_number (queue), telegram_chat_id, language, voice_mode |
| `journeys` | One per patient visit — requested_tests, sequenced_tests, current_index, status |
| `journey_steps` | One per test in the sequence — queue_token, completed_at, reserved_for_time |
| `sessions` | Transient per-chat state (`choosing_tests` selections, etc.) |
| `departments` | Live availability: open / closed / maintenance + queue_length |
| `findings` | Clinical findings recorded by staff (e.g. ECG → Ultrasound handoff) |
| `feedback` | Post-journey patient feedback with rating, sentiment, tags |
| `counters` | Race-safe queue sequence counter |

---

## Setup and Run

```sh
# 1. Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set tokens for HUB_BOT_TOKEN, REGISTRATION_BOT_TOKEN,
# DIAGNOSTIC_BOT_TOKEN, plus DATABASE_URL, TELEGRAM_WEBHOOK_URL

# 2. Run dev stack (backend + frontend with hot reload)
./dev.sh

# 3. Expose for Telegram (in another terminal)
ngrok http --url=<your-reserved-domain> 8000
# Set TELEGRAM_WEBHOOK_URL in .env, restart dev.sh
```

### Endpoints

- **Patient Telegram**: `@cityhospital_smartbot` (Hub) → either Registration or Diagnostic
- **Staff React dashboard**: <http://localhost:8080>
- **Staff Jinja fallback**: <http://localhost:8000/staff>
- **Admin panel**: <http://localhost:8000/admin?password=admin>
- **Health check**: <http://localhost:8000/api/health>

### Testing without Telegram

```sh
curl -X POST http://localhost:8000/debug/message \
  -H 'Content-Type: application/json' \
  -d '{"chat_id": 1, "name": "Ravi", "text": "/start"}'
```

### Smoke tests

```sh
python3 scripts/end_to_end.py    # Full Ravi journey + reroute scenarios + feedback
```

---

## Reliability & Performance

Key reliability decisions baked into the runtime:

- **Connection pool with stale-conn detection** (`app/db.py:get_conn`) — Neon closes idle connections, so the pool health-checks on checkout and discards dead handles instead of poisoning subsequent callers.
- **Cached `Bot` instances per bot type** (`app/telegram_bot.py:_BOTS`) — keeps httpx connection pools to `api.telegram.org` warm, avoids cold-connect TimedOut errors.
- **Single-retry helper `_retrying`** — wraps every Telegram API call. Catches `TimedOut` and `NetworkError`, sleeps 300ms, retries once.
- **Atomic test selection toggle** (`app/journey.py:toggle_test_selection`) — `SELECT ... FOR UPDATE` row lock. Concurrent button taps can't lose updates.
- **Toast feedback on every callback** — `answer_callback_query(text=...)` lands in ~200ms and confirms the user's action even when the slower `edit_message_text` round-trip is laggy.
- **Bounded semaphore on the connection pool** — bursts of webhook calls block instead of raising `PoolError`.

---

## Authority Hierarchy (Don't Violate)

1. **`must_be_last` is HARD** — X-Ray cannot move. Period.
2. **`must_precede` is SOFT** — preferred ordering for the Sequence Engine, but the Reroute Engine may relax it when a test has `reroute_permissions.can_move_later: true` (e.g. ECG).
3. **Data handoffs happen post-hoc** — ECG → Ultrasound findings transfer once both tests are completed; reordering does not break this.

---

## Recent Improvements

- Three-bot architecture (Hub / Registration / Diagnostic) with floor-map continuity
- Switched from SQLite to PostgreSQL with race-safe queue assignment
- Patient ID issuance moved to hospital reception (bot only claims existing IDs)
- Atomic multi-test selection with toast feedback
- 6 new tests added: MRI, CT, PFT, TMT, Urine, Eye Checkup
- Clinical rules canonical_order extended to all 10 tests
- Cached Bot pool + tight HTTPX timeouts + retry logic for slow networks
- Stale Postgres connection auto-discard
- React frontend dedupes journeys per patient and removes the 8-row cap

---

## Smoke Test Coverage

`python3 scripts/end_to_end.py` simulates Ravi's full journey:

1. Register → claim Patient ID → switch to Telugu
2. Type tests in Telugu (`నాకు blood test, ECG, ultrasound, X-Ray కావాలి`)
3. Confirm — sequence locks
4. **Reroute scenario 1**: ECG goes to maintenance before Blood completes → system reorders to Blood → Ultrasound → ECG → X-Ray
5. **Reroute scenario 2**: X-Ray closes before ECG completes → system reserves a future slot
6. Patient completes all 4 tests
7. Patient submits feedback ("5 — staff was very helpful")
8. Verifies floor map attached, ECG findings transferred to Ultrasound, journey metrics computed

Both reroute scenarios + feedback metrics + floor-map attachment are asserted. Test runs in an isolated `smartqueue_e2e_*` schema that's dropped on completion.
