"""WSGI entry point.

Development::

    python -m app            # or: flask --app wsgi run

Production (systemd uses gunicorn against ``wsgi:application``).
"""

from __future__ import annotations


from app import create_app
from app.config import Config

application = create_app()
app = application

if __name__ == "__main__":
    cfg: Config = app.extensions["settings"]
    app.run(
        host=cfg.HOST,
        port=cfg.PORT,
        debug=cfg.debug,
        use_reloader=cfg.debug,
        threaded=True,
    )
