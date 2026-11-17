from __future__ import annotations

import asyncio

import pytest

from alert_enrichment.dedup import MemoryDedupStore
from alert_enrichment.errors import AlertEnrichmentError
from alert_enrichment.runtime import build_cluster, build_dedup, build_publisher
from alert_enrichment.settings import Settings
from alert_enrichment.slack import SinkPublisher, SlackPublisher


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_publisher_is_the_local_sink(tmp_path: object) -> None:
    publisher = build_publisher(settings(slack_sink_path=f"{tmp_path}/slack.jsonl"))

    assert isinstance(publisher, SinkPublisher)


def test_webhook_mode_requires_a_webhook_url() -> None:
    with pytest.raises(AlertEnrichmentError, match="SLACK_WEBHOOK_URL"):
        build_publisher(settings(slack_mode="webhook"))


def test_webhook_mode_rejects_a_blank_secret_value() -> None:
    with pytest.raises(AlertEnrichmentError, match="SLACK_WEBHOOK_URL"):
        build_publisher(settings(slack_mode="webhook", slack_webhook_url="   "))


def test_webhook_mode_rejects_a_url_without_a_scheme() -> None:
    with pytest.raises(AlertEnrichmentError, match="http"):
        build_publisher(
            settings(slack_mode="webhook", slack_webhook_url="hooks.slack.com/services/x")
        )


def test_webhook_mode_builds_the_real_publisher() -> None:
    publisher = build_publisher(
        settings(slack_mode="webhook", slack_webhook_url="https://hooks.slack.test/x")
    )

    assert isinstance(publisher, SlackPublisher)
    asyncio.run(publisher.aclose())


def test_auto_mode_picks_webhook_when_url_is_present() -> None:
    resolved = settings(slack_webhook_url="https://hooks.slack.test/x")

    assert resolved.resolved_slack_mode() == "webhook"
    assert resolved.slack_enabled() is True


def test_auto_mode_ignores_a_blank_secret_value() -> None:
    resolved = settings(slack_webhook_url="  ")

    assert resolved.resolved_slack_mode() == "sink"
    assert resolved.slack_webhook_value() == ""


def test_memory_dedup_is_the_default() -> None:
    assert isinstance(build_dedup(settings()), MemoryDedupStore)


def test_redis_backend_requires_a_url() -> None:
    with pytest.raises(AlertEnrichmentError, match="REDIS_URL"):
        build_dedup(settings(dedup_backend="redis"))


def test_cluster_client_receives_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("alert_enrichment.runtime.RealClusterClient", StubClient)

    build_cluster(settings(kubeconfig="/tmp/kubeconfig", k8s_request_timeout_seconds=2.0))

    assert captured == {
        "in_cluster": None,
        "kubeconfig": "/tmp/kubeconfig",
        "request_timeout": 2.0,
    }
