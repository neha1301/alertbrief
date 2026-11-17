"""Test doubles and fixtures shared by the suite."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from alert_enrichment.errors import ResourceNotFound
from alert_enrichment.slack import RenderedMessage

TESTDATA = Path(__file__).parent / "testdata"
NAMESPACE = "payments"
POD = "checkout-7d9f8b6c5-x2vqn"
POD_LABELS = {
    "alertname": "PodCrashLooping",
    "namespace": NAMESPACE,
    "pod": POD,
    "cluster": "prod-blr-1",
    "severity": "warning",
}


def load_json(name: str) -> Any:
    return json.loads((TESTDATA / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (TESTDATA / name).read_text(encoding="utf-8")


class FakeCluster:
    """Implements ``ClusterClient`` with dictionaries carved from real kubectl output."""

    def __init__(self) -> None:
        self.pods: dict[tuple[str, str], dict[str, Any]] = {}
        self.workloads: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.logs: dict[tuple[str, str, bool], str] = {}
        self.pod_requests: list[tuple[str, str]] = []
        self.log_requests: list[tuple[str, bool]] = []
        self.closed = False

    def get_pod(self, namespace: str, name: str) -> dict[str, Any]:
        self.pod_requests.append((namespace, name))
        try:
            return copy.deepcopy(self.pods[(namespace, name)])
        except KeyError as exc:
            raise ResourceNotFound(f"pod {namespace}/{name} not found") from exc

    def get_workload(self, namespace: str, kind: str, name: str) -> dict[str, Any] | None:
        workload = self.workloads.get((namespace, kind, name))
        return copy.deepcopy(workload) if workload is not None else None

    def list_pod_events(self, namespace: str, name: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.events.get((namespace, name), []))

    def get_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        self.log_requests.append((container, previous))
        try:
            return self.logs[(pod, container, previous)]
        except KeyError as exc:
            kind = "previous" if previous else "current"
            raise ResourceNotFound(f"no {kind} logs for {pod}/{container}") from exc

    def close(self) -> None:
        self.closed = True


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[RenderedMessage] = []
        self.closed = False

    async def publish(self, message: RenderedMessage) -> None:
        self.messages.append(message)

    async def aclose(self) -> None:
        self.closed = True


def crashloop_cluster() -> FakeCluster:
    cluster = FakeCluster()
    cluster.pods[(NAMESPACE, POD)] = load_json("pod_crashloop.json")
    cluster.workloads[(NAMESPACE, "ReplicaSet", "checkout-7d9f8b6c5")] = load_json("replicaset.json")
    cluster.workloads[(NAMESPACE, "Deployment", "checkout")] = load_json("deployment.json")
    cluster.events[(NAMESPACE, POD)] = load_json("events.json")
    cluster.logs[(POD, "checkout", True)] = load_text("logs_previous.txt")
    cluster.logs[(POD, "checkout", False)] = load_text("logs_current.txt")
    return cluster
