"""Thin wrapper around the official Kubernetes client.

The wrapper intentionally returns *raw, camelCase JSON dictionaries* (the same
shape `kubectl get -o json` produces) so that:

* the parsing layer has a single, testable input format;
* tests can use fixtures captured from a real cluster;
* nothing in the enrichment logic depends on the Python client's object model.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException  # type: ignore[attr-defined]

from ..errors import ClusterUnavailable, ResourceNotFound

logger = logging.getLogger(__name__)

WORKLOAD_READERS = ("deployment", "statefulset", "daemonset", "replicaset")


class ClusterClient(Protocol):
    """Everything the enricher needs from a cluster. Faked in tests."""

    def get_pod(self, namespace: str, name: str) -> dict[str, Any]: ...

    def get_workload(self, namespace: str, kind: str, name: str) -> dict[str, Any] | None: ...

    def list_pod_events(self, namespace: str, name: str) -> list[dict[str, Any]]: ...

    def get_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str: ...

    def close(self) -> None: ...


class RealClusterClient:
    def __init__(
        self,
        *,
        in_cluster: bool | None = None,
        kubeconfig: str | None = None,
        request_timeout: float = 1.5,
    ) -> None:
        # (connect, read) timeout: never let a hung API server eat the webhook.
        self._timeout: tuple[float, float] = (min(request_timeout, 3.0), request_timeout * 2)
        if in_cluster is True:
            k8s_config.load_incluster_config()  # type: ignore[no-untyped-call]
        elif kubeconfig is not None:
            k8s_config.load_kube_config(config_file=kubeconfig)  # type: ignore[no-untyped-call]
        else:
            try:
                k8s_config.load_incluster_config()  # type: ignore[no-untyped-call]
            except Exception:
                k8s_config.load_kube_config()  # type: ignore[no-untyped-call]
        self._core = k8s.CoreV1Api()
        self._apps = k8s.AppsV1Api()

    # -- internals ---------------------------------------------------------
    def _serialize(self, obj: Any) -> dict[str, Any]:
        raw: dict[str, Any] = self._core.api_client.sanitize_for_serialization(obj)
        return raw

    def _read(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, _request_timeout=self._timeout, **kwargs)
        except ApiException as exc:
            if exc.status == 404:
                raise ResourceNotFound(f"404 from Kubernetes API: {exc.reason}") from exc
            raise ClusterUnavailable(f"Kubernetes API error {exc.status}: {exc.reason}") from exc
        except Exception as exc:
            raise ClusterUnavailable(f"Kubernetes API unreachable: {exc}") from exc

    # -- protocol ----------------------------------------------------------
    def get_pod(self, namespace: str, name: str) -> dict[str, Any]:
        pod = self._read(self._core.read_namespaced_pod, name, namespace)
        return self._serialize(pod)

    def get_workload(self, namespace: str, kind: str, name: str) -> dict[str, Any] | None:
        readers = {
            "deployment": self._apps.read_namespaced_deployment,
            "statefulset": self._apps.read_namespaced_stateful_set,
            "daemonset": self._apps.read_namespaced_daemon_set,
            "replicaset": self._apps.read_namespaced_replica_set,
        }
        reader = readers.get(kind.lower())
        if reader is None:
            return None
        try:
            obj = self._read(reader, name, namespace)
        except ResourceNotFound:
            return None
        return self._serialize(obj)

    def list_pod_events(self, namespace: str, name: str) -> list[dict[str, Any]]:
        events = self._read(
            self._core.list_namespaced_event,
            namespace,
            field_selector=f"involvedObject.name={name},involvedObject.kind=Pod",
        )
        return [self._serialize(item) for item in events.items]

    def get_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        text: str = self._read(
            self._core.read_namespaced_pod_log,
            pod,
            namespace,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
        )
        return text

    def close(self) -> None:
        self._core.api_client.close()
