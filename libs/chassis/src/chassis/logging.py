"""Structured JSON logging: one object per line, the correlation id on every line, secrets masked."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import IO, Any

from chassis.correlation import current_correlation_id

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}
_SECRET_KEYS = ("password", "secret", "token", "authorization", "api_key", "dsn")


def _mask(key: str, value: Any) -> Any:
    lowered = key.lower()
    return "***" if any(part in lowered for part in _SECRET_KEYS) else value


class JsonFormatter(logging.Formatter):
    """Formats records as single-line JSON with ``ts``, ``level``, ``service``, ``logger``, ``msg``.

    Extra fields passed with ``logger.info("msg", extra={...})`` become top-level keys; keys that look
    like secrets are masked.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        """Render one record."""
        seconds = int(record.created)
        stamp = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{int((record.created - seconds) * 1000):03d}Z"
        )
        payload: dict[str, Any] = {
            "ts": stamp,
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        correlation_id = current_correlation_id()
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = _mask(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(service: str, level: str = "INFO", stream: IO[str] | None = None) -> None:
    """Send every log record of the process, including uvicorn's, to ``stream`` (stderr) as JSON."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").disabled = True
