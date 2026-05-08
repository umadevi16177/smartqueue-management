# SmartQueue — Architecture & Build Journey

How this system was built from scratch — the order in which each layer landed, the architectural decisions at each step, and the data flow through the live system.

---

## Part 1 — Build Journey (from zero to production)

This section walks through the actual phases of construction, in chronological order. Each phase had a specific problem to solve and produced a concrete deliverable.

### Phase 0 — Architecture spec (`final_system_design_v2.html`)

Before any code, the system was designed as **six logical zones**:

1. Patient Entry
2. Telegram Bot Layer
3. AI Core
4. Data + Knowledge
5. Hospital Floor
6. Feedback + Admin

Each zone has clear responsibilities and edges. This is the contract every later phase must respect.

### Phase 1 — Core engines (no I/O, no DB, just logic)

**Goal**: prove the hard problem — clinical sequencing and rerouting — works as pure functions.

Built first because they're testable in isolation and inform every later layer:

| File | Role |
|---|---|
| `app/sequence_engine.py` | Topological sort over `must_precede` rules + `must_be_last` enforcement. Pure function: `list[str] → list[str]`. |
| `app/reroute_engine.py` | Decide between **reorder** and **reserve slot** based on `reroute_permissions`. Pure function: `(sequence, current_index, unavailable_test) → Decision`. |
| `app/data/clinical_rules.json` | Single source of truth: `canonical_order`, `must_precede`, `must_be_last`, `rest_periods`, `data_handoffs`, `reroute_permissions`. |

**Key design decision**: Make the rules **data, not code**. Hospital staff can edit `clinical_rules.json` to change ordering without a deploy. This is the diagram's "Clinical Rules Store" zone.

### Phase 2 — Knowledge & data layer

**Goal**: give engines and bot a place to read static catalogue data and write transactional state.

| File | Role |
|---|---|
| `app/data/test_catalogue.json` | 10 tests with multilingual aliases, floor numbers, room codes, directions. |
| `app/data/messages.json` | Every patient-facing string in English/Hindi/Telugu, keyed by intent (e.g. `claimed`, `welcome_diagnostic`). |
| `app/knowledge.py` | Loaders for the JSON files (with `@lru_cache`), `render_message(key, lang, **vars)` template formatter. |
| `app/db.py` | Connection-pooled Postgres facade. Schema migrations in idempotent `init_db()`. |
| `app/journey.py` | Patient state — `claim_patient_by_id`, `start_journey`, `mark_step_completed`, `apply_reroute`, sessions. |

**Key design decision**: All patient-facing copy lives in JSON, not Python strings. Non-developers can edit translations.

### Phase 3 — Conversation Flow Controller

**Goal**: orchestrate the engines and data layer into a coherent conversation.

`app/flow.py:handle_message(chat_id, sender_name, text, bot_type) → list[Reply]`

Key insight: the flow controller is **stateless**. All state lives in Postgres. This means:

