"""Configuration loading.

Precedence (highest first):

1. real environment variables
2. ``.env`` file in the project root
3. built-in defaults

Secrets are *not* part of this module's defaults; see
:mod:`app.secrets_store` for the SMTP password / secret key handling.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_DIR = PROJECT_ROOT / "instance"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "ja"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    """Immutable-ish configuration object passed to the app factory."""

    def __init__(self, root: Path | None = None) -> None:
        self.ROOT = Path(root) if root else PROJECT_ROOT
        # Remember what the *operator* exported before ``.env`` is merged in.
        # A value that comes from ``.env`` can be changed in the setup wizard,
        # a value pinned by systemd or the shell must not be.
        self.pinned_env = dict(os.environ)
        load_dotenv(self.ROOT / ".env", override=False)

        self.APP_NAME = os.environ.get("APP_NAME", "Verzehrabrechnung")
        self.APP_ENV = os.environ.get("APP_ENV", "production")
        self.HOST = os.environ.get("APP_HOST", "0.0.0.0")
        self.PORT = _int("APP_PORT", 8000)
        self.URL = os.environ.get("APP_URL", "").rstrip("/")
        self.TIMEZONE = os.environ.get("APP_TIMEZONE", "Europe/Berlin")
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.LOG_FILE = os.environ.get("LOG_FILE", "").strip()
        self.WORKERS = _int("WORKERS", 2)

        self.SECRETS_FILE = Path(
            os.environ.get("SECRETS_FILE", str(INSTANCE_DIR / "secrets.env"))
        )
        self.SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)

        self.DATABASE_URL = self._resolve_database_url()
        if self.DATABASE_URL.startswith("sqlite:///") and ":memory:" not in self.DATABASE_URL:
            db_path = Path(self.DATABASE_URL.replace("sqlite:///", "", 1))
            db_path.parent.mkdir(parents=True, exist_ok=True)

        self.TESTING = _bool("TESTING", self.APP_ENV == "testing")
        self.SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", not self.TESTING)
        self.SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or None

    def _resolve_database_url(self) -> str:
        """DATABASE_URL: pinned env var > secrets store > ``.env`` > default.

        The store carries a choice made in the setup wizard, which must survive
        a restart, but must never override what the operator pinned explicitly.
        """
        default = f"sqlite:///{(INSTANCE_DIR / 'payment.db').as_posix()}"
        pinned = (self.pinned_env.get("DATABASE_URL") or "").strip()
        if pinned:
            return pinned
        try:
            from .secrets_store import SecretsStore

            stored = (SecretsStore(self.SECRETS_FILE).get("DATABASE_URL") or "").strip()
            if stored:
                return stored
        except Exception:  # noqa: BLE001 - never block startup on this
            pass
        return os.environ.get("DATABASE_URL", "").strip() or default

    # -- derived helpers ---------------------------------------------------
    @property
    def database_url_pinned(self) -> bool:
        """True when the operator pinned DATABASE_URL in the real environment."""
        return bool((self.pinned_env.get("DATABASE_URL") or "").strip())

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def debug(self) -> bool:
        return self.APP_ENV in {"development", "testing"}


def configure_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.LOG_LEVEL, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg.LOG_FILE:
        try:
            Path(cfg.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.FileHandler(cfg.LOG_FILE, encoding="utf-8")
            )
        except OSError:
            pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # APScheduler is noisy at INFO
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
