"""PayPal.Me link generation.

The PayPal.Me username is *never* hard coded here. It always comes from the
application settings, which are edited through the web UI (or the setup
wizard) and backed by the database.

Resulting URL shape::

    https://www.paypal.me/<USERNAME>/<AMOUNT><CURRENCY>
    https://www.paypal.me/JONASNOEK1/4.40EUR
"""

from __future__ import annotations

import re
from urllib.parse import quote

from .money import MoneyError, paypal_amount

DEFAULT_BASE_URL = "https://www.paypal.me"

# PayPal.Me handles: letters, digits, dot, dash, underscore.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,25}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class PayPalLinkError(ValueError):
    """Raised when a link cannot be built from the given input."""


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise PayPalLinkError("PayPal.Me-Benutzername fehlt")
    if not _USERNAME_RE.match(username):
        raise PayPalLinkError(
            "Ungueltiger PayPal.Me-Benutzername (erlaubt: Buchstaben, Ziffern, . - _)"
        )
    return username


def validate_currency(currency: str) -> str:
    currency = (currency or "").strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise PayPalLinkError("Ungueltige Waehrung (3-stelliger ISO-Code)")
    return currency


def build_link(
    username: str,
    amount_cents: int,
    currency: str = "EUR",
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Build a PayPal.Me payment request link.

    Raises :class:`PayPalLinkError` for invalid usernames/currencies and
    :class:`MoneyError` for negative amounts. The amount is re-validated here
    so a link can never carry a malformed or negative value.
    """
    username = validate_username(username)
    currency = validate_currency(currency)
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    try:
        cents = int(amount_cents)
    except (TypeError, ValueError):
        raise MoneyError("Ungueltiger Betrag")

    if cents < 0:
        raise MoneyError("Betrag darf nicht negativ sein")

    amount = paypal_amount(cents, currency)
    return f"{base_url}/{quote(username, safe='')}/{amount}{currency}"


def is_valid_link(url: str) -> bool:
    """Cheap structural validation used before sending an invoice."""
    if not url or not isinstance(url, str):
        return False
    match = re.match(
        r"^https?://[A-Za-z0-9.\-]+/([A-Za-z0-9._\-]{1,25})/(\d+(?:\.\d{1,2})?)([A-Z]{3})$",
        url.strip(),
    )
    if not match:
        return False
    return float(match.group(2)) > 0
