"""
app/services/services.py â€” ÑÐµÑ€Ð²Ð¸ÑÐ½Ñ‹Ð¹ ÑÐ»Ð¾Ð¹.

Ð˜ÑÐ¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð¸Ñ:
  - NoteService Ð²Ñ‹Ð´ÐµÐ»ÐµÐ½ Ð² Ð¾Ñ‚Ð´ÐµÐ»ÑŒÐ½Ñ‹Ð¹ ÐºÐ»Ð°ÑÑ (Ð±Ñ‹Ð» ÑÐºÐ»ÐµÐµÐ½ Ñ ServiceService)
  - BookingService.create() Ð±ÐµÑ€Ñ‘Ñ‚ Ñ†ÐµÐ½Ñƒ Ð¸Ð· Ð‘Ð”, Ð° Ð½Ðµ settings.SERVICES
  - BookingService.free_slots() / available_dates() Ñ‡Ð¸Ñ‚Ð°ÑŽÑ‚ TIME_SLOTS Ð¸Ð· settings
    (Ð¾Ð½Ð¸ ÑƒÐ¶Ðµ ÑÐ¸Ð½Ñ…Ñ€Ð¾Ð½Ð¸Ð·Ð¸Ñ€Ð¾Ð²Ð°Ð½Ñ‹ Ñ Ð‘Ð” Ñ‡ÐµÑ€ÐµÐ· ServiceService._sync)
  - review_candidates() Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ Ð´Ð°Ñ‚Ñƒ Ð²Ð¸Ð·Ð¸Ñ‚Ð°, Ð° Ð½Ðµ created_at_utc Ð·Ð°Ð¿Ð¸ÑÐ¸
  - expire_reschedules() ÑƒÐ´Ð°Ð»ÑÐµÑ‚ Ð²ÑÐµ ÑÑ€Ð°Ð·Ñƒ Ð² Ð¾Ð´Ð½Ð¾Ð¹ Ñ‚Ñ€Ð°Ð½Ð·Ð°ÐºÑ†Ð¸Ð¸
  - ClientService.set_profile() Ð²Ð°Ð»Ð¸Ð´Ð¸Ñ€ÑƒÐµÑ‚ Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚ Ñ‚ÐµÐ»ÐµÑ„Ð¾Ð½Ð° (RF, 10-11 Ñ†Ð¸Ñ„Ñ€)
"""
from __future__ import annotations

import logging
import re
from datetime import date as dt_date, timedelta
from typing import Optional

# Ð’Ð°Ð»Ð¸Ð´Ð°Ñ†Ð¸Ñ Ð¸ Ð½Ð¾Ñ€Ð¼Ð°Ð»Ð¸Ð·Ð°Ñ†Ð¸Ñ Ñ‚ÐµÐ»ÐµÑ„Ð¾Ð½Ð¾Ð² â€” ÐµÐ´Ð¸Ð½ÑÑ‚Ð²ÐµÐ½Ð½Ñ‹Ð¹ Ð¸ÑÑ‚Ð¾Ñ‡Ð½Ð¸Ðº Ð¿Ñ€Ð°Ð²Ð´Ñ‹
from app.core.phone import normalize_phone as _normalize_phone
from app.core.phone import validate_phone as _validate_phone

from app.core.database import atomic, get_db
from app.core.settings import settings
from app.core.time_utils import (
    booking_dates, local_now, local_today,
    return_notify_date, review_cutoff_utc,
    seconds_until, utc_plus_hours,
)
from app.models.domain import (
    Booking, BookingResult, BookingStatus, Client, ClientCard,
    BookingEvent, Note, RescheduleRequest, Review, SlotInfo, Stats,
)
from app.repositories.repo import (
    BlacklistRepo, BlockedDayRepo, BlockedSlotRepo, BookingRepo, ClientRepo,
    BookingEventRepo, NoteRepo, PortfolioRepo, RescheduleRepo, ReviewRepo, ServiceRepo, TimeSlotRepo,
    WeeklyScheduleRepo,
)
from app.services.excel_worker import ExcelRow, enqueue

logger = logging.getLogger("salon.svc")

def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 19:
        return many
    r = n % 10
    if r == 1:      return one
    if 2 <= r <= 4: return few
    return many


_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _normalize_hhmm(value: str) -> str | None:
    raw = (value or "").strip().replace(".", ":")
    if raw and raw.count(":") == 1:
        left, right = raw.split(":")
        if left.isdigit() and right.isdigit():
            raw = f"{int(left):02d}:{int(right):02d}"
    if not _TIME_RE.match(raw):
        return None
    hh = int(raw[:2])
    mm = int(raw[3:])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return raw


def _normalize_period(value: str) -> tuple[str, str] | None:
    raw = (value or "").strip().replace("â€“", "-").replace("â€”", "-")
    if "-" not in raw:
        return None
    left, right = raw.split("-", 1)
    start = _normalize_hhmm(left)
    end = _normalize_hhmm(right)
    if not start or not end:
        return None
    if start >= end:
        return None
    return start, end


def _weekday_from_date(date_str: str) -> int:
    return dt_date.fromisoformat(date_str).isoweekday()


