"""Input validation helpers shared by all views."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from .money import MoneyError, exponent, parse_to_cents

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")

RESERVED_USERNAMES = {"admin", "root", "setup", "logout", "static", "test"}


class ValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class Validator:
    """Collects field errors so a form can report everything at once."""

    def __init__(self) -> None:
        self.errors: dict[str, str] = {}
        self.values: dict[str, object] = {}

    def add(self, field: str, message: str) -> None:
        self.errors.setdefault(field, message)

    @property
    def ok(self) -> bool:
        return not self.errors

    def first_error(self) -> str:
        return next(iter(self.errors.values()), "")

    def raise_if_invalid(self) -> None:
        if self.errors:
            field, message = next(iter(self.errors.items()))
            raise ValidationError(field, message)

    # -- fields ------------------------------------------------------------
    def required_text(
        self, field: str, raw, *, min_len: int = 1, max_len: int = 255, label: str = ""
    ) -> str:
        name = label or field
        text = unicodedata.normalize("NFC", (raw or "").strip())
        if not text:
            self.add(field, f"{name} ist erforderlich")
            return ""
        if len(text) < min_len:
            self.add(field, f"{name} muss mindestens {min_len} Zeichen haben")
        if len(text) > max_len:
            self.add(field, f"{name} darf hoechstens {max_len} Zeichen haben")
        self.values[field] = text
        return text

    def optional_text(
        self, field: str, raw, *, max_len: int = 2000, label: str = ""
    ) -> str | None:
        name = label or field
        text = unicodedata.normalize("NFC", (raw or "").strip())
        if len(text) > max_len:
            self.add(field, f"{name} darf hoechstens {max_len} Zeichen haben")
            return None
        if not text:
            return None
        self.values[field] = text
        return text

    def email(self, field: str, raw, *, required: bool = True) -> str:
        text = (raw or "").strip().lower()
        if not text:
            if required:
                self.add(field, "E-Mail-Adresse ist erforderlich")
            return ""
        if len(text) > 254:
            self.add(field, "E-Mail-Adresse ist zu lang")
            return text
        if not EMAIL_RE.match(text) or ".." in text:
            self.add(field, "Ungueltige E-Mail-Adresse")
            return text
        local, _, domain = text.partition("@")
        if local.endswith(".") or len(local) > 64:
            self.add(field, "Ungueltige E-Mail-Adresse")
            return text
        if not any(part.isalpha() for part in domain.split(".")) or domain.endswith("."):
            self.add(field, "Ungueltige E-Mail-Adresse")
            return text
        self.values[field] = text
        return text

    def username(self, field: str, raw) -> str:
        text = (raw or "").strip()
        if not text:
            self.add(field, "Benutzername ist erforderlich")
            return ""
        if not USERNAME_RE.match(text):
            self.add(
                field,
                "Benutzername: 3-64 Zeichen, erlaubt sind Buchstaben, Ziffern, . _ -",
            )
            return text
        if text.lower() in RESERVED_USERNAMES:
            self.add(field, "Dieser Benutzername ist reserviert")
            return text
        self.values[field] = text
        return text

    def money(
        self,
        field: str,
        raw,
        *,
        currency: str = "EUR",
        required: bool = True,
        allow_zero: bool = False,
        allow_negative: bool = False,
    ) -> int:
        try:
            cents = parse_to_cents(raw, currency, allow_negative=allow_negative)
        except MoneyError as exc:
            self.add(field, str(exc))
            return 0
        if cents is None:
            self.add(field, "Betrag ist erforderlich")
            return 0
        if cents < 0 and not allow_negative:
            self.add(field, "Betrag darf nicht negativ sein")
            return 0
        if not allow_zero and cents == 0:
            self.add(field, "Betrag muss groesser als 0 sein")
            return 0
        self.values[field] = cents
        return cents

    def try_money(self, field: str, raw, *, currency: str = "EUR", allow_zero: bool = True) -> int | None:
        try:
            cents = parse_to_cents(raw, currency)
        except MoneyError as exc:
            self.add(field, str(exc))
            return None
        if cents < 0:
            self.add(field, "Betrag darf nicht negativ sein")
            return None
        if not allow_zero and cents == 0:
            self.add(field, "Betrag muss groesser als 0 sein")
            return None
        self.values[field] = cents
        return cents

    def integer(
        self, field: str, raw, *, minimum: int | None = None, maximum: int | None = None, required: bool = True, default: int = 0
    ) -> int:
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            if required:
                self.add(field, "Wert ist erforderlich")
            return default
        try:
            value = int(text)
        except ValueError:
            self.add(field, "Bitte eine ganze Zahl eingeben")
            return default
        if minimum is not None and value < minimum:
            self.add(field, f"Wert muss mindestens {minimum} sein")
            return default
        if maximum is not None and value > maximum:
            self.add(field, f"Wert darf hoechstens {maximum} sein")
            return default
        self.values[field] = value
        return value

    def time_of_day(self, field: str, raw) -> str:
        text = (raw or "").strip().replace(".", ":")
        if not TIME_RE.match(text):
            self.add(field, "Bitte eine gueltige Uhrzeit im Format HH:MM angeben")
            return "17:00"
        self.values[field] = f"{int(text[:2]):02d}:{int(text[3:5]):02d}"
        return str(self.values[field])

    def date(self, field: str, raw, *, required: bool = False) -> date | None:
        text = (raw or "").strip()
        if not text:
            if required:
                self.add(field, "Datum ist erforderlich")
            return None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                value = datetime.strptime(text, fmt).date()
                self.values[field] = value
                return value
            except ValueError:
                continue
        self.add(field, "Ungueltiges Datum (erwartet: JJJJ-MM-TT)")
        return None

    def choice(self, field: str, raw, options: list[str], *, default: str | None = None) -> str:
        text = (raw or "").strip()
        if not text and default is not None:
            return default
        if text not in options:
            self.add(field, "Ungueltige Auswahl")
            return default or (options[0] if options else "")
        self.values[field] = text
        return text

    def boolean(self, field: str, raw) -> bool:
        value = str(raw).strip().lower() in {"1", "true", "on", "yes", "ja", "x"}
        self.values[field] = value
        return value

    def timezone(self, field: str, raw) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        text = (raw or "").strip() or "Europe/Berlin"
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            self.add(field, f"Unbekannte Zeitzone: {text}")
            return "Europe/Berlin"
        self.values[field] = text
        return text

    def currency_code(self, field: str, raw, *, default: str = "EUR") -> str:
        from .money import SUPPORTED_CURRENCIES

        text = (raw or "").strip().upper()
        if text not in SUPPORTED_CURRENCIES:
            self.add(
                field,
                "Ungueltige Waehrung. Erlaubt: " + ", ".join(SUPPORTED_CURRENCIES),
            )
            return default
        self.values[field] = text
        return text

    def paypal_username(self, field: str, raw, *, required: bool = True) -> str:
        from .paypal import PayPalLinkError, validate_username

        text = (raw or "").strip()
        if not text:
            if required:
                self.add(field, "PayPal.Me-Benutzername ist erforderlich")
            return ""
        try:
            value = validate_username(text)
        except PayPalLinkError as exc:
            self.add(field, str(exc))
            return text
        self.values[field] = value
        return value

    def max_decimals(self, currency: str) -> int:
        return exponent(currency)
