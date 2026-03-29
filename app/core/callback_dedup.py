"""app/core/callback_dedup.py — защита от двойных callback'ов.

Telegram может прислать один и тот же callback_query дважды (retry,
сетевой сбой). Без дедупликации создаётся две записи / два отмены и т.п.

Решение: хранить в памяти set ID последних N callback_query_id с TTL.
Структура: dict[callback_id, monotonic_ts]. Чистится ленивo при каждом вызове.
"""
from __future__ import annotations

import time
import logging
from typing import Optional

logger = logging.getLogger("salon.dedup")

# Сколько секунд держим callback_id в памяти (Telegram retry window ~60 сек)
_TTL = 90
# Максимальный размер кэша (защита от OOM при большом трафике)
_MAX = 2000

_seen: dict[str, float] = {}


def _evict() -> None:
    """Удаляет устаревшие записи (ленивая чистка)."""
    now = time.monotonic()
    expired = [k for k, ts in _seen.items() if now - ts > _TTL]
    for k in expired:
        del _seen[k]
    # Если всё равно много — обрезаем самые старые
    if len(_seen) > _MAX:
        oldest = sorted(_seen, key=_seen.__getitem__)[:_MAX // 4]
        for k in oldest:
            del _seen[k]


def is_duplicate(callback_id: Optional[str]) -> bool:
    """True если этот callback уже обрабатывался в последние TTL секунд."""
    if not callback_id:
        return False
    _evict()
    if callback_id in _seen:
        logger.debug("dedup: duplicate callback_id=%s", callback_id)
        return True
    _seen[callback_id] = time.monotonic()
    return False