class WeeklyScheduleService:

    def list_days(self) -> list[dict]:
        with get_db() as db:
            return WeeklyScheduleRepo.all_day_templates(db)

    def get_day_template(self, weekday: int) -> dict:
        if weekday < 1 or weekday > 7:
            raise ValueError("weekday must be 1..7")
        with get_db() as db:
            return WeeklyScheduleRepo.get_day_template(db, weekday)

    def add_weekly_time(self, weekday: int, hhmm: str) -> tuple[bool, str]:
        if weekday < 1 or weekday > 7:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ Ð½ÐµÐ´ÐµÐ»Ð¸."
        norm = _normalize_hhmm(hhmm)
        if not norm:
            return False, "Ð¤Ð¾Ñ€Ð¼Ð°Ñ‚ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð¸: Ð§Ð§:ÐœÐœ."
        with atomic() as db:
            ok = WeeklyScheduleRepo.add_time(db, weekday, norm)
        if not ok:
            return False, f"Ð’Ñ€ÐµÐ¼Ñ {norm} ÑƒÐ¶Ðµ ÐµÑÑ‚ÑŒ Ð² ÑˆÐ°Ð±Ð»Ð¾Ð½Ðµ."
        return True, norm

    def remove_weekly_time(self, weekday: int, hhmm: str) -> tuple[bool, str]:
        if weekday < 1 or weekday > 7:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ Ð½ÐµÐ´ÐµÐ»Ð¸."
        norm = _normalize_hhmm(hhmm)
        if not norm:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ð¾Ðµ Ð²Ñ€ÐµÐ¼Ñ."
        with atomic() as db:
            WeeklyScheduleRepo.remove_time(db, weekday, norm)
        return True, norm

    def toggle_day_open(self, weekday: int, is_open: bool) -> tuple[bool, str]:
        if weekday < 1 or weekday > 7:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ Ð½ÐµÐ´ÐµÐ»Ð¸."
        if not is_open:
            with get_db() as db:
                today = local_today().isoformat()
                rows = db.execute(
                    "SELECT date FROM bookings WHERE status='active' AND date>=?",
                    (today,),
                ).fetchall()
                for row in rows:
                    if _weekday_from_date(row["date"]) == weekday:
                        return False, "ÐÐµÐ»ÑŒÐ·Ñ Ð·Ð°ÐºÑ€Ñ‹Ñ‚ÑŒ Ð´ÐµÐ½ÑŒ: ÐµÑÑ‚ÑŒ Ð±ÑƒÐ´ÑƒÑ‰Ð¸Ðµ Ð·Ð°Ð¿Ð¸ÑÐ¸."
        with atomic() as db:
            WeeklyScheduleRepo.set_day_open(db, weekday, is_open)
        return True, "ok"

    def set_closed_period(self, weekday: int, start: str, end: str) -> tuple[bool, str]:
        if weekday < 1 or weekday > 7:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ Ð½ÐµÐ´ÐµÐ»Ð¸."
        start_n = _normalize_hhmm(start)
        end_n = _normalize_hhmm(end)
        if not start_n or not end_n:
            return False, "Ð¤Ð¾Ñ€Ð¼Ð°Ñ‚ Ð¿ÐµÑ€Ð¸Ð¾Ð´Ð°: Ð§Ð§:ÐœÐœ-Ð§Ð§:ÐœÐœ."
        if start_n >= end_n:
            return False, "ÐÐ°Ñ‡Ð°Ð»Ð¾ Ð¿ÐµÑ€Ð¸Ð¾Ð´Ð° Ð´Ð¾Ð»Ð¶Ð½Ð¾ Ð±Ñ‹Ñ‚ÑŒ Ñ€Ð°Ð½ÑŒÑˆÐµ ÐºÐ¾Ð½Ñ†Ð°."
        with get_db() as db:
            today = local_today().isoformat()
            rows = db.execute(
                "SELECT date, time FROM bookings WHERE status='active' AND date>=?",
                (today,),
            ).fetchall()
            for row in rows:
                if _weekday_from_date(row["date"]) != weekday:
                    continue
                if start_n <= row["time"] < end_n:
                    return False, "ÐŸÐµÑ€Ð¸Ð¾Ð´ ÐºÐ¾Ð½Ñ„Ð»Ð¸ÐºÑ‚ÑƒÐµÑ‚ Ñ Ð±ÑƒÐ´ÑƒÑ‰Ð¸Ð¼Ð¸ Ð·Ð°Ð¿Ð¸ÑÑÐ¼Ð¸."
        with atomic() as db:
            WeeklyScheduleRepo.set_closed_period(db, weekday, start_n, end_n)
        return True, f"{start_n}-{end_n}"

    def clear_closed_period(self, weekday: int) -> tuple[bool, str]:
        if weekday < 1 or weekday > 7:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ Ð½ÐµÐ´ÐµÐ»Ð¸."
        with atomic() as db:
            WeeklyScheduleRepo.clear_closed_period(db, weekday)
        return True, "ok"

    def copy_day_template(self, source_weekday: int, target_weekday: int) -> tuple[bool, int | str]:
        if source_weekday < 1 or source_weekday > 7 or target_weekday < 1 or target_weekday > 7:
            return False, "ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ Ð½ÐµÐ´ÐµÐ»Ð¸."
        if source_weekday == target_weekday:
            return False, "ÐÐµÐ»ÑŒÐ·Ñ ÐºÐ¾Ð¿Ð¸Ñ€Ð¾Ð²Ð°Ñ‚ÑŒ ÑˆÐ°Ð±Ð»Ð¾Ð½ Ð² Ñ‚Ð¾Ñ‚ Ð¶Ðµ Ð´ÐµÐ½ÑŒ."
        with atomic() as db:
            copied = WeeklyScheduleRepo.replace_day_times(db, source_weekday, target_weekday)
        return True, copied

    def times_for_date(self, date_str: str, db=None) -> list[str]:
        def _compute(conn) -> list[str]:
            weekday = _weekday_from_date(date_str)
            day = WeeklyScheduleRepo.get_day_template(conn, weekday)
            if not day["is_open"]:
                return []
            times = sorted(day["times"])
            if day["closed_start"] and day["closed_end"]:
                times = [
                    t for t in times
                    if not (day["closed_start"] <= t < day["closed_end"])
                ]
            return times
        if db is not None:
            return _compute(db)
        with get_db() as conn:
            return _compute(conn)

