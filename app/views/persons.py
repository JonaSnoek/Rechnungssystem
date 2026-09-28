"""Person management."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy import asc, func, or_, select

from ..models import (
    Consumption,
    ConsumptionStatus,
    Invoice,
    InvoiceStatus,
    Person,
)
from ..security import audit, login_required, setup_required, validate_csrf
from ..services.billing import get_tz, open_balance_cents
from ..services.export import consumptions_csv
from ..services.stats import person_history
from ..settings_service import Settings
from ..validators import Validator

log = logging.getLogger(__name__)

bp = Blueprint("persons", __name__, url_prefix="/personen")


@bp.route("/")
@setup_required
@login_required
def index():
    db = g.db
    settings = Settings(db)
    query = (request.args.get("q") or "").strip()
    show_inactive = request.args.get("inactive") == "1"

    stmt = select(Person)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Person.first_name.ilike(like),
                Person.last_name.ilike(like),
                Person.email.ilike(like),
                func.lower(Person.first_name + " " + Person.last_name).like(like),
            )
        )
    if not show_inactive:
        stmt = stmt.where(Person.is_active.is_(True))
    stmt = stmt.order_by(asc(Person.last_name), asc(Person.first_name))
    people = list(db.execute(stmt).scalars().all())

    balances: dict[int, int] = {}
    open_counts: dict[int, int] = {}
    if people:
        ids = [p.id for p in people]
        balances = {
            int(pid): int(total or 0)
            for pid, total in db.execute(
                select(Consumption.person_id, func.sum(Consumption.total_cents))
                .where(
                    Consumption.person_id.in_(ids),
                    Consumption.status == ConsumptionStatus.OFFEN,
                )
                .group_by(Consumption.person_id)
            ).all()
        }
        open_counts = {
            int(pid): int(cnt or 0)
            for pid, cnt in db.execute(
                select(Consumption.person_id, func.count(Consumption.id))
                .where(
                    Consumption.person_id.in_(ids),
                    Consumption.status == ConsumptionStatus.OFFEN,
                )
                .group_by(Consumption.person_id)
            ).all()
        }

    last_invoices: dict[int, Invoice] = {}
    if people:
        ids = [p.id for p in people]
        for inv in db.execute(
            select(Invoice)
            .where(
                Invoice.person_id.in_(ids),
                Invoice.status != InvoiceStatus.STORNIERT,
            )
            .order_by(Invoice.id.desc())
        ).scalars().all():
            last_invoices.setdefault(inv.person_id, inv)

    return render_template(
        "persons/index.html",
        persons=people,
        balances=balances,
        open_counts=open_counts,
        last_invoices=last_invoices,
        query=query,
        show_inactive=show_inactive,
        currency=settings.currency,
        total_open=sum(balances.values()),
    )


@bp.route("/neu", methods=["GET", "POST"])
@setup_required
@login_required
def create():
    validate_csrf()
    db = g.db
    if request.method == "GET":
        return render_template(
            "persons/form.html", person=None, form={}, errors={}, balance=0
        )

    v = Validator()
    first = v.required_text("first_name", request.form.get("first_name"), label="Vorname", max_len=80)
    last = v.required_text("last_name", request.form.get("last_name"), label="Nachname", max_len=80)
    email = v.email("email", request.form.get("email"))
    phone = v.optional_text("phone", request.form.get("phone"), max_len=40)
    notes = v.optional_text("notes", request.form.get("notes"), max_len=2000, label="Notizen")
    active = v.boolean("is_active", request.form.get("is_active")) if "is_active" in request.form else True

    if not v.errors:
        duplicate = db.execute(
            select(Person).where(func.lower(Person.email) == email)
        ).scalars().first()
        if duplicate is not None:
            v.add("email", "Diese E-Mail-Adresse wird bereits verwendet")

    if v.errors:
        flash(v.first_error(), "error")
        return render_template(
            "persons/form.html",
            person=None,
            form=request.form,
            errors=v.errors,
        )

    person = Person(
        first_name=first,
        last_name=last,
        email=email,
        phone=phone,
        notes=notes,
        is_active=active,
    )
    db.add(person)
    db.commit()
    audit(db, "person.created", target=f"{person.full_name} (#{person.id})",
          detail=email)
    flash(f"Person {person.full_name} wurde angelegt.", "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>", methods=["GET"])
@setup_required
@login_required
def detail(person_id: int):
    db = g.db
    settings = Settings(db)
    person = db.get(Person, person_id)
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("persons.index"))
    consumptions, invoices = person_history(db, person_id, limit=200)
    balance = open_balance_cents(db, person_id)
    tz = get_tz(settings.timezone)
    return render_template(
        "persons/detail.html",
        person=person,
        consumptions=consumptions,
        invoices=invoices,
        balance=balance,
        currency=settings.currency,
        tz=tz,
    )


@bp.route("/<int:person_id>/bearbeiten", methods=["GET", "POST"])
@setup_required
@login_required
def edit(person_id: int):
    validate_csrf()
    db = g.db
    person = db.get(Person, person_id)
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("persons.index"))

    if request.method == "GET":
        return render_template(
            "persons/form.html",
            person=person,
            form={},
            errors={},
            balance=open_balance_cents(db, person_id),
        )

    v = Validator()
    first = v.required_text("first_name", request.form.get("first_name"), label="Vorname", max_len=80)
    last = v.required_text("last_name", request.form.get("last_name"), label="Nachname", max_len=80)
    email = v.email("email", request.form.get("email"))
    phone = v.optional_text("phone", request.form.get("phone"), max_len=40)
    notes = v.optional_text("notes", request.form.get("notes"), max_len=2000, label="Notizen")
    active = v.boolean("is_active", request.form.get("is_active"))

    if not v.errors:
        duplicate = db.execute(
            select(Person).where(func.lower(Person.email) == email, Person.id != person_id)
        ).scalars().first()
        if duplicate is not None:
            v.add("email", "Diese E-Mail-Adresse wird bereits verwendet")

    if v.errors:
        flash(v.first_error(), "error")
        return render_template(
            "persons/form.html",
            person=person,
            form=request.form,
            errors=v.errors,
            balance=open_balance_cents(db, person_id),
        )

    person.first_name = first
    person.last_name = last
    person.email = email
    person.phone = phone
    person.notes = notes
    person.is_active = active
    db.commit()
    audit(db, "person.updated", target=f"{person.full_name} (#{person.id})", detail=email)
    flash("Person wurde aktualisiert.", "success")
    return redirect(url_for("persons.detail", person_id=person.id))


@bp.route("/<int:person_id>/status", methods=["POST"])
@setup_required
@login_required
def toggle_status(person_id: int):
    validate_csrf()
    db = g.db
    person = db.get(Person, person_id)
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("persons.index"))
    person.is_active = not person.is_active
    db.commit()
    audit(
        db,
        "person.activated" if person.is_active else "person.deactivated",
        target=f"{person.full_name} (#{person.id})",
    )
    flash(
        f"{person.full_name} ist jetzt {'aktiv' if person.is_active else 'inaktiv'}.",
        "success",
    )
    return redirect(request.referrer or url_for("persons.index"))


@bp.route("/<int:person_id>/loeschen", methods=["POST"])
@setup_required
@login_required
def delete(person_id: int):
    validate_csrf()
    db = g.db
    person = db.get(Person, person_id)
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("persons.index"))
    name = person.full_name
    open_bookings = int(
        db.execute(
            select(func.count(Consumption.id)).where(
                Consumption.person_id == person_id,
                Consumption.status == ConsumptionStatus.OFFEN,
            )
        ).scalar_one()
        or 0
    )
    if open_bookings and request.form.get("confirm_open") != "yes":
        flash(
            f"{name} hat noch {open_bookings} offene Buchung(en). "
            "Zum Loeschen muss dies bestaetigt werden - es gehen dabei auch "
            "die Abrechnungshistorie verloren.",
            "warning",
        )
        return redirect(url_for("persons.detail", person_id=person_id))

    db.delete(person)
    db.commit()
    audit(db, "person.deleted", target=f"{name} (#{person_id})")
    flash(f"{name} wurde geloescht.", "success")
    return redirect(url_for("persons.index"))


@bp.route("/<int:person_id>/export.csv")
@setup_required
@login_required
def export_csv(person_id: int):
    db = g.db
    settings = Settings(db)
    person = db.get(Person, person_id)
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("persons.index"))
    data = consumptions_csv(db, settings, person_id=person_id)
    audit(db, "person.exported", target=person.full_name)
    return _csv_response(
        data, f"verzehr-{person.first_name}-{person.last_name}".replace(" ", "_")
    )


def _csv_response(data: str, name: str):
    from flask import Response

    # BOM so Excel UTF-8 correctly detects
    return Response(
        "\ufeff" + data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )
