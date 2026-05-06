"""FastAPI entrypoint.

Exposes:
  POST /telegram/webhook  — Telegram updates
  GET  /staff             — Staff Queue Dashboard
  POST /staff/department/{code} — update queue/availability
  POST /staff/done/{chat_id}     — staff marks current step done for a patient
  GET  /admin             — Hospital Admin Review Panel
  POST /admin/login       — minimal password gate
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.config import settings
from app.db import init_db
from app.feedback import feedback_metrics, list_feedback
from app.flow import handle_message
from app.journey import (
    get_active_journey,
    get_or_create_patient,
    journey_metrics,
    latest_findings_for,
    list_active_journeys,
    list_unclaimed_patients,
    reconcile_department_counters,
    record_findings,
    staff_register_patient,
)
from app.knowledge import clinical_rules, save_clinical_rules
from app.queue_store import ensure_seeded, list_departments, update_department
from app import scheduler as scheduler_mod
from app.telegram_bot import configure_webhooks, process_update, push_replies

log = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_seeded()
    # Re-ground each department's queue_length / estimated_wait_minutes against
    # the actual journey_steps rows. Self-heals any drift that accumulated
    # before the per-step auto-tracking was wired in.
    reconcile_department_counters()
    scheduler_mod.start()
    try:
        results = await configure_webhooks()
        for url in results:
            log.info("Webhook configured: %s", url)
    except Exception:
        log.exception("Webhook configuration failed (continuing without webhooks)")
    yield
    scheduler_mod.shutdown()


app = FastAPI(title="Smart Hospital Diagnostic System", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- Telegram webhook ----------

@app.post("/telegram/webhook/registration")
async def telegram_webhook_registration(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    await process_update(payload, bot_type="registration")
    return {"ok": "true"}


@app.post("/telegram/webhook/diagnostic")
async def telegram_webhook_diagnostic(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    await process_update(payload, bot_type="diagnostic")
    return {"ok": "true"}


@app.post("/telegram/webhook/hub")
async def telegram_webhook_hub(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    await process_update(payload, bot_type="hub")
    return {"ok": "true"}


# ---------- Local debug — simulate a Telegram message ----------

@app.post("/debug/message")
async def debug_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Send a fake message to test the flow without Telegram.

    Pure dispatch — does NOT pre-create the patient row, since that would
    overwrite an explicitly-set language (e.g. /telugu) on every request.
    handle_message creates the row when needed.
    """
    chat_id = int(payload.get("chat_id", 1))
    name = payload.get("name")
    text = payload.get("text", "")
    replies = handle_message(chat_id=chat_id, sender_name=name, text=text)
    return {
        "chat_id": chat_id,
        "replies": [{"text": r.text, "photo": r.photo} for r in replies],
    }


# ---------- Staff Queue Dashboard ----------

@app.get("/staff", response_class=HTMLResponse)
def staff_dashboard(request: Request) -> Any:
    departments = list_departments()
    latest_ecg = latest_findings_for("ECG")
    return templates.TemplateResponse(
        "staff.html",
        {
            "request": request,
            "departments": departments,
            "hospital": settings.hospital_name,
            "latest_ecg_findings": latest_ecg,
        },
    )


@app.post("/staff/findings/{chat_id}")
async def staff_record_findings(
    chat_id: int,
    test_code: str = Form(...),
    findings: str = Form(...),
):
    journey = get_active_journey(chat_id)
    if not journey:
        raise HTTPException(status_code=404, detail="No active journey for this chat_id")
    record_findings(journey["id"], test_code.upper(), findings)
    return RedirectResponse(url="/staff", status_code=303)


@app.post("/staff/department/{code}")
async def staff_update_department(
    code: str,
    queue_length: Optional[int] = Form(default=None),
    estimated_wait_minutes: Optional[int] = Form(default=None),
    availability: Optional[str] = Form(default=None),
):
    update_department(
        code=code,
        queue_length=queue_length,
        estimated_wait_minutes=estimated_wait_minutes,
        availability=availability,
    )
    return RedirectResponse(url="/staff", status_code=303)


@app.post("/staff/done/{chat_id}")
async def staff_mark_done(chat_id: int):
    """Lab tech marks the patient's current step done — same as patient saying /done."""
    journey = get_active_journey(chat_id)
    if not journey:
        raise HTTPException(status_code=404, detail="No active journey")
    replies = handle_message(chat_id=chat_id, sender_name=None, text="/done")
    await push_replies(chat_id, replies)
    return RedirectResponse(url="/staff", status_code=303)


