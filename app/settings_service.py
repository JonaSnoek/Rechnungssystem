"""Typed access to application settings.

Non-sensitive values live in the ``settings`` table and are editable through
the web UI. Sensitive values (SMTP password, session secret) live only in the
local secrets store and never in a database export.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Setting, utcnow

# --- Keys ------------------------------------------------------------------
APP_NAME = "app_name"
APP_URL = "app_url"
TIMEZONE = "timezone"
DATE_FORMAT = "date_format"
CURRENCY = "currency"
PAYPAL_ME_USERNAME = "paypal_me_username"
PAYPAL_BASE_URL = "paypal_base_url"
AUTO_BILLING_ENABLED = "auto_billing_enabled"
AUTO_BILLING_TIME = "auto_billing_time"
SMTP_HOST = "smtp_host"
SMTP_PORT = "smtp_port"
SMTP_ENCRYPTION = "smtp_encryption"
SMTP_USERNAME = "smtp_username"
MAIL_FROM_NAME = "mail_from_name"
MAIL_FROM_ADDRESS = "mail_from_address"
MAIL_REPLY_TO = "mail_reply_to"
EMAIL_SUBJECT_TEMPLATE = "email_subject_template"
EMAIL_BODY_TEXT_TEMPLATE = "email_body_text_template"
EMAIL_BODY_HTML_TEMPLATE = "email_body_html_template"
INVOICE_NUMBER_PREFIX = "invoice_number_prefix"
INCLUDE_ZERO_ITEMS = "include_zero_items"
AUTO_CATCHUP_ENABLED = "auto_catchup_enabled"
SETUP_COMPLETED = "setup_completed"
RETAIN_DATA_MONTHS = "retain_data_months"

DEFAULTS: dict[str, Any] = {
    APP_NAME: "Verzehrabrechnung",
    APP_URL: "",
    TIMEZONE: "Europe/Berlin",
    DATE_FORMAT: "%d.%m.%Y",
    CURRENCY: "EUR",
    PAYPAL_ME_USERNAME: "",
    PAYPAL_BASE_URL: "https://www.paypal.me",
    AUTO_BILLING_ENABLED: "true",
    AUTO_BILLING_TIME: "17:00",
    SMTP_HOST: "",
    SMTP_PORT: "587",
    SMTP_ENCRYPTION: "starttls",
    SMTP_USERNAME: "",
    MAIL_FROM_NAME: "Verzehrabrechnung",
    MAIL_FROM_ADDRESS: "",
    MAIL_REPLY_TO: "",
    EMAIL_SUBJECT_TEMPLATE: "Deine Verzehrabrechnung vom {datum}",
    EMAIL_BODY_TEXT_TEMPLATE: """Hallo {vorname},

du hast am {datum} folgende Sachen verzehrt:

{produkte}

Gesamt: {gesamtbetrag}

{kontenuebersicht}

Vielen Dank!""",
    EMAIL_BODY_HTML_TEMPLATE: """<p>Hallo {vorname},</p>
