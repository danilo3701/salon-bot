"""app/core/settings.py"""
from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def _req(key: str) -> str:
    v = os.getenv(key, "").strip()
    if not v:
        raise RuntimeError(f"Env var not set: {key}")
    return v


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _rate(raw: str) -> tuple[int, int]:
    try:
        n, w = raw.split("/")
        return int(n), int(w)
    except Exception:
        raise ValueError(f"Rate must be 'N/seconds', got: {raw!r}")


class Settings:
    _RUNTIME_KEY_TO_ATTR: dict[str, str] = {
        "MASTER_ADDRESS": "MASTER_ADDRESS",
        "MASTER_CONTACT": "MASTER_CONTACT",
        "MASTER_BIO": "MASTER_BIO",
        "MASTER_PHOTO_ID": "MASTER_PHOTO_ID",
    }

    def __init__(self):
        # Core
        self.BOT_TOKEN: str = _req("BOT_TOKEN")
        self.ADMIN_IDS: list[int] = [int(x) for x in _get("ADMIN_IDS").split(",") if x]

        # Paths
        self.BASE_DIR: Path = BASE_DIR
        self.DATA_DIR: Path = BASE_DIR / "data"
        self.LOGS_DIR: Path = BASE_DIR / "logs"

        # Master info
        self.MASTER_USERNAME:  str = _get("MASTER_USERNAME",  "@master")
        self.MASTER_ADDRESS:   str = _get("MASTER_ADDRESS",   "Адрес не указан")
        self.MASTER_CONTACT:   str = _get("MASTER_CONTACT",   self.MASTER_USERNAME)
        self.YANDEX_MAPS_URL:  str = _get("YANDEX_MAPS_URL",  "")
        self.MASTER_BIO:       str = _get("MASTER_BIO",       "")
        self.MASTER_PHOTO_ID:  str = _get("MASTER_PHOTO_ID",  "")

        # Business — mutable, intentionally instance-level.
        # _state_lock защищает SERVICES и TIME_SLOTS от гонки между asyncio-потоком
        # и excel_worker (отдельный поток). Читать без лока — ок (GIL), писать — с локом.
        self._state_lock = threading.Lock()
        self.SERVICES: dict[str, int] = {
            "Маникюр":     1500,
            "Гель-лак":    2000,
            "Наращивание": 3000,
        }
        self.TIME_SLOTS: dict[str, str] = {
            "09:00": "12:00",
            "12:00": "15:00",
            "15:00": "18:00",
            "18:00": "21:00",
        }
        self.MAX_SLOTS_PER_DAY:        int = int(_get("MAX_SLOTS_PER_DAY",        "4"))
        self.BOOKING_WINDOW_DAYS:      int = int(_get("BOOKING_WINDOW_DAYS",      "60"))
        self.RETURN_NOTIFY_DAYS:       int = int(_get("RETURN_NOTIFY_DAYS",       "21"))
        self.RESCHEDULE_TIMEOUT_HOURS: int = int(_get("RESCHEDULE_TIMEOUT_HOURS", "24"))
        self.REVIEW_DELAY_HOURS:       int = int(_get("REVIEW_DELAY_HOURS",       "4"))

        # Rate limits
        self.RATE_BOOKING:     tuple[int, int] = _rate(_get("RATE_BOOKING",     "3/300"))
        self.RATE_REVIEW:      tuple[int, int] = _rate(_get("RATE_REVIEW",      "2/3600"))
        self.RATE_RESCHEDULE:  tuple[int, int] = _rate(_get("RATE_RESCHEDULE",  "3/300"))

        self.TIMEZONE: str = _get("TIMEZONE", "Europe/Moscow")
        self.RUB_TO_EUR_RATE: float = float(_get("RUB_TO_EUR_RATE", "100"))

    @property
    def DB_PATH(self) -> str:
        return str(self.DATA_DIR / "salon.db")

    @property
    def EXCEL_PATH(self) -> str:
        return str(self.DATA_DIR / "bookings.xlsx")

    def update_services(self, new: dict[str, int]) -> None:
        """Атомарно заменяет SERVICES. Потокобезопасно."""
        with self._state_lock:
            self.SERVICES = new

    def update_time_slots(self, new: dict[str, str]) -> None:
        """Атомарно заменяет TIME_SLOTS. Потокобезопасно."""
        with self._state_lock:
            self.TIME_SLOTS = new

    def update_env_key(self, key: str, value: str) -> bool:
        """Обновляет одну переменную в .env файле (best-effort).

        Значение записывается в файл чтобы пережить рестарт контейнера.
        Многострочные значения кодируются: \\n → \\\\n.

        Returns True если запись прошла успешно.
        """
        try:
            env_path = self.BASE_DIR / ".env"
            # Кодируем переносы строк для однострочного формата .env
            encoded = value.replace("\\", "\\\\").replace("\n", "\\n")
            line = f"{key}={encoded}"
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()
                replaced = False
                for i, l in enumerate(lines):
                    if l.startswith(f"{key}="):
                        lines[i] = line
                        replaced = True
                        break
                if not replaced:
                    lines.append(line)
            else:
                lines = [line]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def init_dirs(self):
        for d in (self.DATA_DIR, self.LOGS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    def set_runtime_value(self, key: str, value: str):
        attr = self._RUNTIME_KEY_TO_ATTR.get(key)
        if not attr:
            raise KeyError(f"Unsupported runtime setting key: {key}")
        setattr(self, attr, value)
        from app.core.database import atomic
        from app.repositories.repo import RuntimeSettingRepo
        with atomic() as db:
            RuntimeSettingRepo.set(db, key, value)

    def load_runtime_values(self):
        from app.core.database import get_db
        from app.repositories.repo import RuntimeSettingRepo
        with get_db() as db:
            pairs = RuntimeSettingRepo.all(db)
        for key, value in pairs.items():
            attr = self._RUNTIME_KEY_TO_ATTR.get(key)
            if attr:
                setattr(self, attr, value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.init_dirs()
    return s


settings = get_settings()
