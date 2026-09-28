"""Server side scheduler.

Runs inside the web process by default (and can also run standalone through
``scheduler.py``). Every minute it checks whether the configured billing time
has been reached.

Missed runs are recovered: the last successfully billed period is persisted in
``scheduler_state.last_billed_period``. If the server was down at 17:00 the
next start (or the next tick) notices that today is still unbilled and catches
up, one period at a time.

A database uniqueness constraint plus a per-period ``billing_runs`` record make
double billing impossible, even if two processes tick simultaneously.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .models import SchedulerState, utcnow
from .services.billing import (
    BillingResult,
    get_tz,
    local_now,
    next_run_at,
    periods_due,
    run_daily_billing,
)
from .settings_service import Settings

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()
_app = None

TICK_SECONDS = 60
BILLING_LOCK_KEY = "daily_billing"


def get_app():
    return _app


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------
def _state(db) -> SchedulerState:
    state = db.get(SchedulerState, 1)
    if state is None:
        state = SchedulerState(id=1)
        db.add(state)
        db.flush()
    return state


def scheduler_snapshot(db) -> dict:
    state = db.get(SchedulerState, 1)
    settings = Settings(db)
    tz = get_tz(settings.timezone)
    now = local_now(tz)
    return {
        "running": bool(_scheduler and _scheduler.running),
        "last_success_at": state.last_success_at if state else None,
        "last_error": state.last_error if state else None,
        "consecutive_failures": state.consecutive_failures if state else 0,
        "last_billed_period": state.last_billed_period if state else None,
        "last_tick_at": state.last_tick_at if state else None,
        "heartbeat": state.heartbeat if state else None,
        "now": now,
        "next_run_at": next_run_at(settings, now) if settings.auto_billing_enabled else None,
        "timezone": settings.timezone,
        "billing_time": settings.auto_billing_time,
        "enabled": settings.auto_billing_enabled,
        "catchup_enabled": settings.auto_catchup_enabled,
    }


# ---------------------------------------------------------------------------
# the tick
# ---------------------------------------------------------------------------
def _acquire_billing_lock(db) -> bool:
    """Cooperative, database backed lock so only one process bills at a time."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        state = _state(db)
        now = utcnow()
        if state.heartbeat and (now - state.heartbeat) < timedelta(minutes=5):
            # another process is currently working on it
            return False
        state.heartbeat = now
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        db.rollback()
        _lock.release()
        return False


def _release_billing_lock() -> None:
    if _lock.locked():
        _lock.release()


def tick(app=None) -> BillingResult | None:
    """Evaluate whether billing is due. Safe to call every minute."""
    app = app or _app
    if app is None:
        return None
    from .db import get_session

    db = get_session()
    try:
        settings = Settings(db)
        if not settings.auto_billing_enabled:
            return None

        state = _state(db)
        state.last_tick_at = utcnow()
        db.commit()

        due = _periods_to_bill(settings, state)
        if not due:
            return None

        if not _acquire_billing_lock(db):
            log.info("Billing-Lock nicht erhalten - ueberspringe diesen Tick")
            return None
        try:
            results: list[BillingResult] = []
            for period, is_catchup in due:
                result = _run_period(app, db, settings, period, is_catchup)
                results.append(result)
                # only advance the watermark if the period really completed
                if not result.fatal_error:
                    state = _state(db)
                    state.last_billed_period = period
                    state.last_success_at = utcnow()
                    state.consecutive_failures = 0
                    state.last_error = None
                    db.commit()
                else:
                    state = _state(db)
                    state.last_error = result.fatal_error[:500]
                    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
                    db.commit()
            return results[-1] if results else None
        finally:
            _release_billing_lock()
    except Exception as exc:  # noqa: BLE001 - scheduler must survive anything
        log.exception("Scheduler-Tick fehlgeschlagen")
        try:
            db.rollback()
            state = _state(db)
            state.last_error = f"{type(exc).__name__}: {exc}"[:500]
            state.consecutive_failures = int(state.consecutive_failures or 0) + 1
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return None
    finally:
        from .db import remove_session

        remove_session()


def _periods_to_bill(settings: Settings, state: SchedulerState) -> list[tuple[date, bool]]:
    tz = get_tz(settings.timezone)
    now = local_now(tz)
    today = now.date()
    hour, minute = (
        int(part) for part in (settings.auto_billing_time or "17:00").split(":")
    )
    scheduled_today = datetime.combine(today, dtime(hour, minute), tzinfo=tz)

    last_billed: date | None = state.last_billed_period if state else None

    if settings.auto_catchup_enabled:
        due = periods_due(settings, last_billed, now)
        if not due:
            return []
        # only catch up to the most recent billable day, one run per tick
        return [(due[0], last_billed is not None or due[0] != today)]

    # catch-up disabled: run today once, after the configured time
    if last_billed is not None and last_billed >= today:
        return []
    if now < scheduled_today:
        return []
    if last_billed is not None and last_billed == today:
        return []
    return [(today, False)]


def _run_period(app, db, settings: Settings, period: date, is_catchup: bool) -> BillingResult:

    log.info(
        "Starte Tagesabrechnung fuer %s (%s)",
        period.isoformat(),
        "Nachholung" if is_catchup else "planmaessig",
    )
    with app.app_context():
        result = run_daily_billing(
            db,
            settings,
            period,
            trigger="automatic",
            is_catchup=is_catchup,
            retry_attempts=2,
            retry_delay=2.0,
        )
    if result.emails_sent:
        log.info(
            "Tagesabrechnung %s fertig: %s E-Mails versendet, %s fehlgeschlagen",
            period.isoformat(),
            result.emails_sent,
            result.emails_failed,
        )
    return result


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def _job(app) -> None:
    with app.app_context():
        tick(app)


def init_scheduler(app, *, start: bool = True) -> BackgroundScheduler:
    global _scheduler, _app
    _app = app

    if _scheduler is not None:
        return _scheduler

    with app.app_context():
        settings = Settings(app.extensions["db_session"] or _session())
        tz_name = settings.timezone

    scheduler = BackgroundScheduler(
        timezone=tz_name,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        },
    )
    scheduler.add_job(
        _job,
        trigger=IntervalTrigger(seconds=TICK_SECONDS),
        args=[app],
        id="billing_tick",
        name="Tagesabrechnung pruefen",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    if start:
        scheduler.start()
        log.info("Scheduler gestartet (Intervall %ss, Zeitzone %s)", TICK_SECONDS, tz_name)
    _scheduler = scheduler
    return scheduler


def _session():
    from .db import get_session

    return get_session()


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None
        log.info("Scheduler gestoppt")


def reschedule(app) -> None:
    """Re-read timezone / interval after settings changed."""
    global _scheduler, _app
    if _scheduler is None:
        return

    _app = app
    with app.app_context():
        db = app.extensions.get("db_session") or _session()
        settings = Settings(db)
        tz_name = settings.timezone
    try:
        _scheduler.configure(timezone=tz_name)
    except Exception:  # noqa: BLE001 - timezone change is best effort
        log.warning("Scheduler-Zeitzone konnte nicht geaendert werden")
