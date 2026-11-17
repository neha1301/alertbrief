from __future__ import annotations

import hashlib
import ipaddress
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .utils import ensure_utc

Severity = Literal["critical", "warning", "info", "unknown"]

_SEVERITY_ALIASES: dict[str, Severity] = {
    "critical": "critical",
    "crit": "critical",
    "page": "critical",
    "p1": "critical",
    "warning": "warning",
    "warn": "warning",
    "p2": "warning",
    "info": "info",
    "informational": "info",
}

_POD_LABEL_KEYS = ("pod", "pod_name", "podname", "instance")


class Alert(BaseModel):
    """One entry of the Alertmanager v4 webhook ``alerts`` array."""

    model_config = ConfigDict(extra="allow")

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname") or "unknown"

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace") or "default"

    @property
    def pod_name(self) -> str | None:
        for key in _POD_LABEL_KEYS:
            value = self.labels.get(key)
            if not value:
                continue
            value = value.split(":")[0].strip()
            if key == "instance":
                # instance is a fallback: exporters put "node-1", an IP, or a
                # fully qualified name there. Strip the domain, never the IP.
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    value = value.split(".")[0]
            if value:
                return value
        return None

    @property
    def container(self) -> str | None:
        return self.labels.get("container") or self.labels.get("container_name")

    @property
    def cluster(self) -> str:
        return self.labels.get("cluster") or self.labels.get("cluster_name") or "unknown"

    @property
    def severity(self) -> Severity:
        raw = (self.labels.get("severity") or "").strip().lower()
        return _SEVERITY_ALIASES.get(raw, "unknown")

    @property
    def summary(self) -> str:
        return self.annotations.get("summary") or self.annotations.get("description") or ""

    @property
    def firing_since(self) -> datetime | None:
        if self.startsAt is None or self.startsAt.year < 1970:
            return None
        return ensure_utc(self.startsAt)

    @property
    def resolved_at(self) -> datetime | None:
        if self.endsAt is None:
            return None
        if self.endsAt.year < 1970:
            return None
        return ensure_utc(self.endsAt)

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved"

    def dedup_key(self) -> str:
        """Identity of the *thing* that is broken, not of the alert name.

        Two different alerts (``PodCrashLooping`` and ``PodNotReady``) about the
        same pod/container/severity become one Slack message. Alerts about
        different pods stay separate; cross-pod grouping is Alertmanager's job
        (``group_by``) before it calls this webhook.
        """

        target = self.pod_name or self.labels.get("deployment") or self.alertname
        severity_bucket = "critical" if self.severity == "critical" else "other"
        raw = "|".join(
            (
                self.cluster,
                self.namespace,
                target,
                self.container or "*",
                severity_bucket,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    groupKey: str = ""
    truncatedAlerts: int = 0
    externalURL: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    alerts: list[Alert] = Field(default_factory=list)


class OwnerRef(BaseModel):
    kind: str
    name: str


class ContainerSnapshot(BaseModel):
    name: str
    ready: bool = False
    restarts: int = 0
    state: str = "unknown"  # running | waiting | terminated | unknown
    reason: str | None = None
    exit_code: int | None = None
    last_reason: str | None = None
    last_exit_code: int | None = None
    last_terminated_at: datetime | None = None
    image: str | None = None


class PodSnapshot(BaseModel):
    name: str
    namespace: str
    phase: str = "Unknown"
    node: str | None = None
    ready: str = "0/0"
    total_restarts: int = 0
    created_at: datetime | None = None
    owner: OwnerRef | None = None
    containers: list[ContainerSnapshot] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    deleting: bool = False


class RolloutInfo(BaseModel):
    kind: str
    name: str
    desired: int = 0
    ready: int = 0
    updated: int = 0
    available: int = 0
    generation: int | None = None
    observed_generation: int | None = None
    last_update: datetime | None = None
    images: list[str] = Field(default_factory=list)

    @property
    def in_progress(self) -> bool:
        if self.desired == 0:
            return self.observed_generation is not None and (
                self.generation or 0
            ) > (self.observed_generation or 0)
        return (
            self.ready < self.desired
            or self.updated < self.desired
            or (self.generation or 0) > (self.observed_generation or 0)
        )

    @property
    def healthy(self) -> bool:
        return self.desired > 0 and self.ready >= self.desired and not self.in_progress


class LogEvidence(BaseModel):
    container: str
    previous: bool = False
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class Enrichment(BaseModel):
    pod: PodSnapshot | None = None
    rollout: RolloutInfo | None = None
    logs: list[LogEvidence] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    partial: bool = False
    duration_ms: int = 0

    @property
    def has_context(self) -> bool:
        return self.pod is not None or self.rollout is not None

    @property
    def evidence_lines(self) -> int:
        return sum(len(log.lines) for log in self.logs)
