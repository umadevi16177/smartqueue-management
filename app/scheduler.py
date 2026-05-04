"""Background scheduler for night-before reminders and slot-ready alerts.

Wraps APScheduler's AsyncIOScheduler. Falls back to a no-op stub if the
package isn't installed so the rest of the system still boots.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

log = logging.getLogger(__name__)


class _NoopScheduler:
    """Used when APScheduler isn't installed — never schedules anything."""

    def start(self) -> None:
        log.warning("APScheduler not installed — scheduled jobs will not run.")

    def shutdown(self, wait: bool = False) -> None:  # noqa: ARG002
        return None

    def add_job(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        return None


def _build_scheduler():
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        return AsyncIOScheduler()
    except ImportError:
        return _NoopScheduler()


scheduler = _build_scheduler()


def start() -> None:
    if scheduler.__class__.__name__ != "_NoopScheduler":
        scheduler.start()


def shutdown() -> None:
    scheduler.shutdown(wait=False)


def schedule_fasting_reminder(chat_id: int, journey_id: int) -> None:
    """Schedule the night-before fasting reminder.

    Heuristic: if the patient registers before 18:00 today, fire at 19:00
    today; otherwise fire at 19:00 tomorrow. (Hospital appointments here
    are assumed next-day; a real version would key off a scheduled time.)
    """
    now = datetime.now()
    target = datetime.combine(now.date(), time(hour=19, minute=0))
    if now >= target - timedelta(minutes=5):
        target += timedelta(days=1)
    _add_one_shot(
        target,
        _send_fasting_reminder,
        kwargs={"chat_id": chat_id, "journey_id": journey_id},
        job_id=f"fasting:{journey_id}",
    )


def schedule_slot_alert(
    chat_id: int, journey_id: int, test_code: str, slot_time: datetime
) -> None:
    _add_one_shot(
        slot_time,
        _send_slot_alert,
        kwargs={"chat_id": chat_id, "journey_id": journey_id, "test_code": test_code},
        job_id=f"slot:{journey_id}:{test_code}",
    )


def _add_one_shot(when: datetime, fn, *, kwargs: dict, job_id: str) -> None:
    if when < datetime.now():
        log.info("Skipping past-due job %s (%s)", job_id, when)
        return
    try:
        scheduler.add_job(fn, "date", run_date=when, kwargs=kwargs, id=job_id, replace_existing=True)
        log.info("Scheduled %s at %s", job_id, when.isoformat(timespec="minutes"))
    except Exception:
        log.exception("Failed to schedule %s", job_id)


async def _send_fasting_reminder(chat_id: int, journey_id: int) -> None:  # noqa: ARG001
    from app.journey import get_journey, get_patient_language
    from app.knowledge import render_message
    from app.telegram_bot import push_alert

    j = get_journey(journey_id)
    if not j or j["status"] in ("done", "cancelled"):
        return
    if "BLOOD" not in [s["test_code"] for s in j["steps"]]:
        return
    lang = get_patient_language(chat_id) or "en"
    await push_alert(chat_id, render_message("fasting_reminder", lang))


async def _send_slot_alert(chat_id: int, journey_id: int, test_code: str) -> None:  # noqa: ARG001
    from app.journey import get_journey, get_patient_language
    from app.knowledge import directions_for, display_name, render_message
    from app.telegram_bot import push_alert

    j = get_journey(journey_id)
    if not j or j["status"] in ("done", "cancelled"):
        return
    lang = get_patient_language(chat_id) or "en"
    floor, room, _ = directions_for(test_code, lang)
    await push_alert(
        chat_id,
        render_message(
            "slot_now_open",
            lang,
            test=display_name(test_code, lang),
            floor=floor,
            room=room,
        ),
    )
