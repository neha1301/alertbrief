"""Object graph assembly, kept outside the request handlers for testability."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .dedup import DedupStore, MemoryDedupStore, RedisDedupStore
from .errors import AlertEnrichmentError
from .k8s import ClusterClient, RealClusterClient
from .settings import Settings
from .slack.publisher import Publisher, SinkPublisher, SlackPublisher

logger = logging.getLogger(__name__)


@dataclass
class Deps:
    settings: Settings
    cluster: ClusterClient
    publisher: Publisher
    dedup: DedupStore

    async def aclose(self) -> None:
        await self.publisher.aclose()
        self.cluster.close()
        self.dedup.close()


def build_cluster(settings: Settings) -> ClusterClient:
    return RealClusterClient(
        in_cluster=settings.kubernetes_in_cluster,
        kubeconfig=settings.kubeconfig,
        request_timeout=settings.k8s_request_timeout_seconds,
    )


def build_publisher(settings: Settings) -> Publisher:
    mode = settings.resolved_slack_mode()
    if mode == "webhook":
        url = settings.slack_webhook_value()
        if not url:
            raise AlertEnrichmentError("SLACK_MODE=webhook but SLACK_WEBHOOK_URL is not set")
        if not url.startswith(("http://", "https://")):
            raise AlertEnrichmentError(
                "SLACK_WEBHOOK_URL must start with http:// or https://"
            )
        return SlackPublisher(
            url,
            timeout=settings.slack_timeout_seconds,
            max_retries=settings.slack_max_retries,
        )
    logger.warning(
        "SLACK_MODE=sink: messages are written to %s instead of Slack",
        settings.slack_sink_path,
    )
    return SinkPublisher(settings.slack_sink_path)


def build_dedup(settings: Settings) -> DedupStore:
    if settings.dedup_backend == "redis":
        if not settings.redis_url:
            raise AlertEnrichmentError("DEDUP_BACKEND=redis but REDIS_URL is not set")
        return RedisDedupStore(
            settings.redis_url,
            stale_after=settings.dedup_stale_reservation_seconds,
        )
    return MemoryDedupStore(stale_after=settings.dedup_stale_reservation_seconds)