class ClientService:

    def touch(self, user_id: int, username: Optional[str], first_name: str):
        with atomic() as db:
            ClientRepo.upsert(db, user_id, username, first_name)

    def get(self, user_id: int) -> Optional[Client]:
        with get_db() as db:
            return ClientRepo.get(db, user_id)

    def set_profile(self, user_id: int, name: str, phone: str):
        """FIX: Ð²Ð°Ð»Ð¸Ð´Ð¸Ñ€ÑƒÐµÑ‚ Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚ Ñ‚ÐµÐ»ÐµÑ„Ð¾Ð½Ð° Ð¿ÐµÑ€ÐµÐ´ ÑÐ¾Ñ…Ñ€Ð°Ð½ÐµÐ½Ð¸ÐµÐ¼ Ð² Ð‘Ð”."""
        if not phone or not phone.strip():
            raise ValueError("Ð¢ÐµÐ»ÐµÑ„Ð¾Ð½ ÐºÐ»Ð¸ÐµÐ½Ñ‚Ð° Ð½Ðµ Ð¼Ð¾Ð¶ÐµÑ‚ Ð±Ñ‹Ñ‚ÑŒ Ð¿ÑƒÑÑ‚Ñ‹Ð¼")
        if not _validate_phone(phone):
            raise ValueError(
                f"ÐÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚ Ñ‚ÐµÐ»ÐµÑ„Ð¾Ð½Ð°: {phone!r}. "
                "ÐžÐ¶Ð¸Ð´Ð°ÐµÑ‚ÑÑ Ñ€Ð¾ÑÑÐ¸Ð¹ÑÐºÐ¸Ð¹ Ð½Ð¾Ð¼ÐµÑ€ +7/8 + 10 Ñ†Ð¸Ñ„Ñ€."
            )
        normalized = _normalize_phone(phone)
        with atomic() as db:
            ClientRepo.set_profile(db, user_id, name, normalized)

    def all_clients(self) -> list[Client]:
        with get_db() as db:
            return ClientRepo.all_registered(db)

    def delete_client(self, user_id: int) -> bool:
        """Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ð¿Ñ€Ð¾Ñ„Ð¸Ð»ÑŒ ÐºÐ»Ð¸ÐµÐ½Ñ‚Ð°. Ð—Ð°Ð¿Ð¸ÑÐ¸ Ð¾ÑÑ‚Ð°ÑŽÑ‚ÑÑ."""
        try:
            with atomic() as db:
                ClientRepo.delete_profile(db, user_id)
            logger.info("client profile deleted uid=%s", user_id)
            return True
        except Exception as e:
            logger.error("delete_client uid=%s: %s", user_id, e)
            return False

    def import_clients(self, rows: list[dict]) -> tuple[int, int]:
        """Ð˜Ð¼Ð¿Ð¾Ñ€Ñ‚Ð¸Ñ€ÑƒÐµÑ‚ ÐºÐ»Ð¸ÐµÐ½Ñ‚Ð¾Ð² Ð¸Ð· ÑÐ¿Ð¸ÑÐºÐ° {'name': ..., 'phone': ...}.
        Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ (added, updated).
        """
        from app.core.phone import normalize_phone, validate_phone
        added = updated = 0
        for row in rows:
            name  = str(row.get("name", "")).strip()
            phone = str(row.get("phone", "")).strip()
            if not name or not validate_phone(phone):
                continue
            phone = normalize_phone(phone)
            with get_db() as db:
                existing = db.execute(
                    "SELECT user_id FROM clients WHERE phone=?", (phone,)
                ).fetchone()
            with atomic() as db:
                ClientRepo.upsert_by_phone(db, name, phone)
            if existing:
                updated += 1
            else:
                added += 1
        return added, updated

    def card(self, user_id: int) -> Optional[ClientCard]:
        with get_db() as db:
            client   = ClientRepo.get(db, user_id)
            if not client:
                return None
            bookings = BookingRepo.user_all(db, user_id)
            notes    = NoteRepo.for_client(db, user_id)
            reviews  = ReviewRepo.for_client(db, user_id)
        today = local_today().isoformat()
        past  = [b for b in bookings
                 if b.status in (BookingStatus.ACTIVE, BookingStatus.COMPLETED)
                 and b.date < today]
        spent = sum(b.price for b in past)
        last  = max((b.date for b in past), default=None)
        return ClientCard(
            client=client, bookings=bookings, notes=notes, reviews=reviews,
            total_spent=spent, last_visit=last,
        )

