from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from alert_enrichment.dedup import MemoryDedupStore
from alert_enrichment.errors import PublishError
from alert_enrichment.main import create_app
from alert_enrichment.settings import Settings
from alert_enrichment.slack import RenderedMessage
from support import POD, FakeCluster, RecordingPublisher


def dump(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def test_burst_becomes_one_message(client: TestClient, publisher: RecordingPublisher, payload: dict[str, Any]) -> None:
    response = client.post("/webhook/alertmanager", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["received"] == 4
    assert body["sent"] == 1
    assert body["suppressed"] == 0
    assert len(publisher.messages) == 1
    assert "×4 alerts merged" in dump(publisher.messages[0].blocks)
    assert "PodCrashLooping" in publisher.messages[0].text


def test_repeat_webhook_is_suppressed(client: TestClient, publisher: RecordingPublisher, payload: dict[str, Any]) -> None:
    assert client.post("/webhook/alertmanager", json=payload).json()["sent"] == 1

    second = client.post("/webhook/alertmanager", json=payload)

    assert second.status_code == 202
    body = second.json()
    assert body["sent"] == 0
    assert body["suppressed"] == 1
    assert len(publisher.messages) == 1


def test_dry_run_renders_without_publishing_or_reserving(
    client: TestClient, publisher: RecordingPublisher, payload: dict[str, Any]
) -> None:
    response = client.post("/webhook/alertmanager?dry_run=true", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["dry_run"] is True
    assert len(body["messages"]) == 4  # dry run does not dedupe
    assert body["messages"][0]["blocks"][0]["type"] == "header"
    assert publisher.messages == []

    # Nothing was reserved, so the real webhook still sends.
    assert client.post("/webhook/alertmanager", json=payload).json()["sent"] == 1


def test_unknown_pod_still_publishes_partial_context(
    settings: Settings, publisher: RecordingPublisher, payload: dict[str, Any]
) -> None:
    empty = FakeCluster()
    app = create_app(settings, cluster=empty, publisher=publisher, dedup=MemoryDedupStore())

    with TestClient(app) as test_client:
        response = test_client.post("/webhook/alertmanager", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["sent"] == 1
    assert body["results"][0]["partial"] is True
    assert "Partial context" in dump(publisher.messages[0].blocks)
    assert "not found" in dump(publisher.messages[0].blocks)


class FlakyPublisher(RecordingPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.failing = True

    async def publish(self, message: RenderedMessage) -> None:
        if self.failing:
            raise PublishError("slack is down")
        await super().publish(message)


def test_publish_failure_returns_502_and_retry_is_allowed(
    settings: Settings, cluster: FakeCluster, payload: dict[str, Any]
) -> None:
    publisher = FlakyPublisher()
    app = create_app(settings, cluster=cluster, publisher=publisher, dedup=MemoryDedupStore())

    with TestClient(app) as test_client:
        failed = test_client.post("/webhook/alertmanager", json=payload)
        assert failed.status_code == 502
        assert failed.json()["detail"]["errors"]

        publisher.failing = False
        retried = test_client.post("/webhook/alertmanager", json=payload)

    assert retried.status_code == 202
    assert retried.json()["sent"] == 1
    assert len(publisher.messages) == 1


def test_resolved_alert_is_published_separately(
    client: TestClient, publisher: RecordingPublisher, payload: dict[str, Any]
) -> None:
    client.post("/webhook/alertmanager", json=payload)

    resolved = {
        "version": "4",
        "status": "resolved",
        "externalURL": "",
        "alerts": [
            {
                "status": "resolved",
                "labels": payload["alerts"][0]["labels"],
                "annotations": {},
                "startsAt": "2026-11-17T17:58:50Z",
                "endsAt": "2026-11-17T18:09:00Z",
            }
        ],
    }
    response = client.post("/webhook/alertmanager", json=resolved)

    assert response.status_code == 202
    assert response.json()["sent"] == 1
    assert publisher.messages[-1].text.startswith("✅")


def test_empty_payload_is_accepted(client: TestClient, publisher: RecordingPublisher) -> None:
    response = client.post("/webhook/alertmanager", json={"version": "4", "status": "firing", "alerts": []})

    assert response.status_code == 202
    assert response.json()["received"] == 0
    assert publisher.messages == []


def test_health_readiness_and_metrics(client: TestClient) -> None:
    assert client.get("/").json()["service"] == "alert-enrichment"
    assert client.get("/healthz").json() == {"status": "ok"}

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["slack_mode"] == "sink"
    assert ready.json()["dedup_backend"] == "memory"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "alert_enrichment_webhooks_total" in metrics.text


def test_readiness_fails_before_dependencies_exist(client: TestClient) -> None:
    client.app.state.deps = None

    assert client.get("/readyz").status_code == 503


def test_pod_label_is_required_for_enrichment(
    client: TestClient, publisher: RecordingPublisher
) -> None:
    payload = {
        "version": "4",
        "status": "firing",
        "externalURL": "https://alertmanager.example.com",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubeDeploymentReplicasMismatch",
                    "namespace": "payments",
                    "severity": "critical",
                },
                "annotations": {"summary": "Deployment checkout has 2/3 replicas"},
                "startsAt": "2026-11-17T18:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
            }
        ],
    }

    response = client.post("/webhook/alertmanager", json=payload)

    assert response.status_code == 202
    assert response.json()["sent"] == 1
    body = dump(publisher.messages[0].blocks)
    assert "no pod label" in body
    assert "checkout has 2/3 replicas" in body
    assert POD not in body
