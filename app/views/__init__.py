"""Blueprint registration."""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from .setup import bp as setup_bp
    from .auth import bp as auth_bp
    from .main import bp as main_bp
    from .persons import bp as persons_bp
    from .products import bp as products_bp
    from .consumption import bp as consumption_bp
    from .invoices import bp as invoices_bp
    from .settings_view import bp as settings_bp
    from .system import bp as system_bp
    from .api import bp as api_bp

    app.register_blueprint(setup_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(persons_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(consumption_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(api_bp)


__all__ = ["register_blueprints"]
