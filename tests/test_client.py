from __future__ import annotations

from typing import Any

import pytest
from kubernetes.client.rest import ApiException

from alert_enrichment.errors import ClusterUnavailable, ResourceNotFound
from alert_enrichment.k8s.client import RealClusterClient


class FakeApiClient:
    def __init__(self) -> None:
        self.closed = False

    def sanitize_for_serialization(self, obj: Any) -> Any:
        return obj

    def close(self) -> None:
        self.closed = True


class FakeCore:
    def __init__(self) -> None:
        self.api_client = FakeApiClient()
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.pod: dict[str, Any] = {"metadata": {"name": "api-0"}}
        self.items: list[dict[str, Any]] = []

    def read_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("pod", name, namespace, kwargs))
        if self.error:
            raise self.error
        return self.pod

    def list_namespaced_event(self, namespace: str, **kwargs: Any) -> Any:
        self.calls.append(("events", namespace, kwargs))
        if self.error:
            raise self.error

        class EventList:
            items = self.items

        return EventList()

    def read_namespaced_pod_log(self, pod: str, namespace: str, **kwargs: Any) -> str:
        self.calls.append(("logs", pod, namespace, kwargs))
        if self.error:
            raise self.error
        return "ERROR boom\n"


class FakeApps:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.result: dict[str, Any] = {"metadata": {"name": "checkout"}}

    def _read(self, kind: str, name: str, namespace: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((kind, name, namespace, kwargs))
        if self.error:
            raise self.error
        return self.result

    def read_namespaced_deployment(self, name: str, namespace: str, **kwargs: Any) -> dict[str, Any]:
        return self._read("deployment", name, namespace, **kwargs)

    def read_namespaced_stateful_set(self, name: str, namespace: str, **kwargs: Any) -> dict[str, Any]:
        return self._read("statefulset", name, namespace, **kwargs)

    def read_namespaced_daemon_set(self, name: str, namespace: str, **kwargs: Any) -> dict[str, Any]:
        return self._read("daemonset", name, namespace, **kwargs)

    def read_namespaced_replica_set(self, name: str, namespace: str, **kwargs: Any) -> dict[str, Any]:
        return self._read("replicaset", name, namespace, **kwargs)


def make_client() -> tuple[RealClusterClient, FakeCore, FakeApps]:
    client = RealClusterClient.__new__(RealClusterClient)
    client._timeout = (1.5, 3.0)
    core, apps = FakeCore(), FakeApps()
    client._core = core  # type: ignore[assignment]
    client._apps = apps  # type: ignore[assignment]
    return client, core, apps


def test_get_pod_returns_raw_json_and_passes_timeout() -> None:
    client, core, _ = make_client()

    pod = client.get_pod("payments", "api-0")

    assert pod == {"metadata": {"name": "api-0"}}
    _, name, namespace, kwargs = core.calls[0]
    assert (name, namespace) == ("api-0", "payments")
    assert kwargs["_request_timeout"] == (1.5, 3.0)


def test_404_becomes_resource_not_found() -> None:
    client, core, _ = make_client()
    core.error = ApiException(status=404, reason="Not Found")

    with pytest.raises(ResourceNotFound):
        client.get_pod("payments", "ghost")


def test_api_error_becomes_cluster_unavailable() -> None:
    client, core, _ = make_client()
    core.error = ApiException(status=403, reason="Forbidden")

    with pytest.raises(ClusterUnavailable, match="403"):
        client.get_pod("payments", "api-0")


def test_network_error_becomes_cluster_unavailable() -> None:
    client, core, _ = make_client()
    core.error = OSError("connection reset")

    with pytest.raises(ClusterUnavailable, match="unreachable"):
        client.get_pod("payments", "api-0")


def test_workload_readers_cover_all_kinds() -> None:
    client, _, apps = make_client()

    for kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"):
        assert client.get_workload("payments", kind, "checkout") is not None

    assert [call[0] for call in apps.calls] == [
        "deployment",
        "statefulset",
        "daemonset",
        "replicaset",
    ]
    assert client.get_workload("payments", "Job", "migrate") is None


def test_missing_workload_is_none_not_an_error() -> None:
    client, _, apps = make_client()
    apps.error = ApiException(status=404, reason="Not Found")

    assert client.get_workload("payments", "Deployment", "checkout") is None


def test_events_selector_is_pod_scoped() -> None:
    client, core, _ = make_client()
    core.items = [{"reason": "BackOff"}]

    events = client.list_pod_events("payments", "api-0")

    assert events == [{"reason": "BackOff"}]
    _, namespace, kwargs = core.calls[0]
    assert namespace == "payments"
    assert kwargs["field_selector"] == "involvedObject.name=api-0,involvedObject.kind=Pod"


def test_logs_request_previous_and_tail() -> None:
    client, core, _ = make_client()

    text = client.get_logs("payments", "api-0", "api", previous=True, tail_lines=50)

    assert text.startswith("ERROR")
    _, pod, namespace, kwargs = core.calls[0]
    assert (pod, namespace) == ("api-0", "payments")
    assert kwargs["previous"] is True
    assert kwargs["tail_lines"] == 50
    assert kwargs["container"] == "api"


def test_close_closes_the_underlying_api_client() -> None:
    client, core, _ = make_client()
    client.close()

    assert core.api_client.closed is True