- Webhook calls are idempotent (Telegram retries don't corrupt state)
- Multiple uvicorn workers can serve the same chat
- Restarts don't lose anything

### Phase 4 — Telegram I/O layer

**Goal**: connect the flow controller to actual Telegram users.

`app/telegram_bot.py:process_update()` translates a raw Telegram webhook payload into:
1. Extract `chat_id`, `text` (or `callback_query.data`), sender info
2. Call `handle_message()` to get `list[Reply]`
3. Send each Reply via Bot API (text, photo, voice, inline keyboard)

`app/main.py` exposes `/telegram/webhook/{bot_type}` FastAPI routes.

**Key design decision**: separate `flow.handle_message` (pure logic) from `telegram_bot.process_update` (I/O). The flow controller has no knowledge of Telegram — easy to test, easy to swap front-ends (debug HTTP, future SMS, kiosk).

### Phase 5 — Three-bot architecture

**Goal**: solve the UX problem that one Telegram bot username doing everything was confusing for patients.

Split into three bots, each with a clear purpose:

```
┌──────────────┐      ┌──────────────────────┐
│   New patient│ ───► │  Hub Bot (front desk)│
└──────────────┘      └──────────┬───────────┘
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
        ┌────────────────────┐    ┌──────────────────────┐
        │ Registration Bot   │    │  Diagnostic Bot      │
        │ - claim Patient ID │    │  - select tests      │
        │ - pick language    │    │  - lock sequence     │
        │ - deep link →      │ ─► │  - guide step-by-step│
        └────────────────────┘    │  - collect feedback  │
                                  └──────────────────────┘
```

All three share **one FastAPI process** and **one Postgres**. They differ only by:
- Telegram token (separate `@cityhospital_smartbot`, `@Registration_token_bot`, `@Diagnostic_token_bot`)
- The branch of `handle_message` they enter (via `bot_type` parameter)

Returning patients deep-link from Hub straight to Diagnostic via `https://t.me/{bot}?start={patient_id}` — no re-registration.

### Phase 6 — Hospital floor + staff dashboard

**Goal**: give staff a live view of patients, queues, and departments.

| File | Role |
|---|---|
| `app/queue_store.py` | Department availability (open/closed/maintenance) + queue length tracking. |
| `app/templates/staff.html` | Server-rendered Jinja fallback for legacy browsers. |
| `app/main.py:/api/*` | JSON API for the React frontend. |
| `frontend/frontend/src/pages/Index.tsx` | React + Vite + shadcn/ui control center. |
| `dev.sh` | Single command that starts uvicorn :8000 + Vite :8080 together. |

Staff actions trigger live updates:
- Toggle ECG to `maintenance` → next patient finishing Blood gets rerouted automatically.
- Toggle X-Ray to `closed` → next patient who would advance to X-Ray gets a reserved slot.
- Mark a step done → bot pushes the patient to next step (with floor map).

### Phase 7 — Floor-map continuity

**Goal**: visual navigation. Words alone aren't enough for low-literacy patients.

- `scripts/generate_floor_maps.py` — Pillow generator that produces per-department PNGs (`blood.png`, `ecg.png`, etc.).
- `app/static/floor_maps/` — checked into the repo so the bot can `send_photo` them with each "Next: X" message.
- Floor map attached on first sequence lock and on every step transition.

### Phase 8 — Multilingual & voice

**Goal**: serve Telugu and Hindi speakers — many in our target hospitals don't read English fluently.

- Every `messages.json` key has en/hi/te translations.
- Test name aliases in `test_catalogue.json` accept `రక్త పరీక్ష`, `रक्त परीक्षण`, `blood test` interchangeably.
- `LLM_PROVIDER=anthropic` parses free-text Telugu/Hindi prescriptions; `LLM_PROVIDER=none` falls back to script heuristics.
- `/voice` toggles gTTS audio replies in the patient's language.

### Phase 9 — Production hardening (this session)

**Goal**: take the working prototype to "demo doesn't fall over."

- **Cached `Bot` pool** — was creating fresh `Bot` per webhook → cold-connect TimedOut. Now reuses one Bot per `bot_type` with warm httpx pool.
- **Tight HTTPX timeouts** (5s/8s) + single-retry helper `_retrying` — fail fast, recover quickly.
- **Atomic test toggle** with `SELECT ... FOR UPDATE` — concurrent button taps can't lose updates.
- **Stale connection detection** in pool — Neon kills idle conns; we now health-check on checkout and discard dead handles.
- **Toast feedback** via `answer_callback_query(text=...)` — instant visual confirmation independent of slow `edit_message_text`.
- **Frontend dedupe** by `patient_identifier` — staff dashboard shows one row per patient instead of one row per journey.

---

## Part 2 — Live System Architecture (data flow)

### High-level component map

```
┌─────────────────────────────────────────────────────────────────────┐
│                          PATIENT (Telegram)                         │
└─────────────────────────────────────────────────────────────────────┘
                  │ (HTTPS via ngrok tunnel)
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       FastAPI :8000                                 │
│                                                                     │
│   /telegram/webhook/{hub,registration,diagnostic}                   │
│                  │                                                  │
│                  ▼                                                  │
│       ┌───────────────────────┐                                     │
│       │   telegram_bot.py     │  cached Bot pool, retries, toast    │
│       │   process_update()    │                                     │
│       └──────────┬────────────┘                                     │
│                  │                                                  │
│                  ▼                                                  │
│       ┌───────────────────────┐                                     │
│       │   flow.py             │  3-bot dispatch, command parsing    │
│       │   handle_message()    │                                     │
│       └──────────┬────────────┘                                     │
│                  │                                                  │
│        ┌─────────┼──────────┬──────────┐                           │
│        ▼         ▼          ▼          ▼                            │
│   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐                       │
│   │sequence│ │reroute │ │journey │ │knowledge│                      │
│   │engine  │ │engine  │ │.py     │ │.py      │                      │
│   └───┬────┘ └───┬────┘ └───┬────┘ └────┬────┘                      │
│       │          │          │           │                           │
│       ▼          ▼          ▼           ▼                           │
│   ┌────────────────────┐  ┌────────────────────┐                    │
│   │ clinical_rules.json│  │  Postgres (Neon)   │                    │
│   │ test_catalogue.json│  │  patients,journeys │                    │
│   │ messages.json      │  │  sessions, etc.    │                    │
│   └────────────────────┘  └────────────────────┘                    │
│                                                                     │
│   /api/* (JSON)            /staff, /admin (Jinja)                   │
└─────────────────────────────────────────────────────────────────────┘
                  ▲                  ▲
                  │                  │
                  │                  │ (server-side render)
                  │                  │
┌─────────────────┴────────────┐    │
│   React + Vite :8080         │    │
│   (TanStack Query polls)     │    │
│   Staff control center       │    │
└──────────────────────────────┘    │
                                    │
┌───────────────────────────────────┘
│  Hospital staff (browser)
└──────────────
```

### End-to-end patient journey (sequence diagram)

```
Patient            Hub Bot           Registration Bot    Diagnostic Bot      Backend          Postgres
   │                  │                       │                  │                │                │
   │── /start ──────► │                       │                  │                │                │
   │                  │── handle_message ──────────────────────► │                │                │
   │                  │                       │                  │── get_patient_identifier ──────►│
   │                  │                       │                  │ ◄────── (None) ────────────────│
   │ ◄──"Tap Register Now"─┤                  │                  │                │                │
   │                                                                                                │
   │── /start (Reg Bot) ─────────────────────► │                                                    │
   │ ◄──"Welcome to City Hospital. /english /hindi /telugu"────  │                                  │
   │── /telugu ─────────────────────────────► │                  │                │                │
   │ ◄──"Enter your Patient ID"──────────── │                                                       │
   │── P-001 ───────────────────────────────► │                                                     │
   │                                          │── claim_patient_by_id ────────────────────────────►│
   │                                          │                  │                │── UPDATE patients SET telegram_chat_id ─►│
   │ ◄──"Welcome Ravi" + [Go to Tests button] ─┤                                                    │
   │                                                                                                │
   │── /start P-001 (Diagnostic) ──────────────────────────────► │                                  │
   │                                                             │── set_session(empty) ──────────►│
   │ ◄──"Welcome, Ravi. Patient ID P-001..." ─────────────────── │                                  │
   │ ◄──[Test menu: ⬜ Blood ⬜ ECG ⬜ X-Ray ⬜ Ultrasound + lang+confirm]─                            │
   │                                                                                                │
   │── tap ⬜ Blood ───────────────────────────────────────────► │                                   │
   │                                                             │── toggle_test_selection ───────►│
   │                                                             │   (SELECT ... FOR UPDATE)       │
   │ ◄──Toast: "✅ Blood Test added (1 selected)" (instant ~200ms)                                  │
   │ ◄──Edited menu: ✅ Blood, ⬜ others (~500ms)                                                    │
   │                                                                                                │
   │   [taps ECG, Ultrasound, X-Ray similarly]                                                      │
   │                                                                                                │
   │── tap Confirm Selection ──────────────────────────────────► │                                  │
   │                                                             │── start_journey() ─────────────►│
   │                                                             │   ├── sequence_engine.sequence_tests()
   │                                                             │   │   → [BLOOD, ECG, ULTRASOUND, XRAY]
   │                                                             │   └── INSERT journey + journey_steps
   │ ◄──"Sequence locked" + first step + 📷 blood.png ─────────                                    │
   │                                                                                                │
   │   [Patient walks to Blood Test, completes it. Staff marks /done via dashboard]                │
   │                                                                                                │
   │ ◄──"Blood Test done. Rest 5 min before ECG. Next: ECG, 2nd Floor + 📷 ecg.png" ◄── push from staff│
   │                                                                                                │
   │   [If ECG goes maintenance mid-journey, reroute_engine kicks in:]                              │
   │   reroute_engine.decide() → reorder to [Blood, Ultrasound, ECG, X-Ray]                         │
   │ ◄──"ECG maintenance. Reordered to Blood → Ultrasound → ECG → X-Ray" ─                          │
   │                                                                                                │
   │   [All four tests complete]                                                                    │
   │ ◄──"All four tests complete. Rate 1-5?" ─                                                      │
   │── "5 — staff was very helpful" ────────────────────────────►                                   │
   │                                                             │── feedback.record_patient_feedback ──►│
   │ ◄──"Thank you. Get well soon." ─                                                               │
```

### Test selection toggle (the multi-select hot path)

```
                                      ┌─────────────────────────────┐
Patient taps "⬜ Blood Test" ────────► │  Telegram /webhook/diagnostic│
                                      │  callback_query.data="select:BLOOD"
                                      └────────────────┬────────────┘
                                                       │
                                        ┌──────────────▼──────────────┐
                                        │ flow.py select: branch      │
                                        │  toggle_test_selection()    │
                                        └──────────────┬──────────────┘
                                                       │
                              ┌────────────────────────▼─────────────────────┐
                              │ Postgres (single transaction)                │
                              │  BEGIN                                       │
                              │  SELECT pending_data_json FROM sessions      │
                              │   WHERE chat_id = ? FOR UPDATE   ← row lock  │
                              │  -- Python: toggle BLOOD in/out              │
                              │  INSERT/UPDATE sessions ...                  │
                              │  COMMIT                                      │
                              └────────────────────────┬─────────────────────┘
                                                       │
                                                       ▼
                                              new selected list
                                                       │
                                        ┌──────────────▼──────────────┐
                                        │ _render_test_menu()         │
                                        │ Reply.toast = "✅ Blood Test │
                                        │   added (1 selected)"       │
                                        │ Reply.buttons = updated grid│
                                        └──────────────┬──────────────┘
                                                       │
                                ┌──────────────────────┴───────────────────┐
                                ▼                                          ▼
              answer_callback_query(text=toast)         edit_message_text(buttons=...)
              ~200ms via warm httpx pool               ~500ms-2s, retried once
                                │                                          │
                                ▼                                          ▼
                Toast pops up at top of patient's        Menu re-renders with ✅ Blood
                screen — instant feedback                 (may lag, but toast already confirmed)
```

### Reroute decision tree

```
                       Department becomes unavailable
                                    │
                                    ▼
                ┌───────────────────────────────────┐
                │ reroute_engine.decide_reroute()   │
                │  - sequence: [BLOOD,ECG,US,XRAY]  │
                │  - current_index: 1 (just done BLD)│
                │  - unavailable_test: ECG           │
                └───────────────────┬───────────────┘
                                    │
                ┌───────────────────▼───────────────────┐
                │ Is unavailable_test in must_be_last?  │
                └───────┬────────────────────────┬──────┘
                       YES                       NO
                        │                        │
                        ▼                        ▼
           ┌────────────────────┐   ┌────────────────────────────┐
           │ Action = reserve   │   │ Does the test have         │
           │  (X-Ray cannot     │   │ can_move_later=true?       │
           │   be moved later)  │   └─────┬──────────────┬──────┘
           │ Reserve slot at HH:MM    YES                NO
           │ Notify when open    │     │                  │
           └─────────────────────┘     ▼                  ▼
                              ┌────────────────┐  ┌─────────────────┐
                              │ Action = reorder│  │ Action = reserve│
                              │ Move ECG after │  │ (cannot defer,  │
                              │ Ultrasound:    │  │ must wait)      │
                              │ [BLD,US,ECG,XR]│  └─────────────────┘
                              └────────────────┘
```

---

## Part 3 — Key invariants (don't violate)

These are the rules the entire system depends on. Violating any breaks correctness:

1. **`must_be_last` is HARD** — X-Ray cannot move under any circumstance.
2. **`must_precede` is SOFT** — preferred ordering, but the Reroute Engine can relax it when `can_move_later=true`.
3. **Data handoffs are post-hoc** — ECG findings transfer to Ultrasound *after both complete*, regardless of order.
4. **Patient ID is hospital-issued** — the bot never invents a Patient ID. Reception issues it; the bot only claims existing ones.
5. **`telegram_chat_id` is permanent once claimed** — patients don't re-register on every visit.
6. **`flow.py` is stateless** — all state in Postgres. Webhook retries are idempotent.
7. **LLM is optional** — every Claude API call has a deterministic fallback path (`app.nlu`).

---

## Part 4 — How to extend

To add a new test (e.g. Dental X-Ray):

1. Add to `test_catalogue.json` — code, multilingual aliases, floor, room, directions, `average_minutes`.
2. Add to `clinical_rules.json:canonical_order` and `reroute_permissions`.
3. Add a floor map PNG to `app/static/floor_maps/` (run `generate_floor_maps.py`).
4. Add to `AVAILABLE_TESTS` in `app/flow.py`.
5. (Optional) Add `must_precede` rules if it has clinical ordering constraints.

To add a new bot type (e.g. Pharmacy bot):

1. Add `PHARMACY_BOT_TOKEN` and `PHARMACY_BOT_USERNAME` to `.env`.
2. Add a new branch in `flow.handle_message` for `bot_type == "pharmacy"`.
3. Add the webhook route in `app/main.py`: `/telegram/webhook/pharmacy`.
4. Register the bot in `configure_webhooks()` in `app/telegram_bot.py`.

To swap the LLM:

Set `LLM_PROVIDER=ollama|anthropic|none` in `.env`. All three return the same shape from `parse_test_request()` and `analyse_feedback()`.
