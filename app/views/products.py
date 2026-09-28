"""Product management."""

from __future__ import annotations

import logging

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_, select

from ..models import Consumption, ConsumptionStatus, Product
from ..security import audit, login_required, setup_required, validate_csrf
from ..settings_service import Settings
from ..validators import Validator

log = logging.getLogger(__name__)

bp = Blueprint("products", __name__, url_prefix="/produkte")


@bp.route("/")
@setup_required
@login_required
def index():
    db = g.db
    settings = Settings(db)
    currency = settings.currency
    query = (request.args.get("q") or "").strip()
    show_inactive = request.args.get("inactive") == "1"

    stmt = select(Product)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(Product.name.ilike(like), Product.description.ilike(like)))
    if not show_inactive:
        stmt = stmt.where(Product.is_active.is_(True))
    stmt = stmt.order_by(Product.name.asc())
    products = list(db.execute(stmt).scalars().all())

    usage: dict[int, int] = {}
    open_usage: dict[int, int] = {}
    if products:
        ids = [p.id for p in products]
        usage = {
            int(pid): int(cnt or 0)
            for pid, cnt in db.execute(
                select(Consumption.product_id, func.count(Consumption.id))
                .where(Consumption.product_id.in_(ids))
                .group_by(Consumption.product_id)
            ).all()
        }
        open_usage = {
            int(pid): int(cnt or 0)
            for pid, cnt in db.execute(
                select(Consumption.product_id, func.count(Consumption.id))
                .where(
                    Consumption.product_id.in_(ids),
                    Consumption.status == ConsumptionStatus.OFFEN,
                )
                .group_by(Consumption.product_id)
            ).all()
        }

    return render_template(
        "products/index.html",
        products=products,
        usage=usage,
        open_usage=open_usage,
        query=query,
        show_inactive=show_inactive,
        currency=currency,
    )


@bp.route("/neu", methods=["GET", "POST"])
@setup_required
@login_required
def create():
    validate_csrf()
    db = g.db
    settings = Settings(db)
    if request.method == "GET":
        return render_template(
            "products/form.html", product=None, form={}, errors={}, currency=settings.currency
        )

    v = Validator()
    name = v.required_text("name", request.form.get("name"), label="Name", max_len=120)
    description = v.optional_text("description", request.form.get("description"), max_len=2000)
    category = v.optional_text("category", request.form.get("category"), max_len=60, label="Kategorie")
    price = v.money("price", request.form.get("price"), currency=settings.currency)
    active = v.boolean("is_active", request.form.get("is_active")) if "is_active" in request.form else True

    if not v.errors and name:
        duplicate = db.execute(
            select(Product).where(func.lower(Product.name) == name.lower())
        ).scalars().first()
        if duplicate is not None:
            v.add("name", "Ein Produkt mit diesem Namen existiert bereits")

    if v.errors:
        flash(v.first_error(), "error")
        return render_template(
            "products/form.html",
            product=None,
            form=request.form,
            errors=v.errors,
            currency=settings.currency,
        )

    product = Product(
        name=name,
        description=description,
        category=category,
        price_cents=price,
        is_active=active,
    )
    db.add(product)
    db.commit()
    audit(db, "product.created", target=name, detail=f"{price} Cent")
    flash(f"Produkt {name} wurde angelegt.", "success")
    return redirect(url_for("products.index"))


@bp.route("/<int:product_id>/bearbeiten", methods=["GET", "POST"])
@setup_required
@login_required
def edit(product_id: int):
    validate_csrf()
    db = g.db
    settings = Settings(db)
    product = db.get(Product, product_id)
    if product is None:
        flash("Produkt nicht gefunden.", "error")
        return redirect(url_for("products.index"))

    if request.method == "GET":
        return render_template(
            "products/form.html",
            product=product,
            form={},
            errors={},
            currency=settings.currency,
        )

    v = Validator()
    name = v.required_text("name", request.form.get("name"), label="Name", max_len=120)
    description = v.optional_text("description", request.form.get("description"), max_len=2000)
    category = v.optional_text("category", request.form.get("category"), max_len=60, label="Kategorie")
    price = v.money("price", request.form.get("price"), currency=settings.currency)
    active = v.boolean("is_active", request.form.get("is_active"))

    if not v.errors and name:
        duplicate = db.execute(
            select(Product).where(
                func.lower(Product.name) == name.lower(), Product.id != product_id
            )
        ).scalars().first()
        if duplicate is not None:
            v.add("name", "Ein Produkt mit diesem Namen existiert bereits")

    if v.errors:
        flash(v.first_error(), "error")
        return render_template(
            "products/form.html",
            product=product,
            form=request.form,
            errors=v.errors,
            currency=settings.currency,
        )

    old_price = product.price_cents
    product.name = name
    product.description = description
    product.category = category
    product.price_cents = price
    product.is_active = active
    db.commit()
    if old_price != price:
        audit(
            db,
            "product.price_changed",
            target=name,
            detail=f"{old_price} -> {price} Cent (bestehende Buchungen bleiben unveraendert)",
        )
        flash(
            "Preis gespeichert. Bereits gebuchte Verzehrzeilen behalten "
            "ihren alten Einzelpreis.",
            "info",
        )
    else:
        audit(db, "product.updated", target=name)
        flash("Produkt wurde aktualisiert.", "success")
    return redirect(url_for("products.index"))


@bp.route("/<int:product_id>/status", methods=["POST"])
@setup_required
@login_required
def toggle_status(product_id: int):
    validate_csrf()
    db = g.db
    product = db.get(Product, product_id)
    if product is None:
        flash("Produkt nicht gefunden.", "error")
        return redirect(url_for("products.index"))
    product.is_active = not product.is_active
    db.commit()
    audit(
        db,
        "product.activated" if product.is_active else "product.deactivated",
        target=product.name,
    )
    flash(
        f"{product.name} ist jetzt {'aktiv' if product.is_active else 'inaktiv'}.",
        "success",
    )
    return redirect(request.referrer or url_for("products.index"))


@bp.route("/<int:product_id>/loeschen", methods=["POST"])
@setup_required
@login_required
def delete(product_id: int):
    validate_csrf()
    db = g.db
    product = db.get(Product, product_id)
    if product is None:
        flash("Produkt nicht gefunden.", "error")
        return redirect(url_for("products.index"))
    open_count = int(
        db.execute(
            select(func.count(Consumption.id)).where(
                Consumption.product_id == product_id,
                Consumption.status == ConsumptionStatus.OFFEN,
            )
        ).scalar_one()
        or 0
    )
    if open_count:
        flash(
            f"{product.name} wird noch {open_count}x offen verzehrt. "
            "Bitte zuerst abrechnen oder stornieren, oder das Produkt nur "
            "deaktivieren.",
            "warning",
        )
        return redirect(url_for("products.index"))
    name = product.name
    # historische Buchungen behalten ihren Snapshot (product_id -> NULL)
    db.delete(product)
    db.commit()
    audit(db, "product.deleted", target=name)
    flash(f"{name} wurde geloescht. Historische Buchungen bleiben erhalten.", "success")
    return redirect(url_for("products.index"))