class BookingService:
    def __init__(self):
        self._weekly = WeeklyScheduleService()

    def get(self, bid: int) -> Optional[Booking]:
        with get_db() as db:
            return BookingRepo.get(db, bid)

    def today(self) -> list[Booking]:
        with get_db() as db:
            return BookingRepo.by_date(db, local_today().isoformat())

    def by_date(self, date: str) -> list[Booking]:
        with get_db() as db:
            return BookingRepo.by_date(db, date)

    def user_active(self, user_id: int) -> list[Booking]:
        with get_db() as db:
            return BookingRepo.user_active(db, user_id, local_today().isoformat())

    def user_past(self, user_id: int) -> list[Booking]:
        with get_db() as db:
            return BookingRepo.user_past(db, user_id, local_today().isoformat())

    def future_active(self) -> list[Booking]:
        with get_db() as db:
            return BookingRepo.future(db, local_today().isoformat())

    def events(self, booking_id: int, limit: int = 30) -> list[BookingEvent]:
        with get_db() as db:
            return BookingEventRepo.by_booking(db, booking_id, limit)

    def free_slots(self, date: str) -> list[str]:
        with get_db() as db:
            if BlockedDayRepo.is_blocked(db, date):
                return []
            base_times = self._weekly.times_for_date(date, db=db)
            booked = BookingRepo.booked_times(db, date)
            blocked_slots = BlockedSlotRepo.blocked_for_date(db, date)
        occupied = booked | blocked_slots
        return [t for t in base_times if t not in occupied]

    def available_dates(self) -> list[SlotInfo]:
        dates = booking_dates()
        with get_db() as db:
            blocked  = BlockedDayRepo.blocked_set(db)
            booked_m = BookingRepo.booked_times_bulk(db, dates)
            bslots_m = BlockedSlotRepo.blocked_bulk(db, dates)
            out: list[SlotInfo] = []
            for d in dates:
                if d in blocked:
                    continue
                base_times = self._weekly.times_for_date(d, db=db)
                if not base_times:
                    continue
                occupied = booked_m.get(d, set()) | bslots_m.get(d, set())
                free = [t for t in base_times if t not in occupied]
                if free:
                    out.append(SlotInfo(date=d, free_times=free))
            return out

    def calendar(self, days: int = 14) -> list[SlotInfo]:
        today  = local_today()
        dates  = [(today + timedelta(days=i)).isoformat() for i in range(days)]
        with get_db() as db:
            blocked     = BlockedDayRepo.blocked_set(db)
            booked_m    = BookingRepo.booked_times_bulk(db, dates)
            bslots_m    = BlockedSlotRepo.blocked_bulk(db, dates)
        result = []
        for d in dates:
            if d in blocked:
                result.append(SlotInfo(date=d, free_times=[], is_blocked=True))
            else:
                base_times = self._weekly.times_for_date(d, db=db)
                occupied = booked_m.get(d, set()) | bslots_m.get(d, set())
                free = [t for t in base_times if t not in occupied]
                result.append(SlotInfo(date=d, free_times=free))
        return result

    def blocked_days(self) -> list[tuple[str, str]]:
        with get_db() as db:
            return BlockedDayRepo.all(db)

    def slots_for_day(self, date: str) -> dict:
        with get_db() as db:
            base_times   = self._weekly.times_for_date(date, db=db)
            booked       = BookingRepo.booked_times(db, date)
            blocked_day  = BlockedDayRepo.is_blocked(db, date)
            blocked_slots = BlockedSlotRepo.blocked_for_date(db, date)
        result = []
        for t in base_times:
            if t in booked:
                status = "booked"
            elif blocked_day or t in blocked_slots:
                status = "blocked"
            else:
                status = "free"
            result.append({"time": t, "status": status})
        return {
            "date": date,
            "slots": result,
            "day_blocked": blocked_day,
            "template_count": len(base_times),
        }

    def block_slot(self, date: str, time: str) -> bool:
        try:
            with atomic() as db:
                BlockedSlotRepo.block(db, date, time)
            return True
        except Exception as e:
            logger.error("block_slot %s %s: %s", date, time, e)
            return False

    def unblock_slot(self, date: str, time: str) -> bool:
        try:
            with atomic() as db:
                BlockedSlotRepo.unblock(db, date, time)
            return True
        except Exception as e:
            logger.error("unblock_slot %s %s: %s", date, time, e)
            return False

    def stats(self) -> Stats:
        month = local_now().strftime("%Y-%m")
        with get_db() as db:
            s = BookingRepo.stats(db, month)
        return Stats(
            total_bookings=s["total"], total_revenue=s["revenue"],
            month_bookings=s["month_count"], month_revenue=s["month_revenue"],
            cancelled=s["cancelled"], unique_clients=s["clients"],
            avg_rating=round(s["avg_rating"], 1) if s["avg_rating"] else None,
            month_label=month,
        )

    # ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ñ… Ð·Ð°Ð¿Ð¸ÑÐµÐ¹ Ð½Ð° Ð¾Ð´Ð½Ð¾Ð³Ð¾ ÐºÐ»Ð¸ÐµÐ½Ñ‚Ð°
    MAX_ACTIVE_PER_CLIENT: int = 5

    def create(self, user_id: int, service: str, date: str, time: str) -> BookingResult:
        """
        FIX: Ñ†ÐµÐ½Ð° Ð±ÐµÑ€Ñ‘Ñ‚ÑÑ Ð¸Ð· Ð‘Ð” (Ñ‡ÐµÑ€ÐµÐ· settings.SERVICES, ÑÐ¸Ð½Ñ…Ñ€Ð¾Ð½Ð¸Ð·Ð¸Ñ€Ð¾Ð²Ð°Ð½Ð½Ð¾Ð³Ð¾
        ServiceService._sync), Ð° Ð½Ðµ Ð¸Ð· Ð·Ð°Ñ…Ð°Ñ€Ð´ÐºÐ¾Ð¶ÐµÐ½Ð½Ð¾Ð³Ð¾ ÑÐ»Ð¾Ð²Ð°Ñ€Ñ.
        """
        if service not in settings.SERVICES:
            return BookingResult(ok=False, error="ÐÐµÐ¸Ð·Ð²ÐµÑÑ‚Ð½Ð°Ñ ÑƒÑÐ»ÑƒÐ³Ð°.")
        time = _normalize_hhmm(time or "") or (time or "")
        price = settings.SERVICES[service]
        try:
            with atomic() as db:
                day_times = self._weekly.times_for_date(date, db=db)
                if time not in day_times:
                    return BookingResult(ok=False, error="Ð­Ñ‚Ð¾ Ð²Ñ€ÐµÐ¼Ñ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¾ Ð² Ñ€Ð°ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ð¸.")
                if BlockedDayRepo.is_blocked(db, date):
                    return BookingResult(ok=False, error="Ð”ÐµÐ½ÑŒ Ð·Ð°ÐºÑ€Ñ‹Ñ‚ Ð´Ð»Ñ Ð·Ð°Ð¿Ð¸ÑÐ¸.")
                if time in BlockedSlotRepo.blocked_for_date(db, date):

                    return BookingResult(ok=False, error="Ð­Ñ‚Ð¾Ñ‚ ÑÐ»Ð¾Ñ‚ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿ÐµÐ½ â€” Ð²Ñ‹Ð±ÐµÑ€Ð¸Ñ‚Ðµ Ð´Ñ€ÑƒÐ³Ð¾Ðµ Ð²Ñ€ÐµÐ¼Ñ.")
                booked = BookingRepo.booked_times(db, date)
                if time in booked:
                    return BookingResult(ok=False,
                                        error="Ð¡Ð»Ð¾Ñ‚ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ñ‡Ñ‚Ð¾ Ð·Ð°Ð½ÑÐ»Ð¸ â€” Ð²Ñ‹Ð±ÐµÑ€Ð¸Ñ‚Ðµ Ð´Ñ€ÑƒÐ³Ð¾Ðµ Ð²Ñ€ÐµÐ¼Ñ.")
                if len(booked) >= len(day_times):
                    return BookingResult(ok=False, error="Ð”ÐµÐ½ÑŒ Ð¿Ð¾Ð»Ð½Ð¾ÑÑ‚ÑŒÑŽ Ð·Ð°Ð½ÑÑ‚.")
                # Ð›Ð¸Ð¼Ð¸Ñ‚ Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ñ… Ð·Ð°Ð¿Ð¸ÑÐµÐ¹ Ð½Ð° Ð¾Ð´Ð½Ð¾Ð³Ð¾ ÐºÐ»Ð¸ÐµÐ½Ñ‚Ð°
                active_count = db.execute(
                    "SELECT COUNT(*) FROM bookings WHERE user_id=? AND status='active'",
                    (user_id,),
                ).fetchone()[0]
                if active_count >= self.MAX_ACTIVE_PER_CLIENT:
                    return BookingResult(
                        ok=False,
                        error=f"Ð£ Ð²Ð°Ñ ÑƒÐ¶Ðµ {active_count} Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ñ… "
                              f"{_plural(active_count, 'Ð·Ð°Ð¿Ð¸ÑÑŒ', 'Ð·Ð°Ð¿Ð¸ÑÐ¸', 'Ð·Ð°Ð¿Ð¸ÑÐµÐ¹')}. "
                              f"ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ â€” {self.MAX_ACTIVE_PER_CLIENT}. "
                              f"ÐžÑ‚Ð¼ÐµÐ½Ð¸Ñ‚Ðµ Ð¾Ð´Ð½Ñƒ Ð¸Ð· ÑÑƒÑ‰ÐµÑÑ‚Ð²ÑƒÑŽÑ‰Ð¸Ñ…, Ñ‡Ñ‚Ð¾Ð±Ñ‹ ÑÐ¾Ð·Ð´Ð°Ñ‚ÑŒ Ð½Ð¾Ð²ÑƒÑŽ.",
                    )
                bid     = BookingRepo.insert(db, user_id, service, price, date, time)
                BookingRepo.set_confirmed(db, bid, False)
                BookingEventRepo.insert(
                    db, bid, "created", f"client:{user_id}",
                    f"{date} {time} | {service}",
                )
                booking = BookingRepo.get(db, bid)
            client = self._get_client_name(user_id)
            enqueue(ExcelRow("Ð·Ð°Ð¿Ð¸ÑÑŒ", client[0], client[1],
                             service, date, time, price, "active"))
            logger.info("create bid=%s uid=%s %s %s", bid, user_id, date, time)
            return BookingResult(ok=True, booking=booking)
        except Exception as e:
            logger.error("create uid=%s: %s", user_id, e)
            return BookingResult(ok=False, error="ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐ¾Ð·Ð´Ð°Ñ‚ÑŒ Ð·Ð°Ð¿Ð¸ÑÑŒ. ÐŸÐ¾Ð¿Ñ€Ð¾Ð±ÑƒÐ¹Ñ‚Ðµ ÐµÑ‰Ñ‘ Ñ€Ð°Ð·.")

    def cancel(self, bid: int, by_user: int) -> BookingResult:
        with get_db() as db:
            booking = BookingRepo.get(db, bid)
        if not booking:
            return BookingResult(ok=False, error="Ð—Ð°Ð¿Ð¸ÑÑŒ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½Ð°.")
        if booking.status != BookingStatus.ACTIVE:
            return BookingResult(ok=False, error="Ð—Ð°Ð¿Ð¸ÑÑŒ ÑƒÐ¶Ðµ Ð½ÐµÐ°ÐºÑ‚Ð¸Ð²Ð½Ð°.")
        if booking.user_id != by_user and by_user not in settings.ADMIN_IDS:
            return BookingResult(ok=False, error="ÐÐµÑ‚ Ð¿Ñ€Ð°Ð² Ð´Ð»Ñ Ð¾Ñ‚Ð¼ÐµÐ½Ñ‹.")
        try:
            with atomic() as db:
                BookingRepo.set_status(db, bid, BookingStatus.CANCELLED)
                RescheduleRepo.delete_by_booking(db, bid)
                actor = "admin" if by_user in settings.ADMIN_IDS else "client"
                BookingEventRepo.insert(
                    db, bid, "cancelled", f"{actor}:{by_user}",
                    f"{booking.date} {booking.time} | {booking.service}",
                )
            client = self._get_client_name(booking.user_id)
            enqueue(ExcelRow("Ð¾Ñ‚Ð¼ÐµÐ½Ð°", client[0], client[1],
                             booking.service, booking.date, booking.time, 0, "cancelled"))
            logger.info("cancel bid=%s by=%s", bid, by_user)
            return BookingResult(ok=True, booking=booking)
        except Exception as e:
            logger.error("cancel bid=%s: %s", bid, e)
            return BookingResult(ok=False, error="ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¾Ñ‚Ð¼ÐµÐ½Ð¸Ñ‚ÑŒ.")

    def confirm_reschedule(self, booking_id: int) -> BookingResult:
        with get_db() as db:
            rr      = RescheduleRepo.get_by_booking(db, booking_id)
            booking = BookingRepo.get(db, booking_id)
        if not rr or not booking:
            return BookingResult(ok=False, error="Ð—Ð°Ð¿Ñ€Ð¾Ñ Ð½Ð° Ð¿ÐµÑ€ÐµÐ½Ð¾Ñ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½.")
        try:
            with atomic() as db:
                day_times = self._weekly.times_for_date(rr.new_date, db=db)
                if rr.new_time not in day_times:
                    return BookingResult(ok=False, error="Ð­Ñ‚Ð¾ Ð²Ñ€ÐµÐ¼Ñ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¾ Ð² Ñ€Ð°ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ð¸.")
                if BlockedDayRepo.is_blocked(db, rr.new_date):
                    return BookingResult(ok=False, error="Ð”ÐµÐ½ÑŒ Ð·Ð°ÐºÑ€Ñ‹Ñ‚ Ð´Ð»Ñ Ð·Ð°Ð¿Ð¸ÑÐ¸.")
                if rr.new_time in BlockedSlotRepo.blocked_for_date(db, rr.new_date):
                    return BookingResult(ok=False, error="Ð­Ñ‚Ð¾Ñ‚ ÑÐ»Ð¾Ñ‚ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿ÐµÐ½.")
                booked = BookingRepo.booked_times(db, rr.new_date)
                if rr.new_time in booked:
                    return BookingResult(ok=False, error="Ð¡Ð»Ð¾Ñ‚ ÑƒÐ¶Ðµ Ð·Ð°Ð½ÑÑ‚.")
                old_date, old_time = booking.date, booking.time
                BookingRepo.set_slot(db, booking_id, rr.new_date, rr.new_time)
                RescheduleRepo.delete(db, rr.id)
                BookingEventRepo.insert(
                    db,
                    booking_id,
                    "rescheduled",
                    "admin",
                    f"{old_date} {old_time} -> {rr.new_date} {rr.new_time}",
                )
                updated = BookingRepo.get(db, booking_id)
            return BookingResult(ok=True, booking=updated)
        except Exception as e:
            logger.error("confirm_reschedule bid=%s: %s", booking_id, e)
            return BookingResult(ok=False, error="ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¿ÐµÑ€ÐµÐ½ÐµÑÑ‚Ð¸.")

    def decline_reschedule(self, booking_id: int):
        with atomic() as db:
            RescheduleRepo.delete_by_booking(db, booking_id)

    def request_reschedule(self, booking_id: int,
                           new_date: str, new_time: str) -> bool:
        expires = utc_plus_hours(settings.RESCHEDULE_TIMEOUT_HOURS)
        with atomic() as db:
            RescheduleRepo.create(db, booking_id, new_date, new_time, expires)
        return True

    def reschedule_by_client(self, booking_id: int, by_user: int, new_date: str, new_time: str) -> BookingResult:
        with get_db() as db:
            booking = BookingRepo.get(db, booking_id)
        if not booking:
            return BookingResult(ok=False, error="Запись не найдена.")
        is_admin = by_user in settings.ADMIN_IDS
        if booking.user_id != by_user and not is_admin:
            return BookingResult(ok=False, error="Нет прав для переноса.")
        if booking.status != BookingStatus.ACTIVE:
            return BookingResult(ok=False, error="Перенос доступен только для активной записи.")
        if booking.date < local_today().isoformat():
            return BookingResult(ok=False, error="Прошедшую запись перенести нельзя.")
        new_time = _normalize_hhmm(new_time or "") or (new_time or "")
        if booking.date == new_date and booking.time == new_time:
            return BookingResult(ok=False, error="Выберите другое время для переноса.")

        try:
            with atomic() as db:
                day_times = self._weekly.times_for_date(new_date, db=db)
                if new_time not in day_times:
                    return BookingResult(ok=False, error="Это время недоступно в расписании.")
                if BlockedDayRepo.is_blocked(db, new_date):
                    return BookingResult(ok=False, error="День закрыт для записи.")
                if new_time in BlockedSlotRepo.blocked_for_date(db, new_date):
                    return BookingResult(ok=False, error="Этот слот недоступен.")
                booked = BookingRepo.booked_times(db, new_date)
                if new_time in booked:
                    return BookingResult(ok=False, error="Слот только что заняли — выберите другое время.")

                old_date, old_time = booking.date, booking.time
                BookingRepo.set_slot(db, booking_id, new_date, new_time)
                RescheduleRepo.delete_by_booking(db, booking_id)
                BookingEventRepo.insert(
                    db,
                    booking_id,
                    "rescheduled",
                    f"{'admin' if is_admin else 'client'}:{by_user}",
                    f"{old_date} {old_time} -> {new_date} {new_time}",
                )
                updated = BookingRepo.get(db, booking_id)
            return BookingResult(ok=True, booking=updated)
        except Exception as e:
            logger.error("reschedule_by_client bid=%s uid=%s: %s", booking_id, by_user, e)
            return BookingResult(ok=False, error="Не удалось перенести запись. Попробуйте ещё раз.")

    def confirm_by_master(self, bid: int) -> BookingResult:
        with get_db() as db:
            booking = BookingRepo.get(db, bid)
        if not booking:
            return BookingResult(ok=False, error="Запись не найдена.")
        if booking.status != BookingStatus.ACTIVE:
            return BookingResult(ok=False, error="Подтверждение доступно только для активной записи.")
        if booking.confirmed_by_master:
            return BookingResult(ok=True, booking=booking)
        try:
            with atomic() as db:
                BookingRepo.set_confirmed(db, bid, True)
                BookingEventRepo.insert(
                    db, bid, "confirmed_by_master", "admin",
                    f"{booking.date} {booking.time} | {booking.service}",
                )
                updated = BookingRepo.get(db, bid)
            return BookingResult(ok=True, booking=updated)
        except Exception as e:
            logger.error("confirm_by_master bid=%s: %s", bid, e)
            return BookingResult(ok=False, error="Не удалось подтвердить запись.")

    def get_reschedule(self, booking_id: int) -> Optional[RescheduleRequest]:
        with get_db() as db:
            return RescheduleRepo.get_by_booking(db, booking_id)

    def block_day(self, date: str, note: str = "") -> bool:
        with atomic() as db:
            return BlockedDayRepo.insert(db, date, note)

    def unblock_day(self, date: str):
        with atomic() as db:
            BlockedDayRepo.delete(db, date)

    def mark(self, bid: int, field: str):
        with atomic() as db:
            BookingRepo.mark(db, bid, field)

    def complete_past_bookings(self) -> int:
        today = local_today().isoformat()
        with atomic() as db:
            count = BookingRepo.complete_past(db, today)
        if count:
            logger.info("complete_past: %d bookings â†’ completed", count)
        return count

    def pending_reminders(self) -> dict[str, list[Booking]]:
        today = local_today().isoformat()
        with get_db() as db:
            candidates = BookingRepo.pending_reminders(db, today)
        due_24h, due_2h = [], []
        for b in candidates:
            diff = seconds_until(b.date, b.time)
            if not b.notified_24h and 0 < diff <= 86_400:
                due_24h.append(b)
            if not b.notified_1h and 0 < diff <= 7_200:
                due_2h.append(b)
        return {"24h": due_24h, "2h": due_2h}

    def return_candidates(self) -> list[Booking]:
        with get_db() as db:
            return BookingRepo.return_candidates(db, return_notify_date())

    def review_candidates(self) -> list[Booking]:
        """
        FIX: Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ Ð´Ð°Ñ‚Ñƒ Ð²Ð¸Ð·Ð¸Ñ‚Ð° (b.date), Ð° Ð½Ðµ created_at_utc Ð·Ð°Ð¿Ð¸ÑÐ¸.
        ÐšÐ°Ð½Ð´Ð¸Ð´Ð°Ñ‚Ñ‹ â€” Ð·Ð°Ð¿Ð¸ÑÐ¸, Ñƒ ÐºÐ¾Ñ‚Ð¾Ñ€Ñ‹Ñ… Ð´Ð°Ñ‚Ð° Ð²Ð¸Ð·Ð¸Ñ‚Ð° <= ÑÐµÐ³Ð¾Ð´Ð½Ñ - REVIEW_DELAY_HOURS.
        """
        cutoff = review_cutoff_utc()
        with get_db() as db:
            return BookingRepo.review_candidates(db, cutoff)

    def expire_reschedules(self) -> list[RescheduleRequest]:
        """FIX: ÑƒÐ´Ð°Ð»ÑÐµÐ¼ Ð²ÑÐµ Ð¸ÑÑ‚Ñ‘ÐºÑˆÐ¸Ðµ Ð·Ð° Ð¾Ð´Ð½Ñƒ Ñ‚Ñ€Ð°Ð½Ð·Ð°ÐºÑ†Ð¸ÑŽ."""
        with get_db() as db:
            expired = RescheduleRepo.expired(db)
        if expired:
            ids = [rr.id for rr in expired]
            with atomic() as db:
                for rr_id in ids:
                    RescheduleRepo.delete(db, rr_id)
        return expired

    def _get_client_name(self, user_id: int) -> tuple[str, str]:
        with get_db() as db:
            c = ClientRepo.get(db, user_id)
        return (c.name if c else "â€”"), (c.phone if c else "â€”")

