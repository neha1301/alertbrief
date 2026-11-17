"""Slack Block Kit rendering.

Everything here is deliberately defensive: Slack rejects an entire message for
one malformed button URL or an oversized block, so limits are enforced in code
and covered by tests with adversarial log lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..models import Alert, Enrichment, LogEvidence, PodSnapshot, RolloutInfo
from ..utils import humanize_age, humanize_delta, truncate, utcnow

SLACK_MAX_BLOCKS = 50
SECTION_LIMIT = 2900  # Slack hard limit is 3000; leave room for markup
HEADER_LIMIT = 150
FIELD_LIMIT = 250

SEVERITY_EMOJI = {
    "critical": "🚨",
    "warning": "⚠️",
    "info": "ℹ️",
    "unknown": "⚠️",
}


@dataclass
class RenderedMessage:
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)


def _esc(value: str) -> str:
    """Slack mrkdwn escape. Applied to user/cluster data, never to our markup."""

    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _valid_url(value: str | None) -> bool:
    if value is None:
        return False
    return value.startswith(("http://", "https://"))


def _silence_url(alert: Alert, base: str) -> str | None:
    if not base:
        return None
    filters = [
        f'alertname="{alert.alertname}"',
        f'namespace="{alert.namespace}"',
    ]
    if alert.pod_name:
        filters.append(f'pod="{alert.pod_name}"')
    query = "&".join(f"filter={quote(item, safe='')}" for item in filters)
    return f"{base.rstrip('/')}/#/silences/new?{query}"


def _field(label: str, value: str) -> dict[str, Any]:
    return {
        "type": "mrkdwn",
        "text": f"*{label}*\n{_esc(truncate(value, FIELD_LIMIT))}",
    }


def _container_status(pod: PodSnapshot) -> str:
    if not pod.containers:
        return pod.phase
    lines: list[str] = []
    for container in pod.containers:
        if container.state == "waiting" and container.reason:
            detail = container.reason
        elif container.state == "terminated":
            detail = f"Terminated: {container.reason or 'exit'}"
        elif container.state == "running":
            detail = "Running"
        else:
            detail = container.reason or container.state
        if container.last_exit_code not in (None, 0):
            detail += f", last exit {container.last_exit_code}"
        if len(pod.containers) > 1:
            detail = f"{container.name}: {detail}"
        lines.append(detail)
    return "\n".join(lines)


def _restarts(pod: PodSnapshot, now: datetime) -> str:
    if pod.total_restarts == 0:
        return "0"
    stamps = [c.last_terminated_at for c in pod.containers if c.last_terminated_at]
    if not stamps:
        return str(pod.total_restarts)
    age = humanize_age(max(stamps), now=now)
    return f"{pod.total_restarts} (last {age} ago)" if age else str(pod.total_restarts)


def _rollout_line(rollout: RolloutInfo | None, now: datetime) -> str | None:
    if rollout is None:
        return None
    base = f"{rollout.kind} {rollout.name}: {rollout.ready}/{rollout.desired} ready"
    if rollout.in_progress:
        base += " (rolling out)"
    age = humanize_age(rollout.last_update, now=now)
    if age:
        base += f", updated {age} ago"
    return base


def _context_line(
    alert: Alert,
    pod: PodSnapshot | None,
    occurrence: int,
    now: datetime,
) -> str:
    parts = [alert.cluster, f"namespace `{alert.namespace}`"]
    if pod and pod.owner:
        parts.append(f"{pod.owner.kind} `{pod.owner.name}`")
    elif alert.labels.get("deployment"):
        parts.append(f"Deployment `{alert.labels['deployment']}`")
    anchor = alert.resolved_at if alert.is_resolved else alert.firing_since
    age = humanize_age(anchor, now=now)
    if age:
        parts.append(f"{'resolved' if alert.is_resolved else 'firing'} {age} ago")
    if (
        alert.is_resolved
        and alert.firing_since is not None
        and alert.resolved_at is not None
    ):
        duration = humanize_delta(
            (alert.resolved_at - alert.firing_since).total_seconds()
        )
        parts.append(f"after {duration}")
    if occurrence > 1:
        parts.append(f"×{occurrence} alerts merged")
    return " · ".join(parts)


def _logs_text(logs: list[LogEvidence], budget: int) -> str | None:
    chunks: list[str] = []
    for evidence in logs:
        if not evidence.lines:
            continue
        title = f"# {evidence.container}"
        if evidence.previous:
            title += " (previous container)"
        # Cluster log lines are untrusted: escape mrkdwn, then neutralise any
        # fence the application may have logged itself.
        body_lines = "\n".join(_esc(line) for line in evidence.lines)
        chunks.append(title + "\n" + body_lines)
    if not chunks:
        return None

    body = "\n\n".join(chunks).replace("```", "'''")
    header = "*Last error lines*\n```\n"
    footer = "\n```"
    room = budget - len(header) - len(footer)
    if len(body) > room:
        lines = body.splitlines()
        while len(lines) > 1 and len("\n".join(lines)) > room - 2:
            lines.pop(0)
        body = "…\n" + "\n".join(lines)
    return header + body + footer


def _events_text(events: list[str]) -> str | None:
    if not events:
        return None
    body = "\n".join(f"• {_esc(event)}" for event in events)
    return f"*Recent warning events*\n{truncate(body, 900)}"


def _actions(
    alert: Alert,
    *,
    alertmanager_url: str,
    grafana_url: str,
) -> dict[str, Any] | None:
    elements: list[dict[str, Any]] = []
    silence = _silence_url(alert, alertmanager_url)
    if _valid_url(silence):
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Silence 1h"},
                "url": silence,
                "style": "danger",
            }
        )
    runbook = alert.annotations.get("runbook_url")
    if _valid_url(runbook):
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Runbook"},
                "url": runbook,
            }
        )
    dashboard = alert.annotations.get("dashboard_url") or alert.annotations.get("grafana_url")
    if not _valid_url(dashboard):
        dashboard = grafana_url or None
    if _valid_url(dashboard):
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Grafana"},
                "url": dashboard,
            }
        )
    if not elements:
        return None
    return {"type": "actions", "elements": elements[:5]}


def _kubectl_command(alert: Alert, enrichment: Enrichment) -> str | None:
    pod = alert.pod_name
    if not pod:
        return None
    command = f"kubectl -n {alert.namespace} logs {pod}"
    if alert.container:
        command += f" -c {alert.container}"
    elif enrichment.logs and enrichment.logs[0].container:
        command += f" -c {enrichment.logs[0].container}"
    if any(evidence.previous for evidence in enrichment.logs):
        command += " --previous"
    return command + " --tail=100"


def render_alert(
    alert: Alert,
    enrichment: Enrichment,
    *,
    alertmanager_url: str = "",
    grafana_url: str = "",
    occurrence: int = 1,
    now: datetime | None = None,
) -> RenderedMessage:
    now = now or utcnow()
    pod = enrichment.pod
    target = alert.pod_name or alert.namespace
    emoji = "✅" if alert.is_resolved else SEVERITY_EMOJI[alert.severity]

    header_text = truncate(f"{emoji} {alert.alertname} — {alert.namespace}/{target}", HEADER_LIMIT)
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": _context_line(alert, pod, occurrence, now)}
            ],
        },
    ]

    if alert.summary:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": truncate(_esc(alert.summary), 800)},
            }
        )

    if pod is not None:
        image = "unknown"
        if pod.images:
            image = pod.images[0]
        elif enrichment.rollout and enrichment.rollout.images:
            image = enrichment.rollout.images[0]
        fields = [
            _field("Status", _container_status(pod) if not alert.is_resolved else pod.phase),
            _field("Restarts", _restarts(pod, now)),
            _field("Ready", pod.ready),
            _field("Node", pod.node or "unknown"),
            _field("Image", image),
        ]
        rollout = _rollout_line(enrichment.rollout, now)
        if rollout:
            fields.append(_field("Rollout", rollout))
        blocks.append({"type": "section", "fields": fields})

    logs_text = _logs_text(enrichment.logs, SECTION_LIMIT)
    if logs_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": logs_text}})
    elif not alert.is_resolved and enrichment.logs:
        failures = "; ".join(
            f"{item.container}: {item.error}" for item in enrichment.logs if item.error
        )
        note = failures or "no error lines in the last log window"
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": truncate(f"Logs: {_esc(note)}", 500)}
                ],
            }
        )

    events_text = _events_text(enrichment.events)
    if events_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": events_text}})

    if enrichment.errors:
        note = "; ".join(enrichment.errors)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": truncate(f"⚠️ Partial context: {_esc(note)}", 600)}
                ],
            }
        )

    if not alert.is_resolved:
        actions = _actions(
            alert,
            alertmanager_url=alertmanager_url,
            grafana_url=grafana_url,
        )
        if actions:
            blocks.append(actions)

    command = _kubectl_command(alert, enrichment)
    footer_parts = []
    if command:
        footer_parts.append(f"`{command}`")
    footer_parts.append(f"enriched in {enrichment.duration_ms}ms · {now:%H:%M:%SZ}")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(footer_parts)}],
        }
    )

    summary = truncate(alert.summary, 220) or "alert fired"
    text = truncate(
        f"{emoji} {alert.alertname} {alert.namespace}/{target}: "
        f"{'resolved' if alert.is_resolved else summary}",
        280,
    )
    return RenderedMessage(text=text, blocks=blocks[:SLACK_MAX_BLOCKS])
