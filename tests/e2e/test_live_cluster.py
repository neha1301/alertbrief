"""End-to-end: real kind cluster -> real FastAPI app -> sink file.

Skipped unless ``RUN_E2E=1``. The kind workflow (``.github/workflows/e2e.yml``)
deploys ``demo/broken-app.yaml`` first and waits for CrashLoopBackOff.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alert_enrichment.k8s import RealClusterClient
from alert_enrichment.main import create_app
from alert_enrichment.settings import Settings
from alert_enrichment.slack import SinkPublisher

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("RUN_E2E") != "1", reason="needs a live kind cluster"),
]

NAMESPACE = "payments"


def kubectl_json(*args: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(subprocess.check_output(["kubectl", *args], text=True))
    return result


def crashing_pod() -> dict[str, Any]:
    pods = kubectl_json("get", "pods", "-n", NAMESPACE, "-l", "app=checkout", "-o", "json")["items"]
    assert pods, "demo/broken-app.yaml is not deployed"

    def restart_count(pod: dict[str, Any]) -> int:
        return sum(
            int(status.get("restartCount", 0))
            for status in pod.get("status", {}).get("containerStatuses", [])
        )

    pod = max(pods, key=restart_count)
    assert restart_count(pod) >= 1, "pod has not restarted yet"
    return pod


def test_webhook_to_sink_against_a_real_crashlooping_pod(tmp_path: Path) -> None:
    pod = crashing_pod()
    name = pod["metadata"]["name"]
    starts = (datetime.now(tz=UTC) - timedelta(minutes=6)).replace(microsecond=0)
    sink = tmp_path / "slack.jsonl"

    settings = Settings(
        _env_file=None,
        slack_mode="sink",
        slack_sink_path=str(sink),
        kubernetes_in_cluster=False,
        log_json=False,
    )
    app = create_app(
        settings,
        cluster=RealClusterClient(in_cluster=False),
        publisher=SinkPublisher(sink),
    )
    payload = {
        "version": "4",
        "status": "firing",
        "externalURL": "https://alertmanager.example.com",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PodCrashLooping",
                    "namespace": NAMESPACE,
                    "pod": name,
                    "cluster": "kind-alert-enrichment-demo",
                    "severity": "warning",
                },
                "annotations": {
                    "summary": f"Pod {name} is restarting",
                    "runbook_url": "https://runbooks.example.com/pod-crashloop",
                },
                "startsAt": starts.isoformat().replace("+00:00", "Z"),
                "endsAt": "0001-01-01T00:00:00Z",
            }
        ],
    }

    with TestClient(app) as client:
        response = client.post("/webhook/alertmanager", json=payload)
        assert client.get("/readyz").json()["dedup_backend"] == "memory"

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["sent"] == 1
    assert body["results"][0]["partial"] is False, body

    message = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
    rendered = json.dumps(message, ensure_ascii=False)

    assert "CrashLoopBackOff" in rendered or "Error" in rendered
    assert "db pool exhausted" in rendered
    assert "kubectl -n payments logs " + name in rendered
    assert "silences/new" in rendered
    assert len(message["blocks"]) <= 50
