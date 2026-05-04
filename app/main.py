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
    record_findings,
)
from app.knowledge import clinical_rules, save_clinical_rules
from app.queue_store import ensure_seeded, list_departments, update_department
from app import scheduler as scheduler_mod
from app.telegram_bot import configure_webhook, process_update, push_alert

log = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_seeded()
    scheduler_mod.start()
    try:
        result = await configure_webhook()
        log.info("Webhook configured: %s", result)
    except Exception:
        log.exception("Webhook configuration failed (continuing without webhook)")
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


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(
        f"""
        <html><body style='font-family: monospace; padding: 40px;'>
        <h2>Smart Hospital Diagnostic System</h2>
        <p>{settings.hospital_name}</p>
        <ul>
          <li><a href='/staff'>Staff Queue Dashboard</a></li>
          <li><a href='/admin'>Hospital Admin Review Panel</a></li>
        </ul>
        </body></html>
        """
    )


# ---------- Telegram webhook ----------

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    await process_update(payload)
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
    for r in replies:
        if r and r.text:
            await push_alert(chat_id, r.text)
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
