"""app/core/rate_limiter.py"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Coroutine, Any


class RateLimiter:
    """Sliding window, per-user. Asyncio-safe (single thread)."""

    def __init__(self, limit: int, window: int):
        self._limit  = limit
        self._window = window
        self._log: dict[int, list[float]] = defaultdict(list)

    def check(self, user_id: int) -> tuple[bool, int]:
        now = time.monotonic()
        log = [t for t in self._log[user_id] if now - t < self._window]
        self._log[user_id] = log
        if len(log) >= self._limit:
            wait = int(self._window - (now - min(log)))
            return False, max(1, wait)
        self._log[user_id].append(now)
        return True, 0


class Limiters:
    def __init__(self, s):
        self.booking    = RateLimiter(*s.RATE_BOOKING)
        self.review     = RateLimiter(*s.RATE_REVIEW)
        self.reschedule = RateLimiter(*s.RATE_RESCHEDULE)


def setup_logging(logs_dir: Path) -> logging.Logger:
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
    from logging.handlers import RotatingFileHandler

    # Ротация: максимум 5 МБ на файл, хранить 7 архивов (~35 МБ итого)
    file_handler = RotatingFileHandler(
        logs_dir / "bot.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(fmt))

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

    for noisy in ("httpx", "httpcore", "telegram.ext", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("salon")
