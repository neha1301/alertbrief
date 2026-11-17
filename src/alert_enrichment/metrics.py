"""Prometheus metrics. Cheap to add, and they make the service debuggable."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

WEBHOOKS = Counter(
    "alert_enrichment_webhooks_total",
    "Alertmanager webhook requests",
    ["status"],  # accepted | empty | rejected
)

ALERTS = Counter(
    "alert_enrichment_alerts_total",
    "Alerts processed, by dedup decision",
    ["decision"],  # sent | suppressed | error | ignored
)

ENRICHMENT_SECONDS = Histogram(
    "alert_enrichment_duration_seconds",
    "Time spent gathering Kubernetes context",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 8.0),
)

SLACK_PUBLISH = Counter(
    "alert_enrichment_slack_publish_total",
    "Slack publish attempts",
    ["result"],  # ok | error
)