class NoteService:
    """FIX: Ð±Ñ‹Ð» Ð¾ÑˆÐ¸Ð±Ð¾Ñ‡Ð½Ð¾ ÑÐºÐ»ÐµÐµÐ½ Ñ ServiceService."""

    def add(self, user_id: int, author_id: int, text: str):
        with atomic() as db:
            NoteRepo.insert(db, user_id, author_id, text)
        logger.info("note user=%s by=%s", user_id, author_id)

class ServiceService:
    """Ð£Ð¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð¸Ðµ ÑƒÑÐ»ÑƒÐ³Ð°Ð¼Ð¸. ÐŸÐ¾ÑÐ»Ðµ ÐºÐ°Ð¶Ð´Ð¾Ð³Ð¾ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¸Ñ ÑÐ¸Ð½Ñ…Ñ€Ð¾Ð½Ð¸Ð·Ð¸Ñ€ÑƒÐµÑ‚ settings.SERVICES."""

    def sync(self) -> dict[str, int]:
        """Ð—Ð°Ð³Ñ€ÑƒÐ¶Ð°ÐµÑ‚ Ð°ÐºÑ‚ÑƒÐ°Ð»ÑŒÐ½Ñ‹Ð¹ ÑÐ¿Ð¸ÑÐ¾Ðº ÑƒÑÐ»ÑƒÐ³ Ð¸Ð· Ð‘Ð” Ð¸ Ð¾Ð±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ settings.SERVICES."""
        with get_db() as db:
            rows = ServiceRepo.all_active(db)
        settings.update_services(dict(rows))
        return settings.SERVICES

    # Ð¿ÑƒÐ±Ð»Ð¸Ñ‡Ð½Ñ‹Ð¹ Ð¿ÑÐµÐ²Ð´Ð¾Ð½Ð¸Ð¼ Ð´Ð»Ñ main.py
    def _sync(self) -> dict[str, int]:
        return self.sync()

    def all(self) -> dict[str, int]:
        return self.sync()

    def add(self, name: str, price: int) -> tuple[bool, str]:
        name = name.strip()
        if not name:
            return False, "ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ Ð½Ðµ Ð¼Ð¾Ð¶ÐµÑ‚ Ð±Ñ‹Ñ‚ÑŒ Ð¿ÑƒÑÑ‚Ñ‹Ð¼."
        if price <= 0:
            return False, "Ð¦ÐµÐ½Ð° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±Ñ‹Ñ‚ÑŒ Ð±Ð¾Ð»ÑŒÑˆÐµ 0."
        with atomic() as db:
            ok = ServiceRepo.insert(db, name, price)
        if not ok:
            return False, f"Ð£ÑÐ»ÑƒÐ³Ð° Â«{name}Â» ÑƒÐ¶Ðµ ÑÑƒÑ‰ÐµÑÑ‚Ð²ÑƒÐµÑ‚."
        self.sync()
        return True, ""

    def update_price(self, name: str, new_price: int) -> tuple[bool, str]:
        if new_price <= 0:
            return False, "Ð¦ÐµÐ½Ð° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±Ñ‹Ñ‚ÑŒ Ð±Ð¾Ð»ÑŒÑˆÐµ 0."
        with atomic() as db:
            ServiceRepo.update_price(db, name, new_price)
        self.sync()
        return True, ""

    def rename(self, old_name: str, new_name: str) -> tuple[bool, str]:
        new_name = new_name.strip()
        if not new_name:
            return False, "ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ Ð½Ðµ Ð¼Ð¾Ð¶ÐµÑ‚ Ð±Ñ‹Ñ‚ÑŒ Ð¿ÑƒÑÑ‚Ñ‹Ð¼."
        with atomic() as db:
            ok = ServiceRepo.rename(db, old_name, new_name)
        if not ok:
            return False, f"Ð£ÑÐ»ÑƒÐ³Ð° Â«{new_name}Â» ÑƒÐ¶Ðµ ÑÑƒÑ‰ÐµÑÑ‚Ð²ÑƒÐµÑ‚."
        self.sync()
        return True, ""

    def delete(self, name: str) -> tuple[bool, str]:
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM bookings WHERE service=? AND status='active'",
                (name,),
            ).fetchone()[0]
        if count > 0:
            return False, f"ÐÐµÐ»ÑŒÐ·Ñ ÑƒÐ´Ð°Ð»Ð¸Ñ‚ÑŒ: ÐµÑÑ‚ÑŒ {count} Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ñ… Ð·Ð°Ð¿Ð¸ÑÐµÐ¹ Ð½Ð° Â«{name}Â»."
        with atomic() as db:
            ServiceRepo.delete(db, name)
        self.sync()
        return True, ""

