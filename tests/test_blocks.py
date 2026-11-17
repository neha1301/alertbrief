from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from alert_enrichment.k8s import enrich_alert
from alert_enrichment.models import Alert, Enrichment, LogEvidence, PodSnapshot
from alert_enrichment.settings import Settings
from alert_enrichment.slack import render_alert
from support import NAMESPACE, POD, FakeCluster

FIXED_NOW = datetime(2026, 11, 17, 18, 10, 0, tzinfo=UTC)


def as_text(blocks: Any) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def alert(**overrides: Any) -> Alert:
    labels = {
        "alertname": "PodCrashLooping",
        "namespace": NAMESPACE,
        "pod": POD,
        "cluster": "prod-blr-1",
        "severity": "warning",
    }
    annotations = overrides.pop("annotations", {})
    return Alert(labels=labels, annotations=annotations, **overrides)


def all_texts(value: Any) -> list[str]:
    """Every user-visible string in a Block Kit payload, recursively."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "url"} and isinstance(item, str):
                found.append(item)
            else:
                found.extend(all_texts(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(all_texts(item))
    return found


async def test_message_is_actionable(cluster: FakeCluster, settings: Settings) -> None:
    subject = alert(
        annotations={"runbook_url": "https://runbooks.example.com/payments/checkout-crashloop"}
    )
    enrichment = await enrich_alert(subject, cluster, settings)

    message = render_alert(
        subject,
        enrichment,
        alertmanager_url="https://alertmanager.example.com",
        occurrence=4,
        now=FIXED_NOW,
    )
    body = as_text(message.blocks)

    assert message.text.startswith("⚠️ PodCrashLooping")
    assert "CrashLoopBackOff" in body
    assert "12 (last 11m ago)" in body
    assert "0/1" in body
    assert "Deployment checkout: 3/3 ready" in body
    assert "db pool exhausted" in body
    assert "at pool.acquire" in body
    assert "×4 alerts merged" in body
    # Silence link is pre-filled with matchers, URL-encoded for Alertmanager.
    assert "silences/new?filter=alertname%3D%22PodCrashLooping%22" in body
    assert "filter=pod%3D%22" + POD + "%22" in body
    assert "https://runbooks.example.com/payments/checkout-crashloop" in body
    assert "kubectl -n payments logs " + POD in body
    assert "--previous --tail=100" in body
    assert "enriched in" in body
    assert body.count("```") == 2


def test_logs_are_escaped_and_cannot_break_the_code_fence() -> None:
    enrichment = Enrichment(
        pod=None,
        logs=[
            LogEvidence(
                container="checkout",
                lines=["<script>alert('x')</script> & ```rm -rf /```"],
            )
        ],
        duration_ms=3,
    )
    message = render_alert(alert(), enrichment, now=FIXED_NOW)
    body = as_text(message.blocks)

    assert "&lt;script&gt;" in body
    assert "&amp;" in body
    assert "```rm" not in body
    assert message.blocks[-2]["type"] == "section"


def test_slack_limits_hold_for_hostile_logs() -> None:
    lines = [f"{index:04d} " + "x" * 600 for index in range(400)]
    enrichment = Enrichment(
        pod=PodSnapshot(name=POD, namespace=NAMESPACE, ready="0/1", total_restarts=9),
        logs=[LogEvidence(container="checkout", previous=True, lines=lines)],
        duration_ms=42,
    )

    message = render_alert(alert(), enrichment, now=FIXED_NOW)

    assert len(message.blocks) <= 50
    assert len(message.text) <= 3000
    for text in all_texts(message.blocks):
        assert len(text) <= 3000, text[:80]
    # The fence must still be balanced after truncation.
    log_blocks = [
        block for block in message.blocks if block["type"] == "section" and "text" in block
    ]
    assert any("…" in block["text"]["text"] for block in log_blocks)


def test_resolved_alert_is_green_and_has_no_silence_button() -> None:
    subject = alert(
        status="resolved",
        startsAt=datetime(2026, 11, 17, 17, 50, tzinfo=UTC),
        endsAt=datetime(2026, 11, 17, 18, 5, tzinfo=UTC),
    )
    enrichment = Enrichment(pod=None, duration_ms=1)

    message = render_alert(
        subject, enrichment, alertmanager_url="https://alertmanager.example.com", now=FIXED_NOW
    )

    assert message.blocks[0]["text"]["text"].startswith("✅")
    assert "resolved 5m ago" in as_text(message.blocks)
    assert not any(block["type"] == "actions" for block in message.blocks)


def test_no_alertmanager_url_means_no_dead_button() -> None:
    message = render_alert(alert(), Enrichment(duration_ms=1), now=FIXED_NOW)

    assert not any(block["type"] == "actions" for block in message.blocks)


def test_order_of_magnitude_and_partial_context_are_visible() -> None:
    enrichment = Enrichment(errors=["log fetch failed: timeout"], partial=True, duration_ms=7)

    message = render_alert(alert(), enrichment, now=FIXED_NOW)
    body = as_text(message.blocks)

    assert "Partial context" in body
    assert "log fetch failed" in body
