from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_PRICE_RE = re.compile(r"^\d+(?:[.,]\d{1,2})?$")


def parse_eur_input_to_cents(raw: str) -> int | None:
    text = (raw or "").strip().replace(" ", "")
    if not _PRICE_RE.match(text):
        return None
    normalized = text.replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    cents = int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents if cents > 0 else None


def format_eur(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    euros = value // 100
    frac = value % 100
    if frac == 0:
        return f"{sign}{euros:,} €"
    return f"{sign}{euros:,}.{frac:02d} €"