class ReviewService:

    def add(self, user_id: int, booking_id: int,
            rating: int, text: str) -> int:
        """Ð¡Ð¾Ð·Ð´Ð°Ñ‘Ñ‚ Ð¾Ñ‚Ð·Ñ‹Ð². Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ review_id Ð´Ð»Ñ Ð¿Ð¾ÑÐ»ÐµÐ´ÑƒÑŽÑ‰ÐµÐ³Ð¾ Ð¿Ñ€Ð¸ÐºÑ€ÐµÐ¿Ð»ÐµÐ½Ð¸Ñ Ñ„Ð¾Ñ‚Ð¾."""
        with atomic() as db:
            rid = ReviewRepo.insert(db, user_id, booking_id, rating, text)
        logger.info("review uid=%s bid=%s rating=%s", user_id, booking_id, rating)
        return rid

    def attach_photo(self, review_id: int, photo_file_id: str):
        """ÐŸÑ€Ð¸ÐºÑ€ÐµÐ¿Ð»ÑÐµÑ‚ Ñ„Ð¾Ñ‚Ð¾ Ðº ÑƒÐ¶Ðµ ÑÐ¾Ð·Ð´Ð°Ð½Ð½Ð¾Ð¼Ñƒ Ð¾Ñ‚Ð·Ñ‹Ð²Ñƒ."""
        with atomic() as db:
            ReviewRepo.update_photo(db, review_id, photo_file_id)
        logger.info("review photo review_id=%s", review_id)

    def recent(self, limit: int = 20) -> list[Review]:
        with get_db() as db:
            return ReviewRepo.recent(db, limit)

