"""app/services/reminder_guard.py — идемпотентная отправка напоминаний.

Проблема: если бот упал между mark() и send(), после рестарта
пользователь получит дублирующее уведомление (флаг не проставлен).

Решение: сначала проставляем флаг в БД (BEGIN IMMEDIATE),
только потом пытаемся отправить. Если send упал — флаг уже стоит
и повторной отправки не будет. Небольшой риск «тихого» пропуска
уведомления лучше, чем спам пользователю.
"""
from __future__ import annotations

import logging
from typing import Callable, Awaitable

logger = logging.getLogger("salon.reminder")


async def send_with_mark(
    *,
    bot,
    booking,
    field: str,
    mark_fn: Callable[[int, str], None],
    send_fn: Callable[[], Awaitable[None]],
) -> bool:
    """
    1. Маркируем запись (field) в БД — атомарно.
    2. Только потом отправляем сообщение.

    Если отправка упала — запись уже помечена, повтор не придёт.
    Возвращает True если сообщение отправлено успешно.
    """
    try:
        mark_fn(booking.id, field)
    except Exception:
        logger.exception(
            "send_with_mark: cannot mark bid=%s field=%s", booking.id, field
        )
        return False  # лучше пропустить, чем дублировать

    try:
        await send_fn()
        return True
    except Exception:
        logger.warning(
            "send_with_mark: send failed bid=%s field=%s (already marked)",
            booking.id, field,
        )
        return False