# ---------- Hospital Admin Review Panel ----------

@app.get("/admin", response_class=HTMLResponse)
def admin_panel(request: Request, password: Optional[str] = None) -> Any:
    if password != settings.admin_password:
        return templates.TemplateResponse(
            "admin_login.html", {"request": request, "hospital": settings.hospital_name}
        )
    feedback = list_feedback(limit=100)
    fb_metrics = feedback_metrics()
    j_metrics = journey_metrics()
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "feedback": feedback,
            "fb_metrics": fb_metrics,
            "j_metrics": j_metrics,
            "hospital": settings.hospital_name,
            "password": password,
        },
    )


@app.get("/admin/rules", response_class=HTMLResponse)
def admin_rules(request: Request, password: Optional[str] = None) -> Any:
    if password != settings.admin_password:
        return templates.TemplateResponse(
            "admin_login.html", {"request": request, "hospital": settings.hospital_name}
        )
    import json as _json

    rules_text = _json.dumps(clinical_rules(), indent=2, ensure_ascii=False)
    return templates.TemplateResponse(
        "admin_rules.html",
        {
            "request": request,
            "hospital": settings.hospital_name,
            "rules_text": rules_text,
            "password": password,
            "errors": [],
            "saved": False,
        },
    )


@app.post("/admin/rules", response_class=HTMLResponse)
async def admin_rules_save(
    request: Request,
    password: str = Form(...),
    rules_text: str = Form(...),
):
    import json as _json

    if password != settings.admin_password:
        raise HTTPException(status_code=403, detail="Invalid admin password")
    errors: list[str] = []
    try:
        data = _json.loads(rules_text)
    except _json.JSONDecodeError as e:
        errors.append(f"JSON parse error: {e.msg} at line {e.lineno}, col {e.colno}")
        return templates.TemplateResponse(
            "admin_rules.html",
            {
                "request": request,
                "hospital": settings.hospital_name,
                "rules_text": rules_text,
                "password": password,
                "errors": errors,
                "saved": False,
            },
        )
    try:
        save_clinical_rules(data)
    except ValueError as e:
        errors = str(e).splitlines()[1:] or [str(e)]
        errors = [line.lstrip("- ").strip() for line in errors if line.strip()]
        return templates.TemplateResponse(
            "admin_rules.html",
            {
                "request": request,
                "hospital": settings.hospital_name,
                "rules_text": rules_text,
                "password": password,
                "errors": errors,
                "saved": False,
            },
        )
    saved_text = _json.dumps(clinical_rules(), indent=2, ensure_ascii=False)
    return templates.TemplateResponse(
        "admin_rules.html",
        {
            "request": request,
            "hospital": settings.hospital_name,
            "rules_text": saved_text,
            "password": password,
            "errors": [],
            "saved": True,
        },
    )


# ---------- JSON API for the React frontend ----------

class DepartmentDTO(BaseModel):
    code: str
    queue_length: int
    estimated_wait_minutes: int
    availability: str  # open | maintenance | closed
    updated_at: str


class DepartmentPatch(BaseModel):
    queue_length: Optional[int] = None
    estimated_wait_minutes: Optional[int] = None
    availability: Optional[str] = Field(default=None, pattern="^(open|maintenance|closed)$")


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok", "hospital": settings.hospital_name}


@app.get("/api/departments", response_model=list[DepartmentDTO])
def api_departments() -> list[DepartmentDTO]:
    rows = list_departments()
    return [DepartmentDTO(**r) for r in rows]


