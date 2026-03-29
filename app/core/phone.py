"""app/core/phone.py — валидация и нормализация RF-номеров.

Единственный источник правды для всего проекта.
Импортируется и сервисным слоем, и telegram-хелперами.
"""
from __future__ import annotations

import re

# Принимает: +7XXXXXXXXXX, 7XXXXXXXXXX, 8XXXXXXXXXX, 9XXXXXXXXXX
# с любыми разделителями — пробелы, тире, скобки.
_PHONE_RE = re.compile(r"^(\+7|7|8)?\s*\(?\d{3}\)?\s*\d{3}[\s\-]?\d{2}[\s\-]?\d{2}$")


def validate_phone(raw: str) -> bool:
    """True если номер является российским мобильным (7/8 + 10 цифр)."""
    cleaned = re.sub(r"[\s\-()]", "", raw).strip()
    digits  = re.sub(r"\D", "", cleaned)
    return bool(_PHONE_RE.match(cleaned)) and 10 <= len(digits) <= 11


def normalize_phone(raw: str) -> str:
    """Нормализует любой валидный RF-номер к виду +7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:        # 9001234567  → 79001234567
        digits = "7" + digits
    elif digits.startswith("8"): # 89001234567 → 79001234567
        digits = "7" + digits[1:]
    return ("+" + digits) if digits.startswith("7") else ("+7" + digits)
