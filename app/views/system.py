"""System: backup, restore, exports, scheduler status, audit and mail log."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, select

from .. import migrations
from ..models import AuditLog, BillingRun, EmailLog
from ..scheduler import scheduler_snapshot
from ..security import audit, login_required, setup_required, validate_csrf
from ..services import backup as backup_service
from ..services.export import consumptions_csv
from ..settings_service import Settings

log = logging.getLogger(__name__)

bp = Blueprint("system", __name__, url_prefix="/system")


def _backup_dir() -> Path:
    from ..config import INSTANCE_DIR

    return Path(current_app.config.get("BACKUP_DIR") or (INSTANCE_DIR / "backups"))


@bp.route("/")
@setup_required
@login_required
def index():
    db = g.db
    settings = Settings(db)
    engine_info = _engine_info()
    migration_status = migrations.status(current_app.extensions["migrations_dir"])
    scheduler = scheduler_snapshot(db)
    return render_template(
        "system/index.html",
        settings=settings,
        engine=engine_info,
        migrations=migration_status,
        migration_version=migrations.current_version(),
        scheduler=scheduler,
        backup_dir=str(_backup_dir()),
        app_version=current_app.config.get("APP_VERSION"),
        db_path=engine_info.get("path", ""),
    )


def _engine_info() -> dict:
    from ..db import get_engine

    engine = get_engine()
    url = engine.url
    return {
        "dialect": engine.dialect.name,
        "driver": engine.dialect.driver,
        "host": url.host or "-",
        "port": url.port or "-",
        "database": url.database or "-",
        "path": url.database or "" if engine.dialect.name == "sqlite" else "",
    }


@bp.route("/backup", methods=["POST"])
@setup_required
@login_required
def create_backup():
    validate_csrf()
    db = g.db
    settings = Settings(db)
    kind = request.form.get("format") or "json"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        if kind == "sqlite":
            target = backup_service.backup_sqlite_file(
                _backup_dir() / f"sqlite-{stamp}.db"
            )
            audit(db, "system.backup", detail=str(target.name))
            flash(f"Rohes SQLite-Backup erstellt: {target.name}", "success")
            return redirect(url_for("system.backups"))

        if kind == "zip":
            data = backup_service.backup_zip(db, settings)
            audit(db, "system.backup", detail="zip")
            return Response(
                data,
                mimetype="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="backup-{stamp}.zip"'
                },
            )

        target = backup_service.write_backup_to_disk(db, settings, _backup_dir())
        audit(db, "system.backup", detail=target.name)
        flash(f"JSON-Backup erstellt: {target.name}", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Backup fehlgeschlagen")
        flash(f"Backup fehlgeschlagen: {exc}", "error")
    return redirect(url_for("system.backups"))


@bp.route("/backup/download")
@setup_required
@login_required
def download_backup():
    db = g.db
    settings = Settings(db)
    data = backup_service.backup_to_json(db)
    audit(db, "system.backup_download")
    from ..services.billing import get_tz, local_now

    stamp = local_now(get_tz(settings.timezone)).strftime("%Y%m%d-%H%M%S")
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="backup-{stamp}.json"'},
    )


@bp.route("/backups")
@setup_required
@login_required
def backups():
    directory = _backup_dir()
    files = []
    if directory.exists():
        for path in sorted(directory.iterdir(), reverse=True):
            if path.is_file():
                stat = path.stat()
                files.append(
                    {
                        "name": path.name,
                        "size": stat.st_size,
                        "size_human": _human_size(stat.st_size),
                        "modified": stat.st_mtime,
                    }
                )
    return render_template("system/backups.html", files=files, backup_dir=str(directory))


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


@bp.route("/backup/restore", methods=["GET", "POST"])
@setup_required
@login_required
def restore():
    db = g.db
    settings = Settings(db)
    preview = None
    error = ""
    if request.method == "POST":
        validate_csrf()
        if request.form.get("confirm") != "JA":
            flash("Bitte bestaetige den Vorgang durch Eingabe von JA.", "error")
            return redirect(url_for("system.restore"))
        payload = None
        try:
            if request.files.get("backup_file"):
                payload = request.files["backup_file"].read().decode("utf-8")
            elif request.form.get("payload"):
                payload = request.form["payload"]
            elif request.form.get("file_name"):
                path = _backup_dir() / Path(request.form["file_name"]).name
                if not path.exists():
                    raise ValueError("Datei nicht gefunden")
                payload = path.read_text(encoding="utf-8")
            if not payload:
                raise ValueError("Kein Backup angegeben")
            counts = backup_service.restore_from_json(
                db, payload, mode=request.form.get("mode") or "replace"
            )
            audit(db, "system.restore", detail=json.dumps(counts, ensure_ascii=False))
            flash(f"Wiederherstellung abgeschlossen: {counts}", "success")
            return redirect(url_for("system.index"))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            flash(f"Wiederherstellung fehlgeschlagen: {error}", "error")

    if request.method == "GET" and request.args.get("file"):
        try:
            path = _backup_dir() / Path(request.args["file"]).name
            preview = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            error = f"Vorschau nicht moeglich: {exc}"
    stored_files = [
        p.name
        for p in sorted(_backup_dir().glob("*.json"), reverse=True)
        if p.is_file()
    ]
    return render_template(
        "system/restore.html",
        error=error,
        preview=preview,
        settings=settings,
        stored_files=stored_files,
    )


@bp.route("/export/verzehr.csv")
@setup_required
@login_required
def export_consumptions():
    db = g.db
    settings = Settings(db)
    from ..validators import Validator

    v = Validator()
    start = v.date("start", request.args.get("start"))
    end = v.date("end", request.args.get("end"))
    person_id = request.args.get("person_id", type=int)
    if v.errors:
        flash(v.first_error(), "error")
        return redirect(url_for("system.index"))
    data = consumptions_csv(db, settings, start=start, end=end, person_id=person_id)
    audit(db, "system.export", detail="verzehr.csv")
    from ..services.billing import get_tz, local_now

    stamp = local_now(get_tz(settings.timezone)).strftime("%Y%m%d")
    return Response(
        "\ufeff" + data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="verzehr-{stamp}.csv"'},
    )


@bp.route("/log")
@setup_required
@login_required
def log_view():
    db = g.db
    page = max(1, request.args.get("page", type=int) or 1)
    per_page = 100
    kind = request.args.get("kind") or "audit"
    if kind not in {"audit", "email", "billing"}:
        kind = "audit"

    total = 0
    rows = []
    if kind == "audit":
        total = int(
            db.execute(select(func.count(AuditLog.id))).scalar_one() or 0
        )
        rows = list(
            db.execute(
                select(AuditLog)
                .order_by(AuditLog.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            .scalars()
            .all()
        )
    elif kind == "email":
        total = int(db.execute(select(func.count(EmailLog.id))).scalar_one() or 0)
        rows = list(
            db.execute(
                select(EmailLog)
                .order_by(EmailLog.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            .scalars()
            .all()
        )
    else:
        total = int(db.execute(select(func.count(BillingRun.id))).scalar_one() or 0)
        rows = list(
            db.execute(
                select(BillingRun)
                .order_by(BillingRun.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
            .scalars()
            .all()
        )

    return render_template(
        "system/log.html",
        kind=kind,
        rows=rows,
        total=total,
        page=page,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@bp.route("/scheduler")
@setup_required
@login_required
def scheduler_view():
    db = g.db
    return render_template("system/scheduler.html", scheduler=scheduler_snapshot(db))


@bp.route("/scheduler/tick", methods=["POST"])
@setup_required
@login_required
def scheduler_tick():
    """Force one scheduler evaluation (useful for testing the catch-up)."""
    validate_csrf()
    from ..scheduler import tick

    app = current_app._get_current_object()
    result = tick(app)
    audit(db, "system.scheduler_tick", detail="manuell ausgeloest")
    if result is None:
        flash("Aktuell ist keine Abrechnung faellig.", "info")
    elif result.emails_sent:
        flash(
            f"Scheduler-Tick: {result.emails_sent} Abrechnung(en) versendet.",
            "success",
        )
    else:
        flash(f"Scheduler-Tick: keine E-Mail versendet ({result.fatal_error or 'nichts faellig'}).", "info")
    return redirect(url_for("system.scheduler_view"))


@bp.route("/scheduler/aktivieren", methods=["POST"])
@setup_required
@login_required
def scheduler_toggle():
    validate_csrf()
    db = g.db
    settings = Settings(db)
    from ..settings_service import AUTO_BILLING_ENABLED

    enabled = not settings.auto_billing_enabled
    settings.set(AUTO_BILLING_ENABLED, "true" if enabled else "false")
    db.commit()
    audit(db, "system.scheduler_toggle", detail="aktiv" if enabled else "inaktiv")
    flash(f"Automatische Abrechnung ist jetzt {'aktiv' if enabled else 'inaktiv'}.", "success")
    return redirect(url_for("system.scheduler_view"))
