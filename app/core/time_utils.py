"""app/core/time_utils.py"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pytz

from app.core.settings import settings

_TZ  = pytz.timezone(settings.TIMEZONE)
UTC  = timezone.utc
_RU  = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_RU_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_RU_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS = ["января","февраля","марта","апреля","мая","июня",
           "июля","августа","сентября","октября","ноября","декабря"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def local_now() -> datetime:
    return datetime.now(_TZ)


def local_today() -> date:
    return local_now().date()


def to_utc(date_str: str, time_str: str) -> datetime:
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return _TZ.localize(naive).astimezone(pytz.utc)


def seconds_until(date_str: str, time_str: str) -> float:
    return (to_utc(date_str, time_str) - utcnow()).total_seconds()


def booking_dates() -> list[str]:
    today = local_today()
    return [(today + timedelta(days=i)).isoformat()
            for i in range(1, settings.BOOKING_WINDOW_DAYS + 1)]


def return_notify_date() -> str:
    """Дата визита, после которого прошло RETURN_NOTIFY_DAYS дней."""
    return (local_today() - timedelta(days=settings.RETURN_NOTIFY_DAYS)).isoformat()


def review_cutoff_utc() -> str:
    """UTC-момент: REVIEW_DELAY_HOURS часов назад.
    Записи, завершённые до этого момента, уже «созрели» для отзыва."""
    dt = utcnow() - timedelta(hours=settings.REVIEW_DELAY_HOURS)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_plus_hours(hours: int) -> str:
    return (utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_date(date_str: str) -> str:
    d = date.fromisoformat(date_str)
    return f"{d.strftime('%d.%m.%Y')} ({_RU[d.weekday()]})"


def fmt_date_btn(date_str: str) -> str:
    """Формат для кнопки выбора даты: 'Пятница, 13 марта'."""
    d = date.fromisoformat(date_str)
    day = _RU_FULL[d.weekday()].capitalize()
    return f"{day}, {d.day} {_MONTHS[d.month - 1]}"


def fmt_slot(start: str) -> str:
    return start


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
