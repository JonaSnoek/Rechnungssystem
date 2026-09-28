"""Small JSON API (session authenticated, same-origin only)."""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request

from ..models import Person, Product
from ..money import format_cents
from ..security import login_required, setup_required, session_valid
from ..settings_service import Settings

log = logging.getLogger(__name__)

bp = Blueprint("api", __name__, url_prefix="/api")


def _guard():
    """API auth via the same session, returning a 401 response when invalid."""
    if not session_valid(g.db):
        return jsonify({"error": "Nicht angemeldet"}), 401
    return None


@bp.route("/session")
def session_info():
    blocked = _guard()
    if blocked:
        return blocked
    settings = Settings(g.db)
    return jsonify(
        {
            "ok": True,
            "admin": getattr(g, "admin", None).username,
            "currency": settings.currency,
            "app_name": settings.app_name,
        }
    )


@bp.route("/persons")
@setup_required
@login_required
def persons():
    query = (request.args.get("q") or "").strip()
    stmt = g.db.query(Person).filter(Person.is_active.is_(True))
    if query:
        like = f"%{query}%"
        stmt = stmt.filter(
            (Person.first_name.ilike(like))
            | (Person.last_name.ilike(like))
            | (Person.email.ilike(like))
        )
    rows = stmt.order_by(Person.last_name, Person.first_name).limit(100).all()
    return jsonify(
        {
            "ok": True,
            "persons": [
                {"id": p.id, "name": p.full_name, "email": p.email} for p in rows
            ],
        }
    )


@bp.route("/products")
@setup_required
@login_required
def products():
    settings = Settings(g.db)
    rows = (
        g.db.query(Product)
        .filter(Product.is_active.is_(True))
        .order_by(Product.name)
        .limit(200)
        .all()
    )
    return jsonify(
        {
            "ok": True,
            "currency": settings.currency,
            "products": [
                {
                    "id": p.id,
                    "name": p.name,
                    "price": format_cents(p.price_cents, settings.currency),
                    "price_cents": p.price_cents,
                }
                for p in rows
            ],
        }
    )


@bp.route("/products/<int:product_id>")
@setup_required
@login_required
def product_detail(product_id: int):
    settings = Settings(g.db)
    product = g.db.get(Product, product_id)
    if product is None:
        return jsonify({"error": "Produkt nicht gefunden"}), 404
    return jsonify(
        {
            "ok": True,
            "product": {
                "id": product.id,
                "name": product.name,
                "description": product.description,
                "price": format_cents(product.price_cents, settings.currency),
                "price_cents": product.price_cents,
                "is_active": product.is_active,
            },
        }
    )
