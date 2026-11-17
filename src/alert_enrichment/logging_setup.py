"""Minimal structured logging: JSON by default, human-readable when asked."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.makeLogRecord({}).__dict__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    formatter: logging.Formatter = (
        JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    for handler in root.handlers:
        if getattr(handler, "_alert_enrichment", False):
            handler.setFormatter(formatter)
            root.setLevel(level.upper())
            return
    handler = logging.StreamHandler(sys.stdout)
    handler._alert_enrichment = True  # type: ignore[attr-defined]
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