class TimeSlotService:
    """Ð£Ð¿Ñ€Ð°Ð²Ð»ÐµÐ½Ð¸Ðµ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð½Ñ‹Ð¼Ð¸ ÑÐ»Ð¾Ñ‚Ð°Ð¼Ð¸ Ð¼Ð°ÑÑ‚ÐµÑ€Ð°.
    ÐŸÐ¾ÑÐ»Ðµ ÐºÐ°Ð¶Ð´Ð¾Ð³Ð¾ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¸Ñ ÑÐ¸Ð½Ñ…Ñ€Ð¾Ð½Ð¸Ð·Ð¸Ñ€ÑƒÐµÑ‚ settings.TIME_SLOTS.
    """

    def sync(self) -> dict[str, str]:
        with get_db() as db:
            slots = TimeSlotRepo.all_active(db)
        settings.update_time_slots(slots)
        return slots

    def all(self) -> list[tuple[str, str, bool]]:
        with get_db() as db:
            return TimeSlotRepo.all(db)

    def add(self, start: str, end: str) -> tuple[bool, str]:
        """Ð”Ð¾Ð±Ð°Ð²Ð¸Ñ‚ÑŒ ÑÐ»Ð¾Ñ‚. start/end Ð² Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ðµ HH:MM."""
        import re
        _t = re.compile(r"^\d{2}:\d{2}$")
        if not _t.match(start) or not _t.match(end):
            return False, "Ð¤Ð¾Ñ€Ð¼Ð°Ñ‚ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð¸: Ð§Ð§:ÐœÐœ (Ð½Ð°Ð¿Ñ€Ð¸Ð¼ÐµÑ€ 10:00)"
        if start >= end:
            return False, "Ð’Ñ€ÐµÐ¼Ñ Ð½Ð°Ñ‡Ð°Ð»Ð° Ð´Ð¾Ð»Ð¶Ð½Ð¾ Ð±Ñ‹Ñ‚ÑŒ Ñ€Ð°Ð½ÑŒÑˆÐµ ÐºÐ¾Ð½Ñ†Ð°."
        with atomic() as db:
            ok = TimeSlotRepo.insert(db, start, end)
        if not ok:
            return False, f"Ð¡Ð»Ð¾Ñ‚ {start} ÑƒÐ¶Ðµ ÑÑƒÑ‰ÐµÑÑ‚Ð²ÑƒÐµÑ‚."
        self.sync()
        return True, ""

    def toggle(self, start: str, active: bool) -> tuple[bool, str]:
        """Ð’ÐºÐ»ÑŽÑ‡Ð¸Ñ‚ÑŒ / Ð²Ñ‹ÐºÐ»ÑŽÑ‡Ð¸Ñ‚ÑŒ ÑÐ»Ð¾Ñ‚."""
        with atomic() as db:
            TimeSlotRepo.set_active(db, start, active)
        self.sync()
        return True, ""

    def delete(self, start: str) -> tuple[bool, str]:
        # ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ðµ Ð·Ð°Ð¿Ð¸ÑÐ¸ Ð½Ð° ÑÑ‚Ð¾Ñ‚ ÑÐ»Ð¾Ñ‚
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM bookings WHERE time=? AND status='active'",
                (start,),
            ).fetchone()[0]
        if count > 0:
            return False, f"ÐÐµÐ»ÑŒÐ·Ñ ÑƒÐ´Ð°Ð»Ð¸Ñ‚ÑŒ: ÐµÑÑ‚ÑŒ {count} Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ñ… Ð·Ð°Ð¿Ð¸ÑÐµÐ¹ Ð½Ð° {start}."
        with atomic() as db:
            TimeSlotRepo.delete(db, start)
        self.sync()
        return True, ""

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BlacklistService
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