@app.patch("/api/departments/{code}", response_model=DepartmentDTO)
def api_update_department(code: str, patch: DepartmentPatch) -> DepartmentDTO:
    code = code.upper()
    updated = update_department(
        code=code,
        queue_length=patch.queue_length,
        estimated_wait_minutes=patch.estimated_wait_minutes,
        availability=patch.availability,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Unknown department {code}")
    return DepartmentDTO(**updated)


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    return {
        "journey": journey_metrics(),
        "feedback": feedback_metrics(),
    }


@app.get("/api/journeys/active")
def api_active_journeys() -> list[dict[str, Any]]:
    """Active patients for the staff dashboard — patient ID, name, current step."""
    return list_active_journeys()


@app.post("/api/journeys/{journey_id}/complete-current")
async def api_complete_current_step(journey_id: int) -> dict[str, Any]:
    """Staff click-to-complete on a queue row. Mirrors what the patient typing
    /done would do: marks the current step completed, advances the journey,
    and pushes the next-step message to the patient via Telegram. Returns
    the updated journey + the test that was just completed."""
    from app.journey import get_journey

    try:
        journey = get_journey(journey_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Journey {journey_id} not found")
    if journey["status"] in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Journey is {journey['status']}")

    # Find the chat_id for this journey's patient.
    from app.db import get_conn

    with get_conn() as conn:
        row = conn.execute(
            "SELECT p.telegram_chat_id FROM journeys j "
            "JOIN patients p ON p.id = j.patient_id WHERE j.id = %s",
            (journey_id,),
        ).fetchone()
    if not row or row["telegram_chat_id"] is None:
        raise HTTPException(
            status_code=409,
            detail="Journey is not bound to a Telegram chat — patient hasn't claimed their ID yet.",
        )
    chat_id = int(row["telegram_chat_id"])

    # Identify the step that's about to be completed before advancing, so we
    # can return it to the caller for UI feedback.
    completed_test: Optional[str] = None
    for s in journey["steps"]:
        if s["department_status"] != "completed":
            completed_test = s["test_code"]
            break

    replies = handle_message(chat_id=chat_id, sender_name=None, text="/done")
    try:
        await push_replies(chat_id, replies)
    except Exception:
        log.exception("push_replies failed (Telegram outbound) — staff action still succeeded")

    # Re-read the journey post-advance and surface the next step in the same
    # shape the frontend gets from /api/journeys/active (which has computed
    # current_test / current_token fields).
    from app.journey import get_journey as _get_j  # avoid shadowing

    j = _get_j(journey_id)
    next_test: Optional[str] = None
    next_token: Optional[str] = None
    for s in j["steps"]:
        if s["department_status"] != "completed":
            next_test = s["test_code"]
            next_token = s["queue_token"]
            break

    return {
        "journey_id": journey_id,
        "completed_test": completed_test,
        "journey": {**j, "current_test": next_test, "current_token": next_token},
    }


class RegisterPatientPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    patient_id: Optional[str] = Field(default=None, max_length=20)


@app.post("/api/patients")
def api_register_patient(payload: RegisterPatientPayload) -> dict[str, Any]:
    """Staff-driven patient registration.

    - No patient_id: issue a new permanent P-NNN ID + new FCFS queue number.
    - With patient_id: reuse the existing permanent ID, issue a fresh queue
      number for this visit. 404 if the ID is unknown.
    """
    try:
        return staff_register_patient(payload.name, payload.patient_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/patients/unclaimed")
def api_unclaimed_patients() -> list[dict[str, Any]]:
    """Patients staff has registered but who haven't messaged the bot yet."""
    return list_unclaimed_patients()


# ---------- Frontend serving (registered LAST so it doesn't shadow API routes) ----------
# StaticFiles mounted at "/" is a catch-all — every previously-registered API route
# wins because FastAPI checks routes in registration order.

import os as _os

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        reg_bot_username = _os.getenv("REGISTRATION_BOT_USERNAME", "SmartQueueRegistrationBot")
        return HTMLResponse(
            f"""
            <html>
            <head>
                <title>Smart Hospital Registration</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background-color: #f0f2f5; }}
                    .card {{ background: white; padding: 40px; border-radius: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); text-align: center; max-width: 400px; width: 90%; }}
                    h2 {{ color: #1a73e8; margin-top: 0; }}
                    p {{ color: #5f6368; line-height: 1.5; }}
                    .btn {{ display: inline-block; background-color: #1a73e8; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: bold; margin-top: 20px; transition: background-color 0.2s; }}
                    .btn:hover {{ background-color: #1557b0; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <h2>Smart Hospital</h2>
                    <p>Welcome to City General Hospital. Please register your Patient ID to begin your diagnostic journey.</p>
                    <a href="https://t.me/{reg_bot_username}" class="btn">Register Now</a>
                    <div style="margin-top: 30px; border-top: 1px solid #eee; padding-top: 20px; font-size: 12px; color: #999;">
                        <a href="/staff" style="color: #999; margin-right: 15px;">Staff Dashboard</a>
                        <a href="/admin" style="color: #999;">Admin Panel</a>
                    </div>
                </div>
            </body>
            </html>
            """
        )
