"""app/models/domain.py"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date, datetime
from enum import Enum
from typing import Optional


def _today_iso() -> str:
    try:
        from app.core.time_utils import local_today
        return local_today().isoformat()
    except Exception:
        return _date.today().isoformat()


class BookingStatus(str, Enum):
    ACTIVE    = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Client:
    user_id:      int
    username:     Optional[str]
    first_name:   str
    name:         str
    phone:        str
    registered_at: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        return self.name or self.first_name or "Клиент"

    @property
    def is_registered(self) -> bool:
        return bool(self.name and self.phone)


@dataclass(frozen=True)
class Booking:
    id:              int
    user_id:         int
    service:         str
    price:           int
    date:            str
    time:            str
    status:          BookingStatus = BookingStatus.ACTIVE
    notified_1h:     bool = False
    notified_24h:    bool = False
    notified_return: bool = False
    review_sent:     bool = False
    created_at_utc:  Optional[datetime] = None

    @property
    def end_time(self) -> str:
        from app.core.settings import settings
        return settings.TIME_SLOTS.get(self.time, "")


@dataclass(frozen=True)
class RescheduleRequest:
    id:         int
    booking_id: int
    new_date:   str
    new_time:   str
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class Note:
    id:         int
    user_id:    int
    author_id:  int
    text:       str
    created_at: Optional[datetime]


@dataclass(frozen=True)
class Review:
    id:           int
    user_id:      int
    booking_id:   int
    rating:       int
    text:         str
    photo_file_id: str          # file_id из Telegram, пусто если фото нет
    created_at:   Optional[datetime]


@dataclass
class BookingResult:
    ok:      bool
    booking: Optional[Booking] = None
    error:   str = ""


@dataclass
class SlotInfo:
    date:       str
    free_times: list[str]
    is_blocked: bool = False

    @property
    def is_full(self) -> bool:
        return not self.is_blocked and not self.free_times

    @property
    def booked_count(self) -> int:
        from app.core.settings import settings
        return settings.MAX_SLOTS_PER_DAY - len(self.free_times)


@dataclass
class Stats:
    total_bookings:  int
    total_revenue:   int
    month_bookings:  int
    month_revenue:   int
    cancelled:       int
    unique_clients:  int
    avg_rating:      Optional[float]
    month_label:     str = ""


@dataclass
class ClientCard:
    client:      Client
    bookings:    list[Booking]
    notes:       list[Note]
    reviews:     list[Review]     # все отзывы клиента
    total_spent: int
    last_visit:  Optional[str]

    @property
    def visits(self) -> int:
        today = _today_iso()
        return len([
            b for b in self.bookings
            if b.status in (BookingStatus.ACTIVE, BookingStatus.COMPLETED)
            and b.date < today
        ])

    @property
    def upcoming(self) -> int:
        today = _today_iso()
        return len([
            b for b in self.bookings
            if b.status == BookingStatus.ACTIVE and b.date >= today
        ])
