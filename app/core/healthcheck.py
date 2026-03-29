"""app/core/healthcheck.py — лёгкий HTTP health-check эндпоинт.

Запускается в отдельном потоке вместе с ботом.
GET http://localhost:8080/health → 200 OK  {"status": "ok", "uptime_s": 123}
GET http://localhost:8080/health → 503     {"status": "error", "detail": "..."}

Используется systemd, Docker HEALTHCHECK, Uptime Robot и любым мониторингом.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

logger = logging.getLogger("salon.health")

_start_time: float = time.monotonic()
_server: Optional[HTTPServer] = None
_thread: Optional[threading.Thread] = None

# Флаг готовности — выставляется после успешного запуска polling
_ready: bool = False


def set_ready(value: bool = True) -> None:
    global _ready
    _ready = value


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self._respond(404, {"status": "not_found"})
            return

        uptime = int(time.monotonic() - _start_time)
        if _ready:
            self._respond(200, {"status": "ok", "uptime_s": uptime})
        else:
            self._respond(503, {"status": "starting", "uptime_s": uptime})

    def _respond(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # Подавляем стандартный лог — слишком шумно при частых проверках
        pass


def start(port: int = 8080) -> threading.Thread:
    """Запускает HTTP-сервер в демон-потоке. Возвращает поток."""
    global _server, _thread

    def _run():
        global _server
        try:
            _server = HTTPServer(("0.0.0.0", port), _Handler)
            logger.info("Health-check listening on :%d/health", port)
            _server.serve_forever()
        except Exception:
            logger.exception("Health-check server failed")

    _thread = threading.Thread(target=_run, daemon=True, name="healthcheck")
    _thread.start()
    return _thread


def stop() -> None:
    global _server
    if _server:
        _server.shutdown()
        _server = None
