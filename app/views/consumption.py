"""Verzehr erfassen: quick entry grid and per-person detail."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import asc, func, select

from ..models import Consumption, ConsumptionStatus, Person, Product, new_token
from ..security import audit, login_required, setup_required, validate_csrf
from ..services.accounts import (
    balance_cents as account_balance_cents,
)
from ..services.accounts import (
    post_consumption,
    reopen_consumption,
    reverse_consumption,
)
from ..services.billing import get_tz, local_day_bounds, local_now, open_balance_cents
from ..settings_service import Settings

log = logging.getLogger(__name__)

bp = Blueprint("consumption", __name__, url_prefix="/verzehr")

MAX_QUANTITY = 999


@bp.route("/erfassen", methods=["GET"])
@setup_required
@login_required
def entry():
    db = g.db
    settings = Settings(db)
    currency = settings.currency

    people = list(
        db.execute(
            select(Person).where(Person.is_active.is_(True)).order_by(Person.last_name, Person.first_name)
        )
        .scalars()
        .all()
    )
    products = list(
        db.execute(
            select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
        )
        .scalars()
        .all()
    )

    person_id = request.args.get("person_id", type=int)
    if person_id and all(p.id != person_id for p in people):
        person_id = None
    person = next((p for p in people if p.id == person_id), None)

    balance = open_balance_cents(db, person_id) if person_id else 0
    tz = get_tz(settings.timezone)
    now = local_now(tz)
    start_utc, _ = local_day_bounds(now.date(), tz)
    today_count = 0
    if person_id:
        today_count = int(
            db.execute(
                select(func.coalesce(func.sum(Consumption.quantity), 0)).where(
                    Consumption.person_id == person_id,
                    Consumption.created_at >= start_utc,
                )
            ).scalar_one()
            or 0
        )

    recent = []
    if person_id:
        recent = list(
            db.execute(
                select(Consumption, Product.name)
                .join(Product, Consumption.product_id == Product.id, isouter=True)
                .where(Consumption.person_id == person_id)
                .order_by(Consumption.created_at.desc(), Consumption.id.desc())
                .limit(8)
            ).all()
        )

    return render_template(
        "consumption/entry.html",
        persons=people,
        products=products,
        person=person,
        person_id=person_id,
        balance=balance,
        today_count=today_count,
        recent=recent,
        currency=currency,
        now=now,
        max_quantity=MAX_QUANTITY,
    )


def _parse_cart(form) -> tuple[dict[int, int], list[str]]:
    """Read the ``qty_<product_id>`` inputs. Returns counts and problems."""
    counts: dict[int, int] = {}
    problems: list[str] = []
    for key, raw in form.items():
        if not key.startswith("qty_"):
            continue
        try:
            product_id = int(key[4:])
        except ValueError:
            continue
        text = (raw or "").strip()
        if text == "":
            continue
        try:
            qty = int(float(text))
        except ValueError:
            problems.append("Ungueltige Menge angegeben.")
            continue
        if qty == 0:
            continue
        if qty < 0:
            problems.append("Negative Mengen sind nicht moeglich.")
            continue
        if qty > MAX_QUANTITY:
            problems.append(f"Menge ist auf {MAX_QUANTITY} begrenzt.")
            continue
        counts[product_id] = counts.get(product_id, 0) + qty
    return counts, problems


@bp.route("/speichern", methods=["POST"])
@setup_required
@login_required
def save():
    validate_csrf()
    db = g.db
    settings = Settings(db)
    currency = settings.currency

    person_id = request.form.get("person_id", type=int)
    if not person_id:
        flash("Bitte eine Person auswaehlen.", "error")
        return redirect(url_for("consumption.entry"))
    person = db.get(Person, person_id)
    if person is None or not person.is_active:
        flash("Person ist nicht (mehr) aktiv.", "error")
        return redirect(url_for("consumption.entry"))

    counts, problems = _parse_cart(request.form)
    for problem in problems:
        flash(problem, "error")
    if problems:
        return redirect(url_for("consumption.entry", person_id=person_id))

    if not counts:
        flash("Es wurde nichts gebucht.", "warning")
        return redirect(url_for("consumption.entry", person_id=person_id))

    product_ids = list(counts.keys())
    products = {
        p.id: p
        for p in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars().all()
    }
    note = (request.form.get("note") or "").strip()[:255] or None
    batch_id = new_token(8)
    total = 0
    created = 0
    for product_id, qty in counts.items():
        product = products.get(product_id)
        if product is None:
            flash(f"Produkt #{product_id} existiert nicht mehr.", "error")
            continue
        if not product.is_active:
            flash(f"{product.name} ist deaktiviert und wurde nicht gebucht.", "warning")
            continue
        if product.price_cents < 0:
            flash(f"{product.name} hat einen ungueltigen Preis.", "error")
            continue
        line_total = int(product.price_cents) * qty
        row = Consumption(
            person_id=person.id,
            product_id=product.id,
            product_name=product.name,
            product_description=product.description,
            unit_price_cents=product.price_cents,
            quantity=qty,
            total_cents=line_total,
            currency=currency,
            status=ConsumptionStatus.OFFEN,
            note=note,
            batch_id=batch_id,
        )
        db.add(row)
        # Die Buchung belastet das Konto sofort, nicht erst beim Rechnungsversand.
        db.flush()
        post_consumption(db, row, currency=currency)
        total += line_total
        created += 1

    db.commit()
    if created:
        from ..money import format_cents

        audit(
            db,
            "consumption.created",
            target=person.full_name,
            detail=f"{created} Position(en), {format_cents(total, currency)}",
        )
        flash(
            f"{created} Position(en) fuer {person.full_name} gespeichert "
            f"({format_cents(total, currency)}).",
            "success",
        )
    return redirect(url_for("consumption.entry", person_id=person_id, saved=batch_id))


@bp.route("/schnell", methods=["POST"])
@setup_required
@login_required
def quick():
    """Single-product quick booking: product + person + optional qty.

    Designed for fast repeated use from a tablet or phone.
    """
    validate_csrf()
    db = g.db
    settings = Settings(db)
    currency = settings.currency

    person_id = request.form.get("person_id", type=int)
    product_id = request.form.get("product_id", type=int)
    qty_raw = (request.form.get("quantity") or "1").strip()
    try:
        qty = int(qty_raw)
    except ValueError:
        qty = 1
    qty = max(1, min(MAX_QUANTITY, qty))

    person = db.get(Person, person_id) if person_id else None
    product = db.get(Product, product_id) if product_id else None
    if person is None or not person.is_active:
        return jsonify({"ok": False, "error": "Person nicht gefunden"}), 400
    if product is None or not product.is_active:
        return jsonify({"ok": False, "error": "Produkt nicht verfuegbar"}), 404

    line_total = int(product.price_cents) * qty
    consumption = Consumption(
        person_id=person.id,
        product_id=product.id,
        product_name=product.name,
        product_description=product.description,
        unit_price_cents=product.price_cents,
        quantity=qty,
        total_cents=line_total,
        currency=currency,
        status=ConsumptionStatus.OFFEN,
        batch_id=new_token(8),
        note=(request.form.get("note") or "").strip()[:255] or None,
    )
    db.add(consumption)
    db.flush()
    # Belastung des Guthabenkontos sofort bei der Erfassung.
    post_consumption(db, consumption, currency=currency)
    db.commit()
    audit(db, "consumption.quick", target=person.full_name, detail=f"{qty}x {product.name}")

    balance = account_balance_cents(db, person.id)
    from ..money import format_cents

    return jsonify(
        {
            "ok": True,
            "message": f"{qty}x {product.name}",
            "line_total": format_cents(line_total, currency),
            "balance": format_cents(balance, currency),
            "balance_cents": balance,
            "product": {
                "id": product.id,
                "name": product.name,
                "price_cents": product.price_cents,
                "price": format_cents(product.price_cents, currency),
            },
        }
    )


@bp.route("/<int:consumption_id>/stornieren", methods=["POST"])
@setup_required
@login_required
def cancel(consumption_id: int):
    validate_csrf()
    db = g.db
    settings = Settings(db)
    consumption = db.get(Consumption, consumption_id)
    if consumption is None:
        flash("Buchung nicht gefunden.", "error")
        return redirect(url_for("main.dashboard"))
    if consumption.status == ConsumptionStatus.ABGERECHNET:
        flash(
            "Diese Buchung ist bereits abgerechnet und kann nicht mehr "
            "storniert werden. Bitte die Abrechnung stornieren.",
            "warning",
        )
        return redirect(request.referrer or url_for("main.dashboard"))
    consumption.status = ConsumptionStatus.STORNIERT
    consumption.invoice_id = None
    # Ausgleich durch eine Gegenbuchung; die urspruengliche Verzehrbewegung
    # bleibt in der Kontohistorie sichtbar.
    reverse_consumption(db, consumption, currency=settings.currency)
    db.commit()
    audit(
        db,
        "consumption.cancelled",
        target=f"{consumption.quantity}x {consumption.product_name}",
    )
    flash("Buchung storniert.", "success")
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.route("/<int:consumption_id>/bestaetigen", methods=["POST"])
@setup_required
@login_required
def reopen(consumption_id: int):
    """Set a cancelled booking back to OFFEN."""
    validate_csrf()
    db = g.db
    settings = Settings(db)
    consumption = db.get(Consumption, consumption_id)
    if consumption is None:
        flash("Buchung nicht gefunden.", "error")
        return redirect(url_for("main.dashboard"))
    if consumption.status == ConsumptionStatus.ABGERECHNET:
        flash("Buchung ist abgerechnet.", "warning")
        return redirect(request.referrer or url_for("main.dashboard"))
    consumption.status = ConsumptionStatus.OFFEN
    reopen_consumption(db, consumption, currency=settings.currency)
    db.commit()
    audit(db, "consumption.reopened", target=consumption.product_name)
    flash("Buchung wieder aktiviert.", "success")
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.route("/uebersicht", methods=["GET"])
@setup_required
@login_required
def overview():
    db = g.db
    settings = Settings(db)
    status_filter = request.args.get("status") or ""
    person_id = request.args.get("person_id", type=int)
    page = max(1, request.args.get("page", type=int) or 1)
    per_page = 50

    stmt = select(Consumption, Person.first_name, Person.last_name).join(
        Person, Consumption.person_id == Person.id
    )
    if status_filter in {s.value for s in ConsumptionStatus}:
        stmt = stmt.where(Consumption.status == ConsumptionStatus(status_filter))
    else:
        stmt = stmt.where(Consumption.status != ConsumptionStatus.STORNIERT)
    if person_id:
        stmt = stmt.where(Consumption.person_id == person_id)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(db.execute(count_stmt).scalar_one() or 0)
    rows = list(
        db.execute(
            stmt.order_by(Consumption.created_at.desc(), Consumption.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    pages = max(1, (total + per_page - 1) // per_page)
    person_options = list(
        db.execute(select(Person).order_by(asc(Person.last_name), asc(Person.first_name)))
        .scalars()
        .all()
    )
    return render_template(
        "consumption/overview.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
        status_filter=status_filter,
        person_id=person_id,
        person_options=person_options,
        currency=settings.currency,
        ConsumptionStatus=ConsumptionStatus,
    )
