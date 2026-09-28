"""Mailer factory: builds a configured :class:`Mailer` from settings + secrets."""

from __future__ import annotations

from ..mailer import Mailer, SmtpConfig
from ..secrets_store import SecretsStore
from ..settings_service import Settings


def smtp_config_from_settings(
    settings: Settings, secrets: SecretsStore | None = None, timeout: int = 30
) -> SmtpConfig:
    password = ""
    if secrets is not None:
        password = secrets.get("SMTP_PASSWORD", "") or ""
    return SmtpConfig(
        host=(settings.get("smtp_host") or "").strip(),
        port=settings.smtp_port,
        encryption=settings.smtp_encryption,
        username=(settings.get("smtp_username") or "").strip(),
        password=password,
        from_name=settings.get("mail_from_name") or "Verzehrabrechnung",
        from_address=(settings.get("mail_from_address") or "").strip(),
        reply_to=(settings.get("mail_reply_to") or "").strip(),
        timeout=timeout,
    )


def build_mailer(
    settings: Settings, secrets: SecretsStore | None = None, timeout: int = 30
) -> Mailer:
    return Mailer(smtp_config_from_settings(settings, secrets, timeout))


def is_email_configured(settings: Settings) -> bool:
    return bool(
        (settings.get("smtp_host") or "").strip()
        and (settings.get("mail_from_address") or "").strip()
    )
