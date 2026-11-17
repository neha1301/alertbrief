from __future__ import annotations

import json
import logging
import sys

from alert_enrichment.logging_setup import JsonFormatter, configure_logging


def test_json_formatter_includes_extra_fields_and_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "alert_enrichment.test",
            logging.ERROR,
            __file__,
            1,
            "failed %s",
            ("webhook",),
            sys.exc_info(),
        )
    record.incident = "abc123"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["msg"] == "failed webhook"
    assert payload["level"] == "ERROR"
    assert payload["logger"] == "alert_enrichment.test"
    assert payload["incident"] == "abc123"
    assert "ValueError: boom" in payload["exc"]
    assert payload["ts"].endswith("Z") or "+00:00" in payload["ts"]


def test_configure_logging_is_idempotent_and_switches_level() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        configure_logging("DEBUG", json_logs=True)
        configure_logging("WARNING", json_logs=False)

        ours = [h for h in root.handlers if getattr(h, "_alert_enrichment", False)]
        assert len(ours) == 1
        assert root.level == logging.WARNING
        assert not isinstance(ours[0].formatter, JsonFormatter)
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_alert_enrichment", False):
                root.removeHandler(handler)
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
