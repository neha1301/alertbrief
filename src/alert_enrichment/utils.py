"""Small, dependency-free helpers shared by the enrichment and rendering layers."""

from __future__ import annotations

import re
from datetime import UTC, datetime

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    """Kubernetes/Alertmanager timestamps are UTC; be defensive about naive values."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse RFC3339 timestamps from Kubernetes/Alertmanager.

    Alertmanager uses the zero time (``0001-01-01T00:00:00Z``) to mean "unset",
    so anything before the Unix epoch is treated as missing.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = ensure_utc(parsed)
    if parsed.year < 1970:
        return None
    return parsed


def humanize_delta(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def humanize_age(value: datetime | None, *, now: datetime | None = None) -> str | None:
    if value is None:
        return None
    reference = now or utcnow()
    return humanize_delta((reference - value).total_seconds())


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    keep = max(limit - len(suffix), 0)
    return text[:keep].rstrip() + suffix
