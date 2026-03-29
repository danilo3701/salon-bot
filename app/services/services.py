"""
app/services/services.py — сервисный слой.

Исправления:
  - NoteService выделен в отдельный класс (был склеен с ServiceService)
  - BookingService.create() берёт цену из БД, а не settings.SERVICES
  - BookingService.free_slots() / available_dates() читают TIME_SLOTS из settings
    (они уже синхронизированы с БД через ServiceService._sync)
  - review_candidates() использует дату визита, а не created_at_utc записи
  - expire_reschedules() удаляет все сразу в одной транзакции
  - ClientService.set_profile() валидирует формат телефона (RF, 10-11 цифр)
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

# Валидация и нормализация телефонов — единственный источник правды
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
    Note, RescheduleRequest, Review, SlotInfo, Stats,
)
from app.repositories.repo import (
    BlacklistRepo, BlockedDayRepo, BlockedSlotRepo, BookingRepo, ClientRepo,
    NoteRepo, PortfolioRepo, RescheduleRepo, ReviewRepo, ServiceRepo, TimeSlotRepo,
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

class ClientService:

    def touch(self, user_id: int, username: Optional[str], first_name: str):
        with atomic() as db:
            ClientRepo.upsert(db, user_id, username, first_name)

    def get(self, user_id: int) -> Optional[Client]:
        with get_db() as db:
            return ClientRepo.get(db, user_id)

    def set_profile(self, user_id: int, name: str, phone: str):
        """FIX: валидирует формат телефона перед сохранением в БД."""
        if not phone or not phone.strip():
            raise ValueError("Телефон клиента не может быть пустым")
        if not _validate_phone(phone):
            raise ValueError(
                f"Некорректный формат телефона: {phone!r}. "
                "Ожидается российский номер +7/8 + 10 цифр."
            )
        normalized = _normalize_phone(phone)
        with atomic() as db:
            ClientRepo.set_profile(db, user_id, name, normalized)

    def all_clients(self) -> list[Client]:
        with get_db() as db:
            return ClientRepo.all_registered(db)

    def delete_client(self, user_id: int) -> bool:
        """Удаляет профиль клиента. Записи остаются."""
        try:
            with atomic() as db:
                ClientRepo.delete_profile(db, user_id)
            logger.info("client profile deleted uid=%s", user_id)
            return True
        except Exception as e:
            logger.error("delete_client uid=%s: %s", user_id, e)
            return False

    def import_clients(self, rows: list[dict]) -> tuple[int, int]:
        """Импортирует клиентов из списка {'name': ..., 'phone': ...}.
        Возвращает (added, updated).
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

    def free_slots(self, date: str) -> list[str]:
        with get_db() as db:
            if BlockedDayRepo.is_blocked(db, date):
                return []
            booked = BookingRepo.booked_times(db, date)
            blocked_slots = BlockedSlotRepo.blocked_for_date(db, date)
        occupied = booked | blocked_slots
        return [t for t in settings.TIME_SLOTS if t not in occupied]

    def available_dates(self) -> list[SlotInfo]:
        dates = booking_dates()
        with get_db() as db:
            blocked  = BlockedDayRepo.blocked_set(db)
            booked_m = BookingRepo.booked_times_bulk(db, dates)
        return [
            SlotInfo(date=d, free_times=[
                t for t in settings.TIME_SLOTS
                if t not in booked_m.get(d, set())
            ])
            for d in dates
            if d not in blocked and any(
                t not in booked_m.get(d, set()) for t in settings.TIME_SLOTS
            )
        ]

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
                occupied = booked_m.get(d, set()) | bslots_m.get(d, set())
                free = [t for t in settings.TIME_SLOTS if t not in occupied]
                result.append(SlotInfo(date=d, free_times=free))
        return result

    def blocked_days(self) -> list[tuple[str, str]]:
        with get_db() as db:
            return BlockedDayRepo.all(db)

    def slots_for_day(self, date: str) -> dict:
        """Возвращает все слоты дня с их статусом для панели мастера."""
        with get_db() as db:
            booked       = BookingRepo.booked_times(db, date)
            blocked_day  = BlockedDayRepo.is_blocked(db, date)
            blocked_slots = BlockedSlotRepo.blocked_for_date(db, date)
        result = []
        for t in settings.TIME_SLOTS:
            if t in booked:
                status = "booked"
            elif blocked_day or t in blocked_slots:
                status = "blocked"
            else:
                status = "free"
            result.append({"time": t, "status": status})
        return {"date": date, "slots": result, "day_blocked": blocked_day}

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

    # Максимум активных записей на одного клиента
    MAX_ACTIVE_PER_CLIENT: int = 5

    def create(self, user_id: int, service: str, date: str, time: str) -> BookingResult:
        """
        FIX: цена берётся из БД (через settings.SERVICES, синхронизированного
        ServiceService._sync), а не из захардкоженного словаря.
        """
        if service not in settings.SERVICES:
            return BookingResult(ok=False, error="Неизвестная услуга.")
        if time not in settings.TIME_SLOTS:
            return BookingResult(ok=False, error="Некорректное время.")
        price = settings.SERVICES[service]
        try:
            with atomic() as db:
                if BlockedDayRepo.is_blocked(db, date):
                    return BookingResult(ok=False, error="День закрыт для записи.")
                if time in BlockedSlotRepo.blocked_for_date(db, date):

                    return BookingResult(ok=False, error="Этот слот недоступен — выберите другое время.")
                booked = BookingRepo.booked_times(db, date)
                if time in booked:
                    return BookingResult(ok=False,
                                        error="Слот только что заняли — выберите другое время.")
                if len(booked) >= settings.MAX_SLOTS_PER_DAY:
                    return BookingResult(ok=False, error="День полностью занят.")
                # Лимит активных записей на одного клиента
                active_count = db.execute(
                    "SELECT COUNT(*) FROM bookings WHERE user_id=? AND status='active'",
                    (user_id,),
                ).fetchone()[0]
                if active_count >= self.MAX_ACTIVE_PER_CLIENT:
                    return BookingResult(
                        ok=False,
                        error=f"У вас уже {active_count} активных "
                              f"{_plural(active_count, 'запись', 'записи', 'записей')}. "
                              f"Максимум — {self.MAX_ACTIVE_PER_CLIENT}. "
                              f"Отмените одну из существующих, чтобы создать новую.",
                    )
                bid     = BookingRepo.insert(db, user_id, service, price, date, time)
                booking = BookingRepo.get(db, bid)
            client = self._get_client_name(user_id)
            enqueue(ExcelRow("запись", client[0], client[1],
                             service, date, time, price, "active"))
            logger.info("create bid=%s uid=%s %s %s", bid, user_id, date, time)
            return BookingResult(ok=True, booking=booking)
        except Exception as e:
            logger.error("create uid=%s: %s", user_id, e)
            return BookingResult(ok=False, error="Не удалось создать запись. Попробуйте ещё раз.")

    def cancel(self, bid: int, by_user: int) -> BookingResult:
        with get_db() as db:
            booking = BookingRepo.get(db, bid)
        if not booking:
            return BookingResult(ok=False, error="Запись не найдена.")
        if booking.status != BookingStatus.ACTIVE:
            return BookingResult(ok=False, error="Запись уже неактивна.")
        if booking.user_id != by_user and by_user not in settings.ADMIN_IDS:
            return BookingResult(ok=False, error="Нет прав для отмены.")
        try:
            with atomic() as db:
                BookingRepo.set_status(db, bid, BookingStatus.CANCELLED)
                RescheduleRepo.delete_by_booking(db, bid)
            client = self._get_client_name(booking.user_id)
            enqueue(ExcelRow("отмена", client[0], client[1],
                             booking.service, booking.date, booking.time, 0, "cancelled"))
            logger.info("cancel bid=%s by=%s", bid, by_user)
            return BookingResult(ok=True, booking=booking)
        except Exception as e:
            logger.error("cancel bid=%s: %s", bid, e)
            return BookingResult(ok=False, error="Не удалось отменить.")

    def confirm_reschedule(self, booking_id: int) -> BookingResult:
        with get_db() as db:
            rr      = RescheduleRepo.get_by_booking(db, booking_id)
            booking = BookingRepo.get(db, booking_id)
        if not rr or not booking:
            return BookingResult(ok=False, error="Запрос на перенос не найден.")
        try:
            with atomic() as db:
                booked = BookingRepo.booked_times(db, rr.new_date)
                if rr.new_time in booked:
                    return BookingResult(ok=False, error="Слот уже занят.")
                BookingRepo.set_slot(db, booking_id, rr.new_date, rr.new_time)
                RescheduleRepo.delete(db, rr.id)
                updated = BookingRepo.get(db, booking_id)
            return BookingResult(ok=True, booking=updated)
        except Exception as e:
            logger.error("confirm_reschedule bid=%s: %s", booking_id, e)
            return BookingResult(ok=False, error="Не удалось перенести.")

    def decline_reschedule(self, booking_id: int):
        with atomic() as db:
            RescheduleRepo.delete_by_booking(db, booking_id)

    def request_reschedule(self, booking_id: int,
                           new_date: str, new_time: str) -> bool:
        expires = utc_plus_hours(settings.RESCHEDULE_TIMEOUT_HOURS)
        with atomic() as db:
            RescheduleRepo.create(db, booking_id, new_date, new_time, expires)
        return True

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
            logger.info("complete_past: %d bookings → completed", count)
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
        FIX: используем дату визита (b.date), а не created_at_utc записи.
        Кандидаты — записи, у которых дата визита <= сегодня - REVIEW_DELAY_HOURS.
        """
        cutoff = review_cutoff_utc()
        with get_db() as db:
            return BookingRepo.review_candidates(db, cutoff)

    def expire_reschedules(self) -> list[RescheduleRequest]:
        """FIX: удаляем все истёкшие за одну транзакцию."""
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
        return (c.name if c else "—"), (c.phone if c else "—")

class NoteService:
    """FIX: был ошибочно склеен с ServiceService."""

    def add(self, user_id: int, author_id: int, text: str):
        with atomic() as db:
            NoteRepo.insert(db, user_id, author_id, text)
        logger.info("note user=%s by=%s", user_id, author_id)

class ServiceService:
    """Управление услугами. После каждого изменения синхронизирует settings.SERVICES."""

    def sync(self) -> dict[str, int]:
        """Загружает актуальный список услуг из БД и обновляет settings.SERVICES."""
        with get_db() as db:
            rows = ServiceRepo.all_active(db)
        settings.update_services(dict(rows))
        return settings.SERVICES

    # публичный псевдоним для main.py
    def _sync(self) -> dict[str, int]:
        return self.sync()

    def all(self) -> dict[str, int]:
        return self.sync()

    def add(self, name: str, price: int) -> tuple[bool, str]:
        name = name.strip()
        if not name:
            return False, "Название не может быть пустым."
        if price <= 0:
            return False, "Цена должна быть больше 0."
        with atomic() as db:
            ok = ServiceRepo.insert(db, name, price)
        if not ok:
            return False, f"Услуга «{name}» уже существует."
        self.sync()
        return True, ""

    def update_price(self, name: str, new_price: int) -> tuple[bool, str]:
        if new_price <= 0:
            return False, "Цена должна быть больше 0."
        with atomic() as db:
            ServiceRepo.update_price(db, name, new_price)
        self.sync()
        return True, ""

    def rename(self, old_name: str, new_name: str) -> tuple[bool, str]:
        new_name = new_name.strip()
        if not new_name:
            return False, "Название не может быть пустым."
        with atomic() as db:
            ok = ServiceRepo.rename(db, old_name, new_name)
        if not ok:
            return False, f"Услуга «{new_name}» уже существует."
        self.sync()
        return True, ""

    def delete(self, name: str) -> tuple[bool, str]:
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM bookings WHERE service=? AND status='active'",
                (name,),
            ).fetchone()[0]
        if count > 0:
            return False, f"Нельзя удалить: есть {count} активных записей на «{name}»."
        with atomic() as db:
            ServiceRepo.delete(db, name)
        self.sync()
        return True, ""

class ReviewService:

    def add(self, user_id: int, booking_id: int,
            rating: int, text: str) -> int:
        """Создаёт отзыв. Возвращает review_id для последующего прикрепления фото."""
        with atomic() as db:
            rid = ReviewRepo.insert(db, user_id, booking_id, rating, text)
        logger.info("review uid=%s bid=%s rating=%s", user_id, booking_id, rating)
        return rid

    def attach_photo(self, review_id: int, photo_file_id: str):
        """Прикрепляет фото к уже созданному отзыву."""
        with atomic() as db:
            ReviewRepo.update_photo(db, review_id, photo_file_id)
        logger.info("review photo review_id=%s", review_id)

    def recent(self, limit: int = 20) -> list[Review]:
        with get_db() as db:
            return ReviewRepo.recent(db, limit)

class TimeSlotService:
    """Управление временными слотами мастера.
    После каждого изменения синхронизирует settings.TIME_SLOTS.
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
        """Добавить слот. start/end в формате HH:MM."""
        import re
        _t = re.compile(r"^\d{2}:\d{2}$")
        if not _t.match(start) or not _t.match(end):
            return False, "Формат времени: ЧЧ:ММ (например 10:00)"
        if start >= end:
            return False, "Время начала должно быть раньше конца."
        with atomic() as db:
            ok = TimeSlotRepo.insert(db, start, end)
        if not ok:
            return False, f"Слот {start} уже существует."
        self.sync()
        return True, ""

    def toggle(self, start: str, active: bool) -> tuple[bool, str]:
        """Включить / выключить слот."""
        with atomic() as db:
            TimeSlotRepo.set_active(db, start, active)
        self.sync()
        return True, ""

    def delete(self, start: str) -> tuple[bool, str]:
        # Проверяем активные записи на этот слот
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM bookings WHERE time=? AND status='active'",
                (start,),
            ).fetchone()[0]
        if count > 0:
            return False, f"Нельзя удалить: есть {count} активных записей на {start}."
        with atomic() as db:
            TimeSlotRepo.delete(db, start)
        self.sync()
        return True, ""

# ════════════════════════════════════════════════════════════════════════════════
# BlacklistService
# ════════════════════════════════════════════════════════════════════════════════


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
    """Сервис портфолио — фото работ мастера.
    Использует единственное WAL-соединение через get_db()/atomic().
    """

    def __init__(self, db_path: str = ""):
        pass  # db_path для обратной совместимости

    def all(self) -> list[dict]:
        with get_db() as db:
            return PortfolioRepo.all(db)

    def add(self, file_id: str) -> tuple[bool, str]:
        with get_db() as db:
            count = PortfolioRepo.count(db)
        if count >= PortfolioRepo.MAX_PHOTOS:
            return False, f"Максимум {PortfolioRepo.MAX_PHOTOS} фото. Удалите лишние."
        with atomic() as db:
            PortfolioRepo.add_atomic(db, file_id)
        return True, ""

    def delete(self, photo_id: int) -> bool:
        with atomic() as db:
            return PortfolioRepo.delete_atomic(db, photo_id)

    def count(self) -> int:
        with get_db() as db:
            return PortfolioRepo.count(db)
