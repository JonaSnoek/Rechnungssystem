"""Money handling: all amounts are stored and computed as integer cents.

Floating point is never used for money. Parsing goes through ``Decimal``,
every value is persisted as ``int`` (minor units) and only converted to a
display string at the very edge of the application.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Union

CENTS = 100

# Number of decimals the system supports per currency.
CURRENCY_EXPONENT = {
    "EUR": 2,
    "USD": 2,
    "GBP": 2,
    "CHF": 2,
    "SEK": 2,
    "NOK": 2,
    "DKK": 2,
    "PLN": 2,
    "CZK": 2,
    "AUD": 2,
    "CAD": 2,
    "JPY": 0,
}

SUPPORTED_CURRENCIES = sorted(CURRENCY_EXPONENT.keys())

_SYMBOLS = {
    "EUR": "\u20ac",
    "USD": "$",
    "GBP": "\u00a3",
    "CHF": "CHF",
    "JPY": "\u00a5",
}


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a money amount."""


def exponent(currency: str) -> int:
    return CURRENCY_EXPONENT.get((currency or "EUR").upper(), 2)


def parse_to_cents(value: Any, currency: str = "EUR", *, allow_negative: bool = False) -> int:
    """Parse user input into integer cents.

    Accepts ``int`` (already cents), ``str`` ("4,40", "4.40", "4", "4.4"),
    ``float`` (converted via str to avoid binary artefacts) and ``Decimal``.
    Raises :class:`MoneyError` for non numeric or too precise input.

    Negative amounts are rejected unless ``allow_negative`` is set. Only the
    administrative opening balance uses that, where a debt is a legitimate value.
    """
    if value is None or value == "":
        raise MoneyError("Betrag fehlt")

    exp = exponent(currency)
    factor = CENTS if exp == 2 else 1

    if isinstance(value, bool):
        raise MoneyError("Ungueltiger Betrag")

    if isinstance(value, int):
        return value * factor

    if isinstance(value, float):
        value = repr(value)

    if isinstance(value, Decimal):
        dec = value
    else:
        text = str(value).strip()
        if not text:
            raise MoneyError("Betrag fehlt")
        text = text.replace("\u00a0", "").replace(" ", "").replace("'", "")
        # German decimal comma
        if "," in text and "." in text:
            # 1.234,56 vs 1,234.56 -> the last separator is the decimal mark
            if text.rindex(",") > text.rindex("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        text = text.replace("\u20ac", "").replace("EUR", "").strip()
        if not text:
            raise MoneyError("Betrag fehlt")
        try:
            dec = Decimal(text)
        except (InvalidOperation, ValueError):
            raise MoneyError("Betrag ist keine gueltige Zahl")

    if not dec.is_finite():
        raise MoneyError("Betrag ist keine gueltige Zahl")

    if dec < 0 and not allow_negative:
        raise MoneyError("Betrag darf nicht negativ sein")

    step = Decimal(1).scaleb(-exp)
    quantized = dec.quantize(step, rounding=ROUND_HALF_UP)
    if quantized != dec:
        raise MoneyError(
            f"Betrag hat zu viele Nachkommastellen (maximal {exp})"
        )

    return int(quantized * (10**exp))


def cents_to_decimal(cents: int, currency: str = "EUR") -> Decimal:
    exp = exponent(currency)
    return (Decimal(int(cents)) / (10**exp)).quantize(Decimal(1).scaleb(-exp))


def format_cents(cents: int, currency: str = "EUR", *, symbol: bool = True) -> str:
    """Human readable amount using German formatting: ``4,40 \u20ac``."""
    exp = exponent(currency)
    if exp == 0:
        text = f"{int(cents):,}".replace(",", ".")
        return f"{text} {currency}" if symbol else text
    negative = cents < 0
    value = abs(int(cents))
    whole, fraction = divmod(value, CENTS)
    text = f"{whole:,}".replace(",", ".") + "," + f"{fraction:02d}"
    if negative:
        text = "-" + text
    if not symbol:
        return text
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return f"{text} \u20ac"
    return f"{text} {_SYMBOLS.get(cur, cur)}"


def format_decimal_input(cents: int, currency: str = "EUR") -> str:
    """Value for ``<input type=number>`` -> ``4.40``."""
    exp = exponent(currency)
    if exp == 0:
        return str(int(cents))
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    whole, fraction = divmod(value, CENTS)
    return f"{sign}{whole}.{fraction:02d}"


def paypal_amount(cents: int, currency: str = "EUR") -> str:
    """Amount string for a PayPal.Me URL: ``4.40`` (dot, no thousands sep)."""
    if cents < 0:
        raise MoneyError("Betrag darf nicht negativ sein")
    exp = exponent(currency)
    if exp == 0:
        return str(int(cents))
    whole, fraction = divmod(int(cents), CENTS)
    return f"{whole}.{fraction:02d}"


def multiply_cents(unit_cents: int, quantity: int) -> int:
    if quantity < 0:
        raise MoneyError("Menge darf nicht negativ sein")
    return int(unit_cents) * int(quantity)


def sum_cents(values) -> int:
    return int(sum(int(v) for v in values))


Number = Union[int, float, str, Decimal]
