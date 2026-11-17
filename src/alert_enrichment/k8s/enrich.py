"""The actual enrichment: pod -> owner workload -> rollout -> logs -> events.

Every step is best-effort. A slow or partially broken cluster must never turn a
useful alert into a dropped webhook, so failures are collected in
``Enrichment.errors`` and the Slack message is still published.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

from ..errors import ResourceNotFound
from ..models import (
    Alert,
    ContainerSnapshot,
    Enrichment,
    LogEvidence,
    OwnerRef,
    PodSnapshot,
    RolloutInfo,
)
from ..settings import Settings
from ..utils import parse_timestamp, truncate
from .client import ClusterClient
from .logs import extract_error_lines, logs_unavailable

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
# kind/containerd append the pod UID to event messages; it is pure noise in Slack.
_UUID_SUFFIX_RE = re.compile(
    r"\s*\([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)\s*$"
)


async def _call(
    fn: Callable[..., _T], *args: Any, limit: float, **kwargs: Any
) -> _T:
    """Run a blocking Kubernetes client call off the event loop, with a deadline."""

    if limit <= 0:
        raise TimeoutError("enrichment budget exhausted before the call started")
    result: _T = await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=limit)
    return result


def _remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


def parse_pod(raw: dict[str, Any], namespace: str) -> PodSnapshot:
    metadata = raw.get("metadata") or {}
    status = raw.get("status") or {}
    spec = raw.get("spec") or {}
    specs: list[dict[str, Any]] = spec.get("containers") or []
    statuses: dict[str, dict[str, Any]] = {
        item.get("name", ""): item for item in (status.get("containerStatuses") or [])
    }

    containers: list[ContainerSnapshot] = []
    for container_spec in specs:
        name = container_spec.get("name") or "unknown"
        container_status = statuses.get(name, {})
        state, reason, exit_code, finished_at = _state_details(container_status.get("state") or {})
        last_state, last_reason, last_exit, last_finished = _state_details(
            container_status.get("lastState") or {}
        )
        containers.append(
            ContainerSnapshot(
                name=name,
                ready=bool(container_status.get("ready")),
                restarts=int(container_status.get("restartCount") or 0),
                state=state,
                reason=reason,
                exit_code=exit_code,
                last_reason=last_reason,
                last_exit_code=last_exit,
                last_terminated_at=last_finished if last_state == "terminated" else finished_at,
                image=container_spec.get("image"),
            )
        )

    owner_refs: list[dict[str, Any]] = metadata.get("ownerReferences") or []
    owner = None
    if owner_refs:
        owner = OwnerRef(
            kind=owner_refs[0].get("kind") or "Unknown",
            name=owner_refs[0].get("name") or "unknown",
        )

    ready_count = sum(1 for container in containers if container.ready)
    images = [image for container in containers if (image := container.image)]
    return PodSnapshot(
        name=metadata.get("name") or "unknown",
        namespace=metadata.get("namespace") or namespace,
        phase=status.get("phase") or "Unknown",
        node=spec.get("nodeName"),
        ready=f"{ready_count}/{len(containers)}",
        total_restarts=sum(container.restarts for container in containers),
        created_at=parse_timestamp(metadata.get("creationTimestamp")),
        owner=owner,
        containers=containers,
        images=images,
        deleting=bool(metadata.get("deletionTimestamp")),
    )


def _state_details(
    state: dict[str, Any],
) -> tuple[str, str | None, int | None, Any]:
    for key in ("running", "waiting", "terminated"):
        if key in state:
            details = state[key] or {}
            finished = parse_timestamp(details.get("finishedAt"))
            return key, details.get("reason"), details.get("exitCode"), finished
    return "unknown", None, None, None


def parse_rollout(raw: dict[str, Any], kind: str) -> RolloutInfo:
    metadata = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}

    last_update = None
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Progressing":
            last_update = parse_timestamp(condition.get("lastUpdateTime"))
            break

    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled") or 0
        ready = status.get("numberReady") or 0
        updated = status.get("updatedNumberScheduled") or 0
        available = status.get("numberAvailable") or 0
    else:
        desired = spec.get("replicas")
        if desired is None:
            desired = 1 if kind != "ReplicaSet" else 0
        ready = status.get("readyReplicas") or 0
        updated = status.get("updatedReplicas") or 0
        available = status.get("availableReplicas") or 0

    template = (spec.get("template") or {}).get("spec") or {}
    images = [
        image
        for container in (template.get("containers") or [])
        if (image := container.get("image"))
    ]

    return RolloutInfo(
        kind=kind,
        name=metadata.get("name") or "unknown",
        desired=int(desired),
        ready=int(ready),
        updated=int(updated),
        available=int(available),
        generation=metadata.get("generation"),
        observed_generation=status.get("observedGeneration"),
        last_update=last_update,
        images=images,
    )


def parse_events(items: list[dict[str, Any]], limit: int) -> list[str]:
    if limit <= 0:
        return []

    def sort_key(item: dict[str, Any]) -> float:
        stamp = parse_timestamp(
            item.get("lastTimestamp")
            or item.get("eventTime")
            or (item.get("metadata") or {}).get("creationTimestamp")
        )
        return stamp.timestamp() if stamp else 0.0

    out: list[str] = []
    for item in sorted(items, key=sort_key, reverse=True):
        if (item.get("type") or "").lower() != "warning":
            continue
        reason = item.get("reason") or "Warning"
        message = _UUID_SUFFIX_RE.sub("", " ".join((item.get("message") or "").split()))
        count = int(item.get("count") or 1)
        suffix = f" (x{count})" if count > 1 else ""
        out.append(f"{reason}: {truncate(message, 180)}{suffix}")
        if len(out) >= limit:
            break
    return out


async def _resolve_rollout(
    client: ClusterClient,
    namespace: str,
    pod: PodSnapshot,
    deadline: float,
) -> RolloutInfo | None:
    owner = pod.owner
    if owner is None:
        return None

    kind, name = owner.kind, owner.name
    if kind == "ReplicaSet":
        replica_set = await _call(
            client.get_workload, namespace, "ReplicaSet", name, limit=_remaining(deadline)
        )
        if not replica_set:
            return None
        parents: list[dict[str, Any]] = (
            (replica_set.get("metadata") or {}).get("ownerReferences") or []
        )
        if not parents:
            return None
        kind = parents[0].get("kind") or ""
        name = parents[0].get("name") or ""

    if kind not in _WORKLOAD_KINDS:
        return None

    raw = await _call(client.get_workload, namespace, kind, name, limit=_remaining(deadline))
    if not raw:
        return None
    return parse_rollout(raw, kind)


def _log_targets(pod: PodSnapshot, alert: Alert, settings: Settings) -> list[ContainerSnapshot]:
    containers = pod.containers
    if alert.container:
        matching = [item for item in containers if item.name == alert.container]
        if matching:
            containers = matching
    ordered = sorted(
        containers,
        key=lambda item: (item.state == "running" and item.restarts == 0, -item.restarts),
    )
    return ordered[: settings.max_log_containers]


async def _fetch_logs(
    client: ClusterClient,
    alert: Alert,
    pod: PodSnapshot,
    settings: Settings,
    deadline: float,
) -> list[LogEvidence]:
    async def fetch(container: ContainerSnapshot) -> LogEvidence:
        # Restarted containers are the interesting ones: the *previous*
        # instance holds the crash, the current one just says "starting".
        use_previous = container.restarts > 0

        async def read(previous: bool) -> str:
            return await _call(
                client.get_logs,
                pod.namespace,
                pod.name,
                container.name,
                previous=previous,
                tail_lines=settings.log_tail_lines,
                limit=_remaining(deadline),
            )

        try:
            text = await read(use_previous)
            if use_previous and logs_unavailable(text):
                # containerd/kind can drop the previous log file; the current
                # container usually still holds the failing run this soon after.
                logger.info(
                    "previous logs unavailable for %s/%s (%s); using current",
                    pod.namespace,
                    pod.name,
                    container.name,
                )
                use_previous = False
                text = await read(False)
        except Exception as exc:
            if not use_previous:
                return LogEvidence(container=container.name, error=truncate(str(exc), 200))
            try:
                use_previous = False
                text = await read(False)
            except Exception as fallback_exc:
                return LogEvidence(
                    container=container.name, error=truncate(str(fallback_exc), 200)
                )

        lines, truncated = extract_error_lines(text, max_lines=settings.max_error_lines)
        return LogEvidence(
            container=container.name,
            previous=use_previous,
            lines=lines,
            truncated=truncated,
        )

    targets = _log_targets(pod, alert, settings)
    return list(await asyncio.gather(*(fetch(container) for container in targets)))


async def _fetch_events(
    client: ClusterClient,
    pod: PodSnapshot,
    settings: Settings,
    deadline: float,
) -> list[str]:
    items = await _call(
        client.list_pod_events,
        pod.namespace,
        pod.name,
        limit=_remaining(deadline),
    )
    return parse_events(items, settings.max_events)


async def enrich_alert(alert: Alert, client: ClusterClient, settings: Settings) -> Enrichment:
    started = time.monotonic()
    deadline = started + settings.enrichment_budget_seconds
    enrichment = Enrichment()

    pod_name = alert.pod_name
    if not pod_name:
        enrichment.errors.append("alert has no pod label; sending without cluster context")
        enrichment.partial = True
        enrichment.duration_ms = int((time.monotonic() - started) * 1000)
        return enrichment

    raw_pod: dict[str, Any] | None = None
    try:
        raw_pod = await _call(
            client.get_pod, alert.namespace, pod_name, limit=_remaining(deadline)
        )
    except ResourceNotFound:
        enrichment.errors.append(
            f"pod {alert.namespace}/{pod_name} not found (deleted or renamed?)"
        )
    except Exception as exc:
        enrichment.errors.append(f"pod lookup failed: {truncate(str(exc), 200)}")
        logger.warning("pod lookup failed for %s/%s: %s", alert.namespace, pod_name, exc)

    if raw_pod is not None:
        pod = parse_pod(raw_pod, alert.namespace)
        enrichment.pod = pod
        rollout, logs, events = await asyncio.gather(
            _resolve_rollout(client, pod.namespace, pod, deadline),
            _fetch_logs(client, alert, pod, settings, deadline),
            _fetch_events(client, pod, settings, deadline),
            return_exceptions=True,
        )
        if isinstance(rollout, BaseException):
            enrichment.errors.append(f"rollout lookup failed: {truncate(str(rollout), 200)}")
            logger.warning("rollout lookup failed: %s", rollout)
        else:
            enrichment.rollout = rollout
        if isinstance(logs, BaseException):
            enrichment.errors.append(f"log fetch failed: {truncate(str(logs), 200)}")
            logger.warning("log fetch failed: %s", logs)
        else:
            enrichment.logs = logs
        if isinstance(events, BaseException):
            enrichment.errors.append(f"event fetch failed: {truncate(str(events), 200)}")
            logger.warning("event fetch failed: %s", events)
        else:
            enrichment.events = events

    enrichment.partial = bool(enrichment.errors)
    enrichment.duration_ms = int((time.monotonic() - started) * 1000)
    return enrichment