<p>du hast am <strong>{datum}</strong> folgende Sachen verzehrt:</p>
{produkte_html}
<p class="total">Gesamt: <strong>{gesamtbetrag}</strong></p>
{kontenuebersicht_html}
<p>Vielen Dank!</p>""",
    INVOICE_NUMBER_PREFIX: "RE-",
    INCLUDE_ZERO_ITEMS: "true",
    AUTO_CATCHUP_ENABLED: "true",
    SETUP_COMPLETED: "false",
    RETAIN_DATA_MONTHS: "0",
}

GROUPS = {
    APP_NAME: "system",
    APP_URL: "system",
    TIMEZONE: "system",
    DATE_FORMAT: "system",
    CURRENCY: "system",
    PAYPAL_ME_USERNAME: "paypal",
    PAYPAL_BASE_URL: "paypal",
    AUTO_BILLING_ENABLED: "billing",
    AUTO_BILLING_TIME: "billing",
    AUTO_CATCHUP_ENABLED: "billing",
    INVOICE_NUMBER_PREFIX: "billing",
    INCLUDE_ZERO_ITEMS: "billing",
    SMTP_HOST: "email",
    SMTP_PORT: "email",
    SMTP_ENCRYPTION: "email",
    SMTP_USERNAME: "email",
    MAIL_FROM_NAME: "email",
    MAIL_FROM_ADDRESS: "email",
    MAIL_REPLY_TO: "email",
    EMAIL_SUBJECT_TEMPLATE: "email",
    EMAIL_BODY_TEXT_TEMPLATE: "email",
    EMAIL_BODY_HTML_TEMPLATE: "email",
    SETUP_COMPLETED: "system",
    RETAIN_DATA_MONTHS: "system",
}

SECRET_KEYS = {"SMTP_PASSWORD", "SECRET_KEY"}


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "ja"}


class Settings:
    """Small read/write facade around the settings table."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- raw ---------------------------------------------------------------
    def all_raw(self) -> dict[str, str]:
        rows = self.session.execute(select(Setting)).scalars().all()
        data = {k: (v if v is not None else "") for k, v in DEFAULTS.items()}
        for row in rows:
            if row.is_secret:
                continue
            data[row.key] = row.value if row.value is not None else ""
        return data

    def get(self, key: str, default: Any = None) -> Any:
        row = self.session.get(Setting, key)
        if row is None:
            return DEFAULTS.get(key, default)
        if row.value is None:
            return DEFAULTS.get(key, default)
        return row.value

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key)
        if raw is None:
            return DEFAULTS.get(key, str(default).lower()) in {
                "1", "true", "yes", "on", "ja"
            } or default
        return _as_bool(str(raw), default)

    def get_int(self, key: str, default: int) -> int:
        raw = self.get(key)
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        row = self.session.get(Setting, key)
        text = "" if value is None else str(value)
        if row is None:
            self.session.add(
                Setting(
                    key=key,
                    value=text,
                    group=GROUPS.get(key, "allgemein"),
                    is_secret=False,
                )
            )
        else:
            row.value = text
            row.group = GROUPS.get(key, row.group)
            row.updated_at = utcnow()

    def set_many(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def delete(self, key: str) -> None:
        row = self.session.get(Setting, key)
        if row is not None:
            self.session.delete(row)

    def reset_defaults(self) -> None:
        for key, value in DEFAULTS.items():
            self.set(key, value)

    # -- typed convenience -------------------------------------------------
    @property
    def currency(self) -> str:
        return (self.get(CURRENCY) or "EUR").upper()

    @property
    def timezone(self) -> str:
        return self.get(TIMEZONE) or "Europe/Berlin"

    @property
    def date_format(self) -> str:
        return self.get(DATE_FORMAT) or "%d.%m.%Y"

    @property
    def paypal_username(self) -> str:
        return (self.get(PAYPAL_ME_USERNAME) or "").strip()

    @property
    def paypal_base_url(self) -> str:
        return (self.get(PAYPAL_BASE_URL) or "https://www.paypal.me").rstrip("/")

    @property
    def app_name(self) -> str:
        return self.get(APP_NAME) or "Verzehrabrechnung"

    @property
    def app_url(self) -> str:
        return (self.get(APP_URL) or "").rstrip("/")

    @property
    def auto_billing_enabled(self) -> bool:
        return self.get_bool(AUTO_BILLING_ENABLED, True)

    @property
    def auto_billing_time(self) -> str:
        return _normalise_time(self.get(AUTO_BILLING_TIME) or "17:00")

    @property
    def auto_catchup_enabled(self) -> bool:
        return self.get_bool(AUTO_CATCHUP_ENABLED, True)

    @property
    def setup_completed(self) -> bool:
        return self.get_bool(SETUP_COMPLETED, False)

    @property
    def smtp_encryption(self) -> str:
        value = (self.get(SMTP_ENCRYPTION) or "starttls").lower()
        return value if value in {"none", "starttls", "ssl"} else "starttls"

    @property
    def smtp_port(self) -> int:
        return self.get_int(SMTP_PORT, 587)

    def format_date(self, value: date | datetime) -> str:
        return value.strftime(self.date_format)

    def summary(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "currency": self.currency,
            "timezone": self.timezone,
            "paypal_username": self.paypal_username,
            "auto_billing_enabled": self.auto_billing_enabled,
            "auto_billing_time": self.auto_billing_time,
        }


def _normalise_time(value: str) -> str:
    """Accept 17:00, 17.00, 5:00 PM-ish loose input -> always HH:MM 24h."""
    text = (value or "").strip()
    if not text:
        return "17:00"
    text = text.replace(".", ":")
    parts = text.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return "17:00"
    hours = max(0, min(23, hours))
    minutes = max(0, min(59, minutes))
    return f"{hours:02d}:{minutes:02d}"


def parse_time(value: str) -> tuple[int, int]:
    norm = _normalise_time(value)
    hours, _, minutes = norm.partition(":")
    return int(hours), int(minutes)


def is_valid_time(value: str) -> bool:
    text = (value or "").strip().replace(".", ":")
    parts = text.split(":")
    if not parts or not parts[0].isdigit():
        return False
    if len(parts) > 1 and not parts[1].isdigit():
        return False
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return False
    return 0 <= hours <= 23 and 0 <= minutes <= 59


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
