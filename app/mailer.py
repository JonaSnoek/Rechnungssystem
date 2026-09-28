"""SMTP transport.

Sends multipart/alternative mails (plain text + HTML) and never raises a
raw ``smtplib`` exception at the call site: failures are returned as
:class:`SendResult` objects so the billing service can keep the bookings open
and allow a retry.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class MailConfigError(RuntimeError):
    """SMTP settings are incomplete or invalid."""


@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    encryption: str = "starttls"  # none | starttls | ssl
    username: str = ""
    password: str = ""
    from_name: str = "Verzehrabrechnung"
    from_address: str = ""
    reply_to: str = ""
    timeout: int = DEFAULT_TIMEOUT

    @property
    def sender(self) -> str:
        return formataddr((self.from_name or "Absender", self.from_address))

    def validate(self) -> None:
        if not self.host or not self.host.strip():
            raise MailConfigError("SMTP-Server ist nicht konfiguriert")
        if not self.from_address or "@" not in self.from_address:
            raise MailConfigError("Absender-E-Mail-Adresse ist nicht konfiguriert")
        if not (1 <= int(self.port) <= 65535):
            raise MailConfigError("SMTP-Port ist ungueltig")


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    code: int = 0

    def __bool__(self) -> bool:
        return self.ok


class Mailer:
    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    # -- transport ---------------------------------------------------------
    def _connect(self):
        cfg = self.config
        cfg.validate()
        try:
            if cfg.encryption == "ssl":
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(
                    cfg.host,
                    cfg.port,
                    timeout=cfg.timeout,
                    context=context,
                )
            else:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailConfigError(f"Verbindung zu {cfg.host}:{cfg.port} fehlgeschlagen: {exc}") from exc

        try:
            server.ehlo()
            if cfg.encryption == "starttls":
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            if cfg.username:
                server.login(cfg.username, cfg.password or "")
        except smtplib.SMTPAuthenticationError as exc:
            raise MailConfigError(f"SMTP-Anmeldung fehlgeschlagen: {exc.smtp_error.decode(errors='replace') if isinstance(exc.smtp_error, bytes) else exc.smtp_error}") from exc
        except (smtplib.SMTPException, ssl.SSLError) as exc:
            raise MailConfigError(f"SMTP-Verbindungsfehler: {exc}") from exc
        except socket.timeout as exc:
            raise MailConfigError("SMTP-Zeitueberschreitung") from exc
        return server

    # -- public ------------------------------------------------------------
    def send(
        self,
        *,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> SendResult:
        if not to or "@" not in to:
            return SendResult(ok=False, error="Ungueltige Empfaengeradresse")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.sender
        message["To"] = to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=_domain_of(to))
        if self.config.reply_to:
            message["Reply-To"] = self.config.reply_to

        message.set_content(text_body or "", subtype="plain", charset="utf-8")
        if html_body:
            message.add_alternative(html_body, subtype="html", charset="utf-8")

        try:
            server = self._connect()
        except MailConfigError as exc:
            return SendResult(ok=False, error=str(exc))

        try:
            refused = server.send_message(message)
            if refused:
                return SendResult(
                    ok=False,
                    error="Empfanger abgelehnt: " + ", ".join(sorted(refused)),
                )
            log.info(
                "E-Mail versendet an %s (Betreff: %s)", _mask_email(to), subject
            )
            return SendResult(ok=True, message_id=message["Message-ID"])
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(ok=False, error="Empfaenger abgelehnt", code=getattr(exc, "smtp_code", 0) or 0)
        except (smtplib.SMTPException, socket.timeout, OSError) as exc:
            return SendResult(ok=False, error=_clean_error(exc))
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - best effort cleanup
                try:
                    server.close()
                except Exception:  # noqa: BLE001
                    pass

    def test_connection(self) -> tuple[bool, str]:
        try:
            server = self._connect()
        except MailConfigError as exc:
            return False, str(exc)
        try:
            server.noop()
            return True, "SMTP-Verbindung erfolgreich aufgebaut"
        except (smtplib.SMTPException, OSError) as exc:
            return False, _clean_error(exc)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


def _domain_of(email: str) -> str:
    _, _, domain = email.partition("@")
    return domain or "localhost"


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = local[:1] + "*"
    else:
        masked = local[:2] + "*" * max(1, len(local) - 2)
    return f"{masked}@{domain}"


def _clean_error(exc: Exception) -> str:
    """Never leak credentials from SMTP exception text."""
    text = str(exc)
    if "password" in text.lower():
        return f"{type(exc).__name__}: Zugangsdaten abgelehnt"
    return f"{type(exc).__name__}: {text[:400]}"
