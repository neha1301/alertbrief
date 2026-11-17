from __future__ import annotations

from datetime import UTC, datetime

from alert_enrichment.models import Alert, RolloutInfo
from alert_enrichment.utils import (
    humanize_age,
    humanize_delta,
    parse_timestamp,
    strip_ansi,
    truncate,
)


def test_alert_helpers_normalise_alertmanager_labels() -> None:
    alert = Alert(
        labels={
            "alertname": "PodCrashLooping",
            "cluster_name": "blr-prod",
            "pod_name": "api-0",
            "container_name": "api",
            "severity": "P1",
        }
    )

    assert alert.cluster == "blr-prod"
    assert alert.namespace == "default"
    assert alert.pod_name == "api-0"
    assert alert.container == "api"
    assert alert.severity == "critical"
    assert alert.summary == ""
    assert alert.firing_since is None
    assert alert.resolved_at is None
    assert alert.is_resolved is False


def test_instance_label_is_stripped_to_a_pod_name() -> None:
    assert Alert(labels={"instance": "10.2.3.4:9100"}).pod_name == "10.2.3.4"
    assert Alert(labels={"instance": "checkout-abc.payments.svc"}).pod_name == "checkout-abc"


def test_zero_timestamps_are_treated_as_missing() -> None:
    alert = Alert(
        startsAt="0001-01-01T00:00:00Z",
        endsAt="0001-01-01T00:00:00Z",
    )

    assert alert.firing_since is None
    assert alert.resolved_at is None


def test_dedup_key_is_per_workload_and_severity() -> None:
    base = {"namespace": "payments", "pod": "checkout-abc", "cluster": "prod"}

    crashloop = Alert(labels={**base, "alertname": "PodCrashLooping", "severity": "warning"})
    not_ready = Alert(labels={**base, "alertname": "PodNotReady", "severity": "warning"})
    critical = Alert(labels={**base, "alertname": "PodCrashLooping", "severity": "critical"})
    other_pod = Alert(
        labels={"namespace": "payments", "pod": "checkout-def", "cluster": "prod", "severity": "warning"}
    )

    assert crashloop.dedup_key() == not_ready.dedup_key()
    assert crashloop.dedup_key() != critical.dedup_key()
    assert crashloop.dedup_key() != other_pod.dedup_key()


def test_rollout_health_and_rollout_in_progress() -> None:
    healthy = RolloutInfo(
        kind="Deployment",
        name="checkout",
        desired=3,
        ready=3,
        updated=3,
        generation=14,
        observed_generation=14,
    )
    rolling = RolloutInfo(
        kind="Deployment",
        name="checkout",
        desired=3,
        ready=1,
        updated=2,
        generation=15,
        observed_generation=14,
    )
    scaled_to_zero = RolloutInfo(kind="Deployment", name="checkout", desired=0, generation=2, observed_generation=1)

    assert healthy.healthy is True
    assert healthy.in_progress is False
    assert rolling.in_progress is True
    assert rolling.healthy is False
    assert scaled_to_zero.in_progress is True


def test_time_helpers() -> None:
    now = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)
    assert humanize_delta(-5) == "0s"
    assert humanize_delta(30) == "30s"
    assert humanize_delta(600) == "10m"
    assert humanize_delta(7200) == "2h"
    assert humanize_delta(3 * 86400) == "3d"
    assert humanize_age(None, now=now) is None
    assert humanize_age(datetime(2026, 11, 17, 17, 45, tzinfo=UTC), now=now) == "15m"


def test_timestamp_parsing_is_defensive() -> None:
    assert parse_timestamp(None) is None
    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp("0001-01-01T00:00:00Z") is None
    assert parse_timestamp("2026-11-17T18:00:00Z") == datetime(
        2026, 11, 17, 18, 0, tzinfo=UTC
    )
    assert parse_timestamp("2026-11-17T18:00:00") == datetime(
        2026, 11, 17, 18, 0, tzinfo=UTC
    )


def test_text_helpers() -> None:
    assert strip_ansi("\x1b[31mERROR\x1b[0m boom") == "ERROR boom"
    assert truncate("short", 20) == "short"
    assert truncate("x" * 20, 10) == "x" * 9 + "…"
