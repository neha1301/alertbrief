from __future__ import annotations

import time
from typing import Any

from alert_enrichment.k8s import enrich_alert
from alert_enrichment.k8s.enrich import parse_events
from alert_enrichment.k8s.logs import extract_error_lines, logs_unavailable
from alert_enrichment.models import Alert
from alert_enrichment.settings import Settings
from support import NAMESPACE, POD, FakeCluster, crashloop_cluster


def make_alert(**labels: str) -> Alert:
    base = {
        "alertname": "PodCrashLooping",
        "namespace": NAMESPACE,
        "pod": POD,
        "cluster": "prod-blr-1",
        "severity": "warning",
    }
    base.update(labels)
    return Alert(status="firing", labels=base)


async def test_crashloop_pod_is_fully_enriched(cluster: FakeCluster, settings: Settings) -> None:
    enrichment = await enrich_alert(make_alert(), cluster, settings)

    assert enrichment.partial is False
    assert enrichment.pod is not None
    pod = enrichment.pod
    assert pod.phase == "Running"
    assert pod.ready == "0/1"
    assert pod.total_restarts == 12
    assert pod.node is not None and pod.node.startswith("ip-10-0-3-14")
    assert pod.owner is not None and pod.owner.kind == "ReplicaSet"

    container = pod.containers[0]
    assert container.reason == "CrashLoopBackOff"
    assert container.last_exit_code == 1
    assert container.last_terminated_at is not None

    assert enrichment.rollout is not None
    assert enrichment.rollout.kind == "Deployment"
    assert enrichment.rollout.name == "checkout"
    assert enrichment.rollout.ready == 3
    assert enrichment.rollout.healthy is True

    assert enrichment.logs[0].previous is True
    assert any("db pool exhausted" in line for line in enrichment.logs[0].lines)
    assert any("at pool.acquire" in line for line in enrichment.logs[0].lines)

    assert enrichment.events[0].startswith("BackOff:")
    assert all("Pulled" not in event for event in enrichment.events)
    assert enrichment.duration_ms >= 0


async def test_missing_pod_degrades_without_raising(
    cluster: FakeCluster, settings: Settings
) -> None:
    enrichment = await enrich_alert(make_alert(pod="ghost-abc"), cluster, settings)

    assert enrichment.partial is True
    assert enrichment.pod is None
    assert "not found" in enrichment.errors[0]


async def test_alert_without_pod_label_skips_cluster(
    cluster: FakeCluster, settings: Settings
) -> None:
    alert = Alert(labels={"alertname": "KubeDeploymentReplicasMismatch", "namespace": NAMESPACE})
    enrichment = await enrich_alert(alert, cluster, settings)

    assert enrichment.partial is True
    assert enrichment.pod is None
    assert cluster.pod_requests == []


async def test_previous_logs_fall_back_to_current(
    cluster: FakeCluster, settings: Settings
) -> None:
    cluster.logs.pop((POD, "checkout", True))

    enrichment = await enrich_alert(make_alert(), cluster, settings)

    evidence = enrichment.logs[0]
    assert evidence.previous is False
    assert any("address already in use" in line for line in evidence.lines)
    assert ("checkout", True) in cluster.log_requests
    assert ("checkout", False) in cluster.log_requests


async def test_unusable_previous_log_text_falls_back_to_current(
    cluster: FakeCluster, settings: Settings
) -> None:
    # containerd/kind returns 200 with this body instead of a 400.
    cluster.logs[(POD, "checkout", True)] = (
        "unable to retrieve container logs for containerd://6f1d0c"
    )

    enrichment = await enrich_alert(make_alert(), cluster, settings)

    evidence = enrichment.logs[0]
    assert evidence.previous is False
    assert any("address already in use" in line for line in evidence.lines)
    assert "unable to retrieve" not in " ".join(evidence.lines)


async def test_rollout_without_parent_is_skipped(settings: Settings) -> None:
    cluster = crashloop_cluster()
    replica_set = cluster.workloads[(NAMESPACE, "ReplicaSet", "checkout-7d9f8b6c5")]
    replica_set["metadata"].pop("ownerReferences")

    enrichment = await enrich_alert(make_alert(), cluster, settings)

    assert enrichment.rollout is None
    assert enrichment.partial is False


async def test_slow_cluster_is_cut_off_by_budget(settings: Settings) -> None:
    class SlowCluster(FakeCluster):
        def get_pod(self, namespace: str, name: str) -> dict[str, Any]:
            time.sleep(0.3)
            return super().get_pod(namespace, name)

    slow_settings = Settings(_env_file=None, enrichment_budget_seconds=0.05, log_json=False)
    enrichment = await enrich_alert(make_alert(), SlowCluster(), slow_settings)

    assert enrichment.partial is True
    assert enrichment.pod is None
    assert "pod lookup failed" in enrichment.errors[0]


def test_extract_error_lines_strips_ansi_and_collapses_repeats() -> None:
    text = (
        "\x1b[31mERROR\x1b[0m db pool exhausted\n"
        "ERROR db pool exhausted\n"
        "    at pool.acquire (db/pool.py:88)\n"
        "INFO request served\n"
        "FATAL failed to acquire connection\n"
    )

    lines, truncated = extract_error_lines(text, max_lines=10)

    assert truncated is False
    assert lines == [
        "ERROR db pool exhausted  (x2)",
        "at pool.acquire (db/pool.py:88)",
        "FATAL failed to acquire connection",
    ]


def test_extract_error_lines_keeps_newest_lines() -> None:
    text = "\n".join(f"ERROR something failed #{index}" for index in range(30))

    lines, truncated = extract_error_lines(text, max_lines=5)

    assert truncated is True
    assert len(lines) == 5
    assert lines[-1] == "ERROR something failed #29"


def test_events_drop_pod_uid_noise_and_normal_events() -> None:
    items = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "count": 4,
            "lastTimestamp": "2026-11-17T18:04:00Z",
            "message": (
                "Back-off restarting failed container checkout in pod "
                "checkout-abc_payments(cd644e85-bfdc-44b0-bb33-fe94dd5cc70d)"
            ),
        },
        {"type": "Normal", "reason": "Pulled", "message": "image present"},
    ]

    events = parse_events(items, limit=5)

    assert events == [
        "BackOff: Back-off restarting failed container checkout in pod "
        "checkout-abc_payments (x4)"
    ]


def test_logs_unavailable_detects_containerd_message() -> None:
    assert logs_unavailable("unable to retrieve container logs for containerd://abc")
    assert logs_unavailable("previous terminated container \"x\" in pod \"y\" not found")
    assert not logs_unavailable("ERROR db pool exhausted")