class BlacklistService:

    def is_banned(self, user_id: int) -> bool:
        with get_db() as db:
            return BlacklistRepo.is_banned(db, user_id)

    def ban(self, user_id: int, reason: str = "") -> bool:
        try:
            with atomic() as db:
                BlacklistRepo.add(db, user_id, reason)
            logger.info("ban uid=%s reason=%r", user_id, reason)
            return True
        except Exception as e:
            logger.error("ban uid=%s: %s", user_id, e)
            return False

    def unban(self, user_id: int) -> bool:
        try:
            with atomic() as db:
                BlacklistRepo.remove(db, user_id)
            logger.info("unban uid=%s", user_id)
            return True
        except Exception as e:
            logger.error("unban uid=%s: %s", user_id, e)
            return False

    def all(self) -> list:
        with get_db() as db:
            return BlacklistRepo.all(db)

class PortfolioService:
    """Ð¡ÐµÑ€Ð²Ð¸Ñ Ð¿Ð¾Ñ€Ñ‚Ñ„Ð¾Ð»Ð¸Ð¾ â€” Ñ„Ð¾Ñ‚Ð¾ Ñ€Ð°Ð±Ð¾Ñ‚ Ð¼Ð°ÑÑ‚ÐµÑ€Ð°.
    Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ ÐµÐ´Ð¸Ð½ÑÑ‚Ð²ÐµÐ½Ð½Ð¾Ðµ WAL-ÑÐ¾ÐµÐ´Ð¸Ð½ÐµÐ½Ð¸Ðµ Ñ‡ÐµÑ€ÐµÐ· get_db()/atomic().
    """

    def __init__(self, db_path: str = ""):
        pass  # db_path Ð´Ð»Ñ Ð¾Ð±Ñ€Ð°Ñ‚Ð½Ð¾Ð¹ ÑÐ¾Ð²Ð¼ÐµÑÑ‚Ð¸Ð¼Ð¾ÑÑ‚Ð¸

    def all(self) -> list[dict]:
        with get_db() as db:
            return PortfolioRepo.all(db)

    def add(self, file_id: str) -> tuple[bool, str]:
        with get_db() as db:
            count = PortfolioRepo.count(db)
        if count >= PortfolioRepo.MAX_PHOTOS:
            return False, f"ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ {PortfolioRepo.MAX_PHOTOS} Ñ„Ð¾Ñ‚Ð¾. Ð£Ð´Ð°Ð»Ð¸Ñ‚Ðµ Ð»Ð¸ÑˆÐ½Ð¸Ðµ."
        with atomic() as db:
            PortfolioRepo.add_atomic(db, file_id)
        return True, ""

    def delete(self, photo_id: int) -> bool:
        with atomic() as db:
            return PortfolioRepo.delete_atomic(db, photo_id)

    def count(self) -> int:
        with get_db() as db:
            return PortfolioRepo.count(db)

