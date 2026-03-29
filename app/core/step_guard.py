"""app/core/step_guard.py — таймаут незавершённых шагов ввода.

Если пользователь начал ввод (onboarding, заметка, редактирование)
и не завершил его за STEP_TIMEOUT_SECONDS — шаг сбрасывается и
клиент получает вежливое сообщение.

Привязывается к StepDispatcher: каждый раз, когда устанавливается step,
записывается метка времени. dispatch() проверяет её перед вызовом хендлера.
"""
from __future__ import annotations

import time
import logging

logger = logging.getLogger("salon.step_guard")

# Сколько секунд бот ждёт завершения шага (10 минут)
STEP_TIMEOUT_SECONDS: int = 600

_TIMEOUT_MSG = (
    "⏱ Похоже, ввод занял слишком долго — я сбросил незавершённый шаг.\n\n"
    "Нажмите «🏠 Главное меню» и начните заново."
)


def set_step(user_data: dict, step: str | None) -> None:
    """Устанавливает текущий шаг и обновляет метку времени."""
    user_data["step"] = step
    if step is not None:
        user_data["step_ts"] = time.monotonic()
    else:
        user_data.pop("step_ts", None)


def is_step_expired(user_data: dict) -> bool:
    """True если шаг установлен, но истёк таймаут."""
    if not user_data.get("step"):
        return False
    ts = user_data.get("step_ts")
    if ts is None:
        return False
    return (time.monotonic() - ts) > STEP_TIMEOUT_SECONDS


async def check_and_reset_if_expired(update, context) -> bool:
    """Проверяет таймаут шага. Если истёк — сбрасывает и отвечает пользователю.

    Returns True если шаг истёк (вызывающий должен прерваться).
    """
    if not is_step_expired(context.user_data):
        return False

    old_step = context.user_data.get("step", "?")
    set_step(context.user_data, None)
    logger.info(
        "step_guard: expired step=%r uid=%s",
        old_step,
        update.effective_user.id if update.effective_user else "?",
    )
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
        ]])
        msg = (update.callback_query.message
               if update.callback_query else update.message)
        if msg:
            await msg.reply_text(_TIMEOUT_MSG, reply_markup=markup)
    except Exception as e:
        logger.error("step_guard reply failed: %s", e)
    return True
