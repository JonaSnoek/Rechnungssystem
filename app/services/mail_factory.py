"""Mailer factory: builds a configured :class:`Mailer` from settings + secrets."""

from __future__ import annotations

import re

from ..mailer import Mailer, SmtpConfig
from ..secrets_store import SecretsStore
from ..settings_service import Settings

# Gmail, Outlook und andere Anbieter zeigen App-Passwoerter in Vierergruppen
# an ("abcd efgh ijkl mnop"). Wird so ein Wert eingefuegt, schlaegt die
# Anmeldung mit "Invalid username or password" fehl, obwohl das Passwort
# korrekt ist. Deshalb werden Leerzeichen entfernt.
_WHITESPACE = re.compile(r"\s+")


def normalize_password(value: str | None) -> str:
    """Strip surrounding and internal whitespace from an SMTP password.

    Gmail/Outlook app passwords are displayed in groups of four separated by
    spaces. Copy-pasting that display form makes authentication fail with a
    misleading error, so all whitespace is removed.
    """
    if not value:
        return ""
    return _WHITESPACE.sub("", value)


def smtp_config_from_settings(
    settings: Settings, secrets: SecretsStore | None = None, timeout: int = 30
) -> SmtpConfig:
    password = ""
    if secrets is not None:
        password = normalize_password(secrets.get("SMTP_PASSWORD", ""))
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
