"""
app/core/database.py

Ключевые решения:
  - isolation_level=None: отключает неявные BEGIN от Python sqlite3
  - threading.local для _depth: безопасно если что-то попадёт в executor
  - executescript() не используется: делает неявный COMMIT, ломает транзакции
  - Одно постоянное соединение на процесс (asyncio однопоточен)
  - Миграции: версионированные, идемпотентные, применяются при старте
  - v3: UNIQUE(date,time) заменён на partial index WHERE status='active'
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

from app.core.settings import settings

logger = logging.getLogger("salon.db")

_conn: sqlite3.Connection | None = None
_local = threading.local()
_conn_lock = threading.Lock()  # защита инициализации соединения


def _depth() -> int:
    return getattr(_local, "d", 0)


def _set_depth(v: int):
    _local.d = v


def get_connection() -> sqlite3.Connection:
    """Возвращает единственное соединение на процесс.
    Double-checked locking: быстрый путь без лока, медленный (инициализация) — с локом.
    """
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:           # повторная проверка под локом
                _conn = sqlite3.connect(
                    settings.DB_PATH,
                    check_same_thread=False,
                    isolation_level=None,
                )
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA foreign_keys=ON")
                _conn.execute("PRAGMA synchronous=NORMAL")
                _conn.execute("PRAGMA cache_size=-8000")
                _conn.execute("PRAGMA temp_store=MEMORY")
                logger.info("SQLite: %s", settings.DB_PATH)
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return _conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """READ — без транзакции."""
    yield get_connection()


@contextmanager
def atomic() -> Generator[sqlite3.Connection, None, None]:
    """WRITE — BEGIN IMMEDIATE / COMMIT|ROLLBACK. Вложенность через SAVEPOINT."""
    conn = get_connection()
    d = _depth()

    if d == 0:
        conn.execute("BEGIN IMMEDIATE")
        _set_depth(1)
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            _set_depth(0)
    else:
        sp = f"sp_{d}"
        conn.execute(f"SAVEPOINT {sp}")
        _set_depth(d + 1)
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            except Exception:
                pass
            raise
        finally:
            _set_depth(d)


def close():
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
        _set_depth(0)


# ══════════════════════════════════════════════════════════════════════════════
# МИГРАЦИИ
#
# Каждая миграция — это (version: int, description: str, statements: list[str]).
# Применяются строго по порядку, каждая ровно один раз.
# Текущая версия хранится в таблице schema_migrations.
#
# Правила добавления новой миграции:
#   1. Добавить новый элемент в конец списка _MIGRATIONS
#   2. Номер версии = предыдущий + 1
#   3. Никогда не редактировать уже применённые миграции
# ══════════════════════════════════════════════════════════════════════════════

_MIGRATIONS: list[tuple[int, str, list[str]]] = [

    # ── v1: начальная схема ──────────────────────────────────────────────────
    (1, "initial schema", [
        """CREATE TABLE IF NOT EXISTS clients (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            first_name     TEXT    NOT NULL DEFAULT '',
            name           TEXT    NOT NULL DEFAULT '',
            phone          TEXT    NOT NULL DEFAULT '',
            registered_at  TEXT    NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",

        """CREATE TABLE IF NOT EXISTS bookings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES clients(user_id),
            service         TEXT    NOT NULL,
            price           INTEGER NOT NULL CHECK(price > 0),
            date            TEXT    NOT NULL,
            time            TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active','cancelled','completed')),
            notified_1h     INTEGER NOT NULL DEFAULT 0,
            notified_24h    INTEGER NOT NULL DEFAULT 0,
            notified_return INTEGER NOT NULL DEFAULT 0,
            review_sent     INTEGER NOT NULL DEFAULT 0,
            created_at_utc  TEXT    NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(date, time)
        )""",

        """CREATE TABLE IF NOT EXISTS reschedule_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id   INTEGER NOT NULL REFERENCES bookings(id),
            new_date     TEXT    NOT NULL,
            new_time     TEXT    NOT NULL,
            expires_at   TEXT    NOT NULL
        )""",

        """CREATE TABLE IF NOT EXISTS blocked_days (
            date  TEXT PRIMARY KEY,
            note  TEXT NOT NULL DEFAULT ''
        )""",

        """CREATE TABLE IF NOT EXISTS client_notes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES clients(user_id),
            author_id      INTEGER NOT NULL,
            note           TEXT    NOT NULL,
            created_at_utc TEXT    NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",

        """CREATE TABLE IF NOT EXISTS reviews (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL REFERENCES clients(user_id),
            booking_id     INTEGER NOT NULL REFERENCES bookings(id),
            rating         INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            text           TEXT    NOT NULL DEFAULT '',
            photo_file_id  TEXT    NOT NULL DEFAULT '',
            created_at_utc TEXT    NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",

        """CREATE TABLE IF NOT EXISTS services (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL UNIQUE,
            price      INTEGER NOT NULL CHECK(price > 0),
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active  INTEGER NOT NULL DEFAULT 1
        )""",

        "CREATE INDEX IF NOT EXISTS idx_bookings_date   ON bookings(date, status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_user   ON bookings(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_notify ON bookings(status, date, notified_24h, notified_1h)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_return ON bookings(date, status, notified_return)",
        "CREATE INDEX IF NOT EXISTS idx_notes_user      ON client_notes(user_id, created_at_utc)",
        "CREATE INDEX IF NOT EXISTS idx_reschedule      ON reschedule_requests(booking_id)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_user    ON reviews(user_id, created_at_utc)",
    ]),

    # ── v2: таблица временных слотов ─────────────────────────────────────────
    (2, "add time_slots table", [
        """CREATE TABLE IF NOT EXISTS time_slots (
            start_time TEXT PRIMARY KEY,
            end_time   TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active  INTEGER NOT NULL DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_time_slots_active ON time_slots(is_active, sort_order)",
    ]),

    # ── v3: исправления схемы ────────────────────────────────────────────────
    #
    # Проблема 1: UNIQUE(date, time) блокирует слот после отмены записи.
    #   Клиент отменил 09:00 → никто другой не может занять тот же слот.
    #   Решение: убираем constraint через пересоздание таблицы (SQLite не умеет
    #   DROP CONSTRAINT), добавляем partial unique index только на active-записи.
    #
    # Проблема 2: expire_reschedules() делает full scan по expires_at каждый час.
    #   Решение: индекс на expires_at.
    (3, "fix unique booking constraint and add expires_at index", [
        # Пересоздаём bookings без UNIQUE(date,time)
        """CREATE TABLE IF NOT EXISTS bookings_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES clients(user_id),
            service         TEXT    NOT NULL,
            price           INTEGER NOT NULL CHECK(price > 0),
            date            TEXT    NOT NULL,
            time            TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'active'
                         CHECK(status IN ('active','cancelled','completed')),
            notified_1h     INTEGER NOT NULL DEFAULT 0,
            notified_24h    INTEGER NOT NULL DEFAULT 0,
            notified_return INTEGER NOT NULL DEFAULT 0,
            review_sent     INTEGER NOT NULL DEFAULT 0,
            created_at_utc  TEXT    NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",
        "INSERT INTO bookings_new SELECT * FROM bookings",
        "DROP TABLE bookings",
        "ALTER TABLE bookings_new RENAME TO bookings",
        # Восстанавливаем индексы из v1
        "CREATE INDEX IF NOT EXISTS idx_bookings_date   ON bookings(date, status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_user   ON bookings(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_notify ON bookings(status, date, notified_24h, notified_1h)",
        "CREATE INDEX IF NOT EXISTS idx_bookings_return ON bookings(date, status, notified_return)",
        # Partial unique index: слот уникален только среди active-записей
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_active_slot ON bookings(date, time) WHERE status='active'",
        # Индекс для быстрого поиска истёкших reschedule-запросов
        "CREATE INDEX IF NOT EXISTS idx_reschedule_expires ON reschedule_requests(expires_at)",
    ]),

    # ── v4: блокировка отдельных слотов на конкретный день ───────────────────
    (4, "add blocked_slots table", [
        """CREATE TABLE IF NOT EXISTS blocked_slots (
            date       TEXT NOT NULL,
            time       TEXT NOT NULL,
            PRIMARY KEY (date, time)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_blocked_slots_date ON blocked_slots(date)",
    ]),

    # ── v5: чёрный список клиентов ────────────────────────────────────────────
    (5, "add blacklist table", [
        """CREATE TABLE IF NOT EXISTS blacklist (
            user_id    INTEGER PRIMARY KEY,
            reason     TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",
    ]),

    # ── v6: счётчик synthetic user_id для импортированных клиентов ───────────
    #
    # Проблема: upsert_by_phone вставлял user_id=0 для всех импортированных.
    # Если импортировать двух клиентов без Telegram — PRIMARY KEY конфликт.
    # Решение: отдельная таблица-счётчик. Импортированные получают
    # отрицательные user_id (-1, -2, ...), которые никогда не совпадут с
    # реальными Telegram user_id (всегда > 0).
    (6, "add imported_client_seq for negative synthetic user_ids", [
        """CREATE TABLE IF NOT EXISTS imported_client_seq (
            id      INTEGER PRIMARY KEY CHECK(id = 1),
            next_id INTEGER NOT NULL DEFAULT -1
        )""",
        "INSERT OR IGNORE INTO imported_client_seq (id, next_id) VALUES (1, -1)",
    ]),

    # ── v7: таблица портфолио мастера ────────────────────────────
    (7, "add portfolio table for master photos", [
        """CREATE TABLE IF NOT EXISTS portfolio (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id    TEXT    NOT NULL,
            position   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
                       DEFAULT (strftime('%%Y-%%m-%%dT%%H:%%M:%%SZ','now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_portfolio_position ON portfolio(position)",
    ]),

    (8, "add weekly schedule templates", [
        """CREATE TABLE IF NOT EXISTS weekly_day_templates (
            weekday      INTEGER PRIMARY KEY CHECK(weekday BETWEEN 1 AND 7),
            is_open      INTEGER NOT NULL DEFAULT 1 CHECK(is_open IN (0,1)),
            closed_start TEXT,
            closed_end   TEXT,
            updated_at   TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )""",
        """CREATE TABLE IF NOT EXISTS weekly_day_times (
            weekday      INTEGER NOT NULL CHECK(weekday BETWEEN 1 AND 7),
            hhmm         TEXT    NOT NULL,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (weekday, hhmm),
            FOREIGN KEY (weekday) REFERENCES weekly_day_templates(weekday) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_weekly_day_times_weekday_order ON weekly_day_times(weekday, sort_order, hhmm)",
        "INSERT OR IGNORE INTO weekly_day_templates (weekday, is_open) VALUES (1,1),(2,1),(3,1),(4,1),(5,1),(6,1),(7,1)",
    ]),

]


def _ensure_migrations_table(conn: sqlite3.Connection):
    """Создаёт таблицу версий если её нет. Вызывается без транзакции — DDL."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            description TEXT    NOT NULL,
            applied_at  TEXT    NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def run_migrations():
    """
    Применяет все новые миграции из _MIGRATIONS по порядку.
    Идемпотентно — уже применённые пропускаются.
    Каждая миграция выполняется в отдельной транзакции.
    При старте на чистой БД применяет всё с v1.
    При обновлении существующего бота применяет только новые.
    """
    conn = get_connection()
    _ensure_migrations_table(conn)
    applied = _applied_versions(conn)

    pending = [(v, desc, stmts) for v, desc, stmts in _MIGRATIONS if v not in applied]
    if not pending:
        logger.info("БД актуальна (версия %d).", max(applied, default=0))
        return

    for version, description, statements in sorted(pending, key=lambda x: x[0]):
        logger.info("Миграция v%d: %s ...", version, description)
        # Временно отключаем foreign keys для DDL (ALTER TABLE и т.п.)
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            _set_depth(1)
            try:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                    (version, description),
                )
                conn.execute("COMMIT")
                logger.info("  ✓ v%d применена.", version)
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                _set_depth(0)
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    final = _applied_versions(conn)
    logger.info("БД обновлена до версии %d.", max(final))


# ── Обратная совместимость: старые точки входа ────────────────────────────────

def init_schema():
    """Устарело — используй run_migrations(). Оставлено для совместимости."""
    run_migrations()


def migrate_time_slots():
    """Устарело — миграция теперь v2 в run_migrations(). Оставлено для совместимости."""
    run_migrations()


def seed_services(default_services: dict[str, int]):
    """Наполняет таблицу services дефолтными значениями (только если пустая)."""
    from app.repositories.repo import ServiceRepo
    with atomic() as db:
        ServiceRepo.seed(db, default_services)
