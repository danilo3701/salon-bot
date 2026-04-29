"""
app/repositories/repo.py — чистый SQL.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from app.core.time_utils import parse_dt
from app.models.domain import (
    Booking, BookingStatus, Client,
    Note, RescheduleRequest, Review,
)


def _booking(r: sqlite3.Row) -> Booking:
    return Booking(
        id=r["id"], user_id=r["user_id"],
        service=r["service"], price=r["price"],
        date=r["date"], time=r["time"],
        status=BookingStatus(r["status"]),
        notified_1h=bool(r["notified_1h"]),
        notified_24h=bool(r["notified_24h"]),
        notified_return=bool(r["notified_return"]),
        review_sent=bool(r["review_sent"]),
        created_at_utc=parse_dt(r["created_at_utc"]),
    )


def _client(r: sqlite3.Row) -> Client:
    return Client(
        user_id=r["user_id"], username=r["username"],
        first_name=r["first_name"], name=r["name"], phone=r["phone"],
        registered_at=parse_dt(r["registered_at"]),
    )


def _review(r: sqlite3.Row) -> Review:
    return Review(
        id=r["id"], user_id=r["user_id"], booking_id=r["booking_id"],
        rating=r["rating"], text=r["text"],
        photo_file_id=r["photo_file_id"] or "",
        created_at=parse_dt(r["created_at_utc"]),
    )


# ── ClientRepo ────────────────────────────────────────────────────────────────

class ClientRepo:

    @staticmethod
    def upsert(db: sqlite3.Connection,
               user_id: int, username: Optional[str], first_name: str):
        db.execute(
            """INSERT INTO clients (user_id, username, first_name)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username   = excluded.username,
                 first_name = excluded.first_name""",
            (user_id, username, first_name),
        )

    @staticmethod
    def set_profile(db: sqlite3.Connection,
                    user_id: int, name: str, phone: str):
        if not name or not name.strip():
            raise ValueError("Имя клиента не может быть пустым")
        if not phone or not phone.strip():
            raise ValueError("Телефон клиента не может быть пустым")
        db.execute(
            "UPDATE clients SET name=?, phone=? WHERE user_id=?",
            (name.strip(), phone.strip(), user_id),
        )

    @staticmethod
    def get(db: sqlite3.Connection, user_id: int) -> Optional[Client]:
        r = db.execute("SELECT * FROM clients WHERE user_id=?", (user_id,)).fetchone()
        return _client(r) if r else None

    @staticmethod
    def all_registered(db: sqlite3.Connection) -> list[Client]:
        rows = db.execute(
            "SELECT * FROM clients WHERE name != '' AND phone != '' ORDER BY registered_at DESC"
        ).fetchall()
        return [_client(r) for r in rows]

    @staticmethod
    def delete_profile(db: sqlite3.Connection, user_id: int):
        """Удаляет профиль клиента (имя, телефон). Записи остаются в истории."""
        db.execute(
            "UPDATE clients SET name='', phone='', username='' WHERE user_id=?",
            (user_id,),
        )

    @staticmethod
    def upsert_by_phone(db: sqlite3.Connection, name: str, phone: str) -> int:
        """Импорт: создаёт или обновляет клиента по телефону.

        Существующий Telegram-клиент с таким телефоном — обновляем имя.
        Новый импортированный клиент — получает отрицательный synthetic user_id
        из счётчика imported_client_seq (никогда не совпадёт с Telegram ID).
        """
        existing = db.execute(
            "SELECT user_id FROM clients WHERE phone=?", (phone,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE clients SET name=? WHERE phone=?", (name, phone)
            )
            return existing["user_id"]
        else:
            # Атомарно получаем следующий отрицательный ID и декрементируем счётчик
            row = db.execute(
                "UPDATE imported_client_seq SET next_id = next_id - 1 "
                "WHERE id = 1 RETURNING next_id + 1"
            ).fetchone()
            synthetic_id = row[0] if row else None
            if synthetic_id is None:
                # Fallback: считаем min существующего и идём ниже
                min_row = db.execute(
                    "SELECT MIN(user_id) FROM clients WHERE user_id < 0"
                ).fetchone()
                synthetic_id = (min_row[0] - 1) if (min_row and min_row[0] is not None) else -1
            db.execute(
                "INSERT INTO clients (user_id, username, name, phone) VALUES (?,?,?,?)",
                (synthetic_id, "", name, phone),
            )
            return synthetic_id


# ── BookingRepo ───────────────────────────────────────────────────────────────

class BookingRepo:

    @staticmethod
    def insert(db: sqlite3.Connection,
               user_id: int, service: str, price: int,
               date: str, time: str) -> int:
        return db.execute(
            "INSERT INTO bookings (user_id, service, price, date, time) VALUES (?,?,?,?,?)",
            (user_id, service, price, date, time),
        ).lastrowid

    @staticmethod
    def get(db: sqlite3.Connection, bid: int) -> Optional[Booking]:
        r = db.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
        return _booking(r) if r else None

    @staticmethod
    def set_status(db: sqlite3.Connection, bid: int, status: BookingStatus):
        db.execute("UPDATE bookings SET status=? WHERE id=?", (status.value, bid))

    @staticmethod
    def set_slot(db: sqlite3.Connection, bid: int, date: str, time: str):
        db.execute("UPDATE bookings SET date=?, time=? WHERE id=?", (date, time, bid))

    @staticmethod
    def mark(db: sqlite3.Connection, bid: int, field: str):
        _ALLOWED = {"notified_1h", "notified_24h", "notified_return", "review_sent"}
        assert field in _ALLOWED, f"Unknown field: {field}"
        db.execute(f"UPDATE bookings SET {field}=1 WHERE id=?", (bid,))

    @staticmethod
    def booked_times(db: sqlite3.Connection, date: str) -> set[str]:
        rows = db.execute(
            "SELECT time FROM bookings WHERE date=? AND status='active'", (date,)
        ).fetchall()
        return {r["time"] for r in rows}

    @staticmethod
    def booked_times_bulk(db: sqlite3.Connection,
                          dates: list[str]) -> dict[str, set[str]]:
        if not dates:
            return {}
        ph = ",".join("?" * len(dates))
        rows = db.execute(
            f"SELECT date, time FROM bookings WHERE date IN ({ph}) AND status='active'",
            dates,
        ).fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["date"], set()).add(r["time"])
        return out

    @staticmethod
    def by_date(db: sqlite3.Connection, date: str) -> list[Booking]:
        rows = db.execute(
            "SELECT * FROM bookings WHERE date=? AND status='active' ORDER BY time",
            (date,),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def future(db: sqlite3.Connection, from_date: str) -> list[Booking]:
        rows = db.execute(
            "SELECT * FROM bookings WHERE date>=? AND status='active' ORDER BY date, time",
            (from_date,),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def user_active(db: sqlite3.Connection,
                    user_id: int, from_date: str) -> list[Booking]:
        rows = db.execute(
            "SELECT * FROM bookings WHERE user_id=? AND date>=? AND status='active'"
            " ORDER BY date, time",
            (user_id, from_date),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def user_past(db: sqlite3.Connection,
                  user_id: int, before_date: str) -> list[Booking]:
        rows = db.execute(
            "SELECT * FROM bookings WHERE user_id=? AND date<? AND status IN ('active','completed')"
            " ORDER BY date DESC, time DESC LIMIT 10",
            (user_id, before_date),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def user_all(db: sqlite3.Connection, user_id: int) -> list[Booking]:
        rows = db.execute(
            "SELECT * FROM bookings WHERE user_id=? ORDER BY date DESC, time DESC",
            (user_id,),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def pending_reminders(db: sqlite3.Connection, today: str,
                          limit: int = 200) -> list[Booking]:
        """Возвращает не более `limit` записей за раз — защита event loop."""
        rows = db.execute(
            "SELECT * FROM bookings WHERE status='active'"
            " AND date >= ?"
            " AND (notified_24h=0 OR notified_1h=0)"
            " ORDER BY date, time"
            " LIMIT ?",
            (today, limit),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def return_candidates(db: sqlite3.Connection, target_date: str) -> list[Booking]:
        """Записи ровно N дней назад без будущих активных записей."""
        rows = db.execute(
            """SELECT b.* FROM bookings b
               WHERE b.date = ? AND b.status IN ('active','completed')
                 AND b.notified_return = 0
                 AND b.user_id NOT IN (
                     SELECT user_id FROM bookings
                     WHERE date > ? AND status = 'active'
                 )""",
            (target_date, target_date),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def review_candidates(db: sqlite3.Connection,
                          cutoff_utc: str) -> list[Booking]:
        """Записи, после которых прошло >= REVIEW_DELAY_HOURS с момента визита.

        FIX: сравниваем дату+время визита (date || ' ' || time) с cutoff,
        а не created_at_utc (время создания записи), которое может быть
        за месяц до визита.
        cutoff_utc — UTC-момент «сейчас минус REVIEW_DELAY_HOURS».
        """
        rows = db.execute(
            "SELECT * FROM bookings WHERE status IN ('active','completed')"
            " AND review_sent=0"
            " AND datetime(date || ' ' || time) <= datetime(?, 'localtime')",
            (cutoff_utc,),
        ).fetchall()
        return [_booking(r) for r in rows]

    @staticmethod
    def complete_past(db: sqlite3.Connection, today: str) -> int:
        cur = db.execute(
            "UPDATE bookings SET status='completed' WHERE status='active' AND date < ?",
            (today,),
        )
        return cur.rowcount

    @staticmethod
    def stats(db: sqlite3.Connection, month: str) -> dict:
        def one(sql, *p):
            return db.execute(sql, p).fetchone()[0]

        return {
            "total":         one("SELECT COUNT(*) FROM bookings WHERE status IN ('active','completed')"),
            "revenue":       one("SELECT COALESCE(SUM(price),0) FROM bookings WHERE status='completed'"),
            "month_count":   one("SELECT COUNT(*) FROM bookings WHERE date LIKE ? AND status IN ('active','completed')", f"{month}%"),
            "month_revenue": one("SELECT COALESCE(SUM(price),0) FROM bookings WHERE date LIKE ? AND status='completed'", f"{month}%"),
            "cancelled":     one("SELECT COUNT(*) FROM bookings WHERE status='cancelled'"),
            "clients":       one("SELECT COUNT(DISTINCT user_id) FROM bookings WHERE status IN ('active','completed')"),
            "avg_rating":    one("SELECT AVG(CAST(rating AS REAL)) FROM reviews"),
        }


# ── BlockedDayRepo ────────────────────────────────────────────────────────────

class BlockedDayRepo:

    @staticmethod
    def is_blocked(db: sqlite3.Connection, date: str) -> bool:
        return db.execute(
            "SELECT 1 FROM blocked_days WHERE date=?", (date,)
        ).fetchone() is not None

    @staticmethod
    def blocked_set(db: sqlite3.Connection) -> set[str]:
        return {r["date"] for r in db.execute("SELECT date FROM blocked_days").fetchall()}

    @staticmethod
    def all(db: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = db.execute("SELECT date, note FROM blocked_days ORDER BY date").fetchall()
        return [(r["date"], r["note"]) for r in rows]

    @staticmethod
    def insert(db: sqlite3.Connection, date: str, note: str = "") -> bool:
        try:
            db.execute("INSERT INTO blocked_days (date, note) VALUES (?,?)", (date, note))
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def delete(db: sqlite3.Connection, date: str):
        db.execute("DELETE FROM blocked_days WHERE date=?", (date,))


# ── RescheduleRepo ────────────────────────────────────────────────────────────

class RescheduleRepo:

    @staticmethod
    def create(db: sqlite3.Connection,
               booking_id: int, new_date: str, new_time: str,
               expires_at: str) -> int:
        db.execute("DELETE FROM reschedule_requests WHERE booking_id=?", (booking_id,))
        return db.execute(
            "INSERT INTO reschedule_requests (booking_id, new_date, new_time, expires_at)"
            " VALUES (?,?,?,?)",
            (booking_id, new_date, new_time, expires_at),
        ).lastrowid

    @staticmethod
    def get_by_booking(db: sqlite3.Connection,
                       booking_id: int) -> Optional[RescheduleRequest]:
        r = db.execute(
            "SELECT * FROM reschedule_requests WHERE booking_id=?", (booking_id,)
        ).fetchone()
        if not r:
            return None
        return RescheduleRequest(
            id=r["id"], booking_id=r["booking_id"],
            new_date=r["new_date"], new_time=r["new_time"],
            expires_at=parse_dt(r["expires_at"]),
        )

    @staticmethod
    def expired(db: sqlite3.Connection) -> list[RescheduleRequest]:
        rows = db.execute(
            "SELECT * FROM reschedule_requests"
            " WHERE expires_at < strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        ).fetchall()
        return [
            RescheduleRequest(
                id=r["id"], booking_id=r["booking_id"],
                new_date=r["new_date"], new_time=r["new_time"],
                expires_at=parse_dt(r["expires_at"]),
            )
            for r in rows
        ]

    @staticmethod
    def delete(db: sqlite3.Connection, rr_id: int):
        db.execute("DELETE FROM reschedule_requests WHERE id=?", (rr_id,))

    @staticmethod
    def delete_by_booking(db: sqlite3.Connection, booking_id: int):
        db.execute("DELETE FROM reschedule_requests WHERE booking_id=?", (booking_id,))



# ── ServiceRepo ───────────────────────────────────────────────────────────────

class ServiceRepo:

    @staticmethod
    def all_active(db: sqlite3.Connection) -> list[tuple[str, int]]:
        """Возвращает список (name, price) активных услуг, отсортированных."""
        rows = db.execute(
            "SELECT name, price FROM services WHERE is_active=1 ORDER BY sort_order, id"
        ).fetchall()
        return [(r["name"], r["price"]) for r in rows]

    @staticmethod
    def get(db: sqlite3.Connection, name: str) -> Optional[tuple[str, int]]:
        r = db.execute(
            "SELECT name, price FROM services WHERE name=? AND is_active=1", (name,)
        ).fetchone()
        return (r["name"], r["price"]) if r else None

    @staticmethod
    def insert(db: sqlite3.Connection, name: str, price: int) -> bool:
        try:
            max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) FROM services").fetchone()[0]
            db.execute(
                "INSERT INTO services (name, price, sort_order) VALUES (?,?,?)",
                (name.strip(), price, max_order + 1),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def update_price(db: sqlite3.Connection, name: str, new_price: int):
        db.execute("UPDATE services SET price=? WHERE name=?", (new_price, name))

    @staticmethod
    def rename(db: sqlite3.Connection, old_name: str, new_name: str) -> bool:
        try:
            db.execute("UPDATE services SET name=? WHERE name=?", (new_name.strip(), old_name))
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def delete(db: sqlite3.Connection, name: str):
        db.execute("DELETE FROM services WHERE name=?", (name,))

    @staticmethod
    def seed(db: sqlite3.Connection, services: dict[str, int]):
        """Наполняет таблицу дефолтными значениями, если она пустая."""
        count = db.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        if count == 0:
            for order, (name, price) in enumerate(services.items()):
                db.execute(
                    "INSERT OR IGNORE INTO services (name, price, sort_order) VALUES (?,?,?)",
                    (name, price, order),
                )


class NoteRepo:

    @staticmethod
    def insert(db: sqlite3.Connection,
               user_id: int, author_id: int, text: str):
        db.execute(
            "INSERT INTO client_notes (user_id, author_id, note) VALUES (?,?,?)",
            (user_id, author_id, text),
        )

    @staticmethod
    def for_client(db: sqlite3.Connection, user_id: int) -> list[Note]:
        rows = db.execute(
            "SELECT * FROM client_notes WHERE user_id=? ORDER BY created_at_utc DESC",
            (user_id,),
        ).fetchall()
        return [
            Note(id=r["id"], user_id=r["user_id"], author_id=r["author_id"],
                 text=r["note"], created_at=parse_dt(r["created_at_utc"]))
            for r in rows
        ]


# ── ReviewRepo ────────────────────────────────────────────────────────────────

class ReviewRepo:

    @staticmethod
    def insert(db: sqlite3.Connection,
               user_id: int, booking_id: int,
               rating: int, text: str,
               photo_file_id: str = "") -> int:
        """Возвращает id нового отзыва. Отзыв можно оставить после каждого визита."""
        cur = db.execute(
            "INSERT INTO reviews (user_id, booking_id, rating, text, photo_file_id)"
            " VALUES (?,?,?,?,?)",
            (user_id, booking_id, rating, text, photo_file_id),
        )
        return cur.lastrowid

    @staticmethod
    def update_photo(db: sqlite3.Connection, review_id: int, photo_file_id: str):
        db.execute(
            "UPDATE reviews SET photo_file_id=? WHERE id=?",
            (photo_file_id, review_id),
        )

    @staticmethod
    def recent(db: sqlite3.Connection, limit: int = 20) -> list[Review]:
        rows = db.execute(
            "SELECT * FROM reviews ORDER BY created_at_utc DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_review(r) for r in rows]

    @staticmethod
    def for_client(db: sqlite3.Connection, user_id: int) -> list[Review]:
        rows = db.execute(
            "SELECT * FROM reviews WHERE user_id=? ORDER BY created_at_utc DESC",
            (user_id,),
        ).fetchall()
        return [_review(r) for r in rows]


# ── TimeSlotRepo ──────────────────────────────────────────────────────────────

class TimeSlotRepo:

    @staticmethod
    def all_active(db: sqlite3.Connection) -> dict[str, str]:
        """Возвращает {start: end} для активных слотов, отсортированных."""
        rows = db.execute(
            "SELECT start_time, end_time FROM time_slots"
            " WHERE is_active=1 ORDER BY sort_order, start_time"
        ).fetchall()
        return {r["start_time"]: r["end_time"] for r in rows}

    @staticmethod
    def all(db: sqlite3.Connection) -> list[tuple[str, str, bool]]:
        """Возвращает [(start, end, is_active), ...] всех слотов."""
        rows = db.execute(
            "SELECT start_time, end_time, is_active FROM time_slots ORDER BY sort_order, start_time"
        ).fetchall()
        return [(r["start_time"], r["end_time"], bool(r["is_active"])) for r in rows]

    @staticmethod
    def insert(db: sqlite3.Connection, start: str, end: str) -> bool:
        try:
            max_order = db.execute(
                "SELECT COALESCE(MAX(sort_order),0) FROM time_slots"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO time_slots (start_time, end_time, sort_order) VALUES (?,?,?)",
                (start, end, max_order + 1),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def set_active(db: sqlite3.Connection, start: str, is_active: bool):
        db.execute(
            "UPDATE time_slots SET is_active=? WHERE start_time=?",
            (1 if is_active else 0, start),
        )

    @staticmethod
    def delete(db: sqlite3.Connection, start: str):
        db.execute("DELETE FROM time_slots WHERE start_time=?", (start,))

    @staticmethod
    def seed(db: sqlite3.Connection, slots: dict[str, str]):
        """Заполняет дефолтными значениями если таблица пустая."""
        count = db.execute("SELECT COUNT(*) FROM time_slots").fetchone()[0]
        if count == 0:
            for order, (start, end) in enumerate(slots.items()):
                db.execute(
                    "INSERT OR IGNORE INTO time_slots (start_time, end_time, sort_order)"
                    " VALUES (?,?,?)",
                    (start, end, order),
                )


# ── BlockedSlotRepo ───────────────────────────────────────────────────────────

class WeeklyScheduleRepo:

    @staticmethod
    def ensure_day(db: sqlite3.Connection, weekday: int):
        db.execute(
            "INSERT OR IGNORE INTO weekly_day_templates (weekday, is_open) VALUES (?, 1)",
            (weekday,),
        )

    @staticmethod
    def get_day_template(db: sqlite3.Connection, weekday: int) -> dict:
        WeeklyScheduleRepo.ensure_day(db, weekday)
        row = db.execute(
            "SELECT weekday, is_open, closed_start, closed_end "
            "FROM weekly_day_templates WHERE weekday=?",
            (weekday,),
        ).fetchone()
        trows = db.execute(
            "SELECT hhmm FROM weekly_day_times WHERE weekday=? ORDER BY sort_order, hhmm",
            (weekday,),
        ).fetchall()
        return {
            "weekday": weekday,
            "is_open": bool(row["is_open"]) if row else True,
            "closed_start": row["closed_start"] if row else None,
            "closed_end": row["closed_end"] if row else None,
            "times": [r["hhmm"] for r in trows],
        }

    @staticmethod
    def all_day_templates(db: sqlite3.Connection) -> list[dict]:
        rows = db.execute(
            "SELECT weekday, is_open, closed_start, closed_end "
            "FROM weekly_day_templates ORDER BY weekday"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            trows = db.execute(
                "SELECT hhmm FROM weekly_day_times WHERE weekday=? ORDER BY sort_order, hhmm",
                (row["weekday"],),
            ).fetchall()
            out.append({
                "weekday": row["weekday"],
                "is_open": bool(row["is_open"]),
                "closed_start": row["closed_start"],
                "closed_end": row["closed_end"],
                "times": [r["hhmm"] for r in trows],
            })
        return out

    @staticmethod
    def add_time(db: sqlite3.Connection, weekday: int, hhmm: str) -> bool:
        WeeklyScheduleRepo.ensure_day(db, weekday)
        try:
            max_order = db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM weekly_day_times WHERE weekday=?",
                (weekday,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO weekly_day_times (weekday, hhmm, sort_order) VALUES (?,?,?)",
                (weekday, hhmm, max_order + 1),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def remove_time(db: sqlite3.Connection, weekday: int, hhmm: str):
        db.execute(
            "DELETE FROM weekly_day_times WHERE weekday=? AND hhmm=?",
            (weekday, hhmm),
        )

    @staticmethod
    def set_day_open(db: sqlite3.Connection, weekday: int, is_open: bool):
        WeeklyScheduleRepo.ensure_day(db, weekday)
        db.execute(
            "UPDATE weekly_day_templates "
            "SET is_open=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE weekday=?",
            (1 if is_open else 0, weekday),
        )

    @staticmethod
    def set_closed_period(db: sqlite3.Connection, weekday: int, start: str, end: str):
        WeeklyScheduleRepo.ensure_day(db, weekday)
        db.execute(
            "UPDATE weekly_day_templates "
            "SET closed_start=?, closed_end=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE weekday=?",
            (start, end, weekday),
        )

    @staticmethod
    def clear_closed_period(db: sqlite3.Connection, weekday: int):
        WeeklyScheduleRepo.ensure_day(db, weekday)
        db.execute(
            "UPDATE weekly_day_templates "
            "SET closed_start=NULL, closed_end=NULL, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE weekday=?",
            (weekday,),
        )

    @staticmethod
    def replace_day_times(db: sqlite3.Connection, source_weekday: int, target_weekday: int) -> int:
        WeeklyScheduleRepo.ensure_day(db, source_weekday)
        WeeklyScheduleRepo.ensure_day(db, target_weekday)
        src_rows = db.execute(
            "SELECT hhmm, sort_order FROM weekly_day_times WHERE weekday=? ORDER BY sort_order, hhmm",
            (source_weekday,),
        ).fetchall()
        db.execute("DELETE FROM weekly_day_times WHERE weekday=?", (target_weekday,))
        for row in src_rows:
            db.execute(
                "INSERT INTO weekly_day_times (weekday, hhmm, sort_order) VALUES (?,?,?)",
                (target_weekday, row["hhmm"], row["sort_order"]),
            )
        db.execute(
            "UPDATE weekly_day_templates "
            "SET updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE weekday=?",
            (target_weekday,),
        )
        return len(src_rows)


class BlockedSlotRepo:

    @staticmethod
    def block(db: sqlite3.Connection, date: str, time: str):
        db.execute(
            "INSERT OR IGNORE INTO blocked_slots (date, time) VALUES (?, ?)",
            (date, time),
        )

    @staticmethod
    def unblock(db: sqlite3.Connection, date: str, time: str):
        db.execute(
            "DELETE FROM blocked_slots WHERE date=? AND time=?",
            (date, time),
        )

    @staticmethod
    def blocked_for_date(db: sqlite3.Connection, date: str) -> set[str]:
        rows = db.execute(
            "SELECT time FROM blocked_slots WHERE date=?", (date,)
        ).fetchall()
        return {r["time"] for r in rows}

    @staticmethod
    def blocked_bulk(db: sqlite3.Connection,
                     dates: list[str]) -> dict[str, set[str]]:
        if not dates:
            return {}
        ph = ",".join("?" * len(dates))
        rows = db.execute(
            f"SELECT date, time FROM blocked_slots WHERE date IN ({ph})", dates
        ).fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["date"], set()).add(r["time"])
        return out


# ── BlacklistRepo ─────────────────────────────────────────────────────────────

class BlacklistRepo:

    @staticmethod
    def add(db: sqlite3.Connection, user_id: int, reason: str = ""):
        db.execute(
            "INSERT OR IGNORE INTO blacklist (user_id, reason) VALUES (?, ?)",
            (user_id, reason),
        )

    @staticmethod
    def remove(db: sqlite3.Connection, user_id: int):
        db.execute("DELETE FROM blacklist WHERE user_id=?", (user_id,))

    @staticmethod
    def is_banned(db: sqlite3.Connection, user_id: int) -> bool:
        row = db.execute(
            "SELECT 1 FROM blacklist WHERE user_id=?", (user_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def all(db: sqlite3.Connection) -> list:
        return db.execute(
            "SELECT user_id, reason, created_at FROM blacklist ORDER BY created_at DESC"
        ).fetchall()


class PortfolioRepo:
    """Хранение file_id фото портфолио мастера (макс. 10 штук)."""

    MAX_PHOTOS = 10

    @staticmethod
    def all(conn) -> list[dict]:
        """Возвращает список фото. conn — sqlite3.Connection (row_factory=Row или tuple)."""
        rows = conn.execute(
            "SELECT id, file_id, position FROM portfolio ORDER BY position, id"
        ).fetchall()
        # Поддерживаем и Row (основное соединение), и tuple (legacy)
        try:
            return [{"id": r["id"], "file_id": r["file_id"], "position": r["position"]} for r in rows]
        except (IndexError, KeyError, TypeError):
            return [{"id": r[0], "file_id": r[1], "position": r[2]} for r in rows]

    @staticmethod
    def add_atomic(db, file_id: str):
        """Добавляет фото в рамках транзакции atomic()."""
        pos = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM portfolio").fetchone()[0]
        db.execute("INSERT INTO portfolio (file_id, position) VALUES (?, ?)", (file_id, pos))

    @staticmethod
    def delete_atomic(db, photo_id: int) -> bool:
        """Удаляет фото в рамках транзакции atomic()."""
        cur = db.execute("DELETE FROM portfolio WHERE id=?", (photo_id,))
        return cur.rowcount > 0

    # Оставляем для обратной совместимости (если где-то вызывается напрямую)
    @staticmethod
    def add(conn, file_id: str) -> tuple[bool, str]:
        count = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
        if count >= PortfolioRepo.MAX_PHOTOS:
            return False, f"Максимум {PortfolioRepo.MAX_PHOTOS} фото. Удалите лишние."
        pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM portfolio").fetchone()[0]
        conn.execute("INSERT INTO portfolio (file_id, position) VALUES (?, ?)", (file_id, pos))
        try:
            conn.commit()
        except Exception:
            pass
        return True, ""

    @staticmethod
    def delete(conn, photo_id: int) -> bool:
        cur = conn.execute("DELETE FROM portfolio WHERE id=?", (photo_id,))
        try:
            conn.commit()
        except Exception:
            pass
        return cur.rowcount > 0

    @staticmethod
    def count(conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]


class RuntimeSettingRepo:
    """Runtime settings editable from bot UI and persisted in DB."""

    @staticmethod
    def get(db: sqlite3.Connection, key: str) -> Optional[str]:
        row = db.execute(
            "SELECT value FROM runtime_settings WHERE key=?",
            (key,),
        ).fetchone()
        return row["value"] if row else None

    @staticmethod
    def set(db: sqlite3.Connection, key: str, value: str):
        db.execute(
            "INSERT INTO runtime_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @staticmethod
    def all(db: sqlite3.Connection) -> dict[str, str]:
        rows = db.execute("SELECT key, value FROM runtime_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
