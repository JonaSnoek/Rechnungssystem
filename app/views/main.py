"""Dashboard and index routes."""

from __future__ import annotations

import logging

from flask import Blueprint, g, redirect, render_template, request, url_for
from sqlalchemy import select

from ..models import BillingRun, Invoice
from ..security import login_required, setup_required
from ..services.billing import get_tz, local_now
from ..services.stats import (
    consumption_trend,
    dashboard_stats,
    open_persons_with_balance,
    recent_activity,
)
from ..settings_service import SETUP_COMPLETED, Settings

log = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    settings = Settings(g.db)
    if not settings.get_bool(SETUP_COMPLETED, False):
        return redirect(url_for("setup.index"))
    if getattr(g, "admin", None) is None:
        return redirect(url_for("auth.login", next=request.full_path))
    return redirect(url_for("main.dashboard"))


@bp.route("/dashboard")
@setup_required
@login_required
def dashboard():
    db = g.db
    settings = Settings(db)
    stats = dashboard_stats(db, settings)
    activity = recent_activity(db, settings, limit=12)
    open_list = open_persons_with_balance(db)[:8]
    trend = consumption_trend(db, settings, days=14)
    tz = get_tz(settings.timezone)
    now = local_now(tz)

    recent_runs = list(
        db.execute(select(BillingRun).order_by(BillingRun.id.desc()).limit(5)).scalars().all()
    )
    recent_invoices = list(
        db.execute(select(Invoice).order_by(Invoice.id.desc()).limit(6)).scalars().all()
    )

    return render_template(
        "dashboard.html",
        stats=stats,
        activity=activity,
        open_persons=open_list,
        trend=trend,
        recent_runs=recent_runs,
        recent_invoices=recent_invoices,
        now=now,
        currency=settings.currency,
        max_trend=max([t["cents"] for t in trend], default=0) or 1,
    )
