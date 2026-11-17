from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request

from .. import metrics
from ..k8s import enrich_alert
from ..models import Alert, AlertmanagerWebhook
from ..runtime import Deps
from ..slack import render_alert

router = APIRouter()
logger = logging.getLogger(__name__)


def _deps(request: Request) -> Deps:
    deps = getattr(request.app.state, "deps", None)
    if deps is None:
        raise HTTPException(status_code=503, detail="service is still starting")
    return cast(Deps, deps)


@router.post("/webhook/alertmanager", status_code=202)
async def receive_alertmanager(
    request: Request,
    payload: AlertmanagerWebhook,
    dry_run: bool = False,
) -> Any:
    """Receive an Alertmanager v4 webhook and publish at most one message per incident.

    ``?dry_run=true`` renders the exact Slack payload without touching dedup
    state or Slack. It is the fastest way to debug what the service will say.
    """

    deps = _deps(request)
    settings = deps.settings
    started = time.monotonic()

    if not payload.alerts:
        metrics.WEBHOOKS.labels(status="empty").inc()
        return {"received": 0, "sent": 0, "suppressed": 0, "ignored": 0, "errors": []}

    external_url = payload.externalURL or settings.alertmanager_url

    if dry_run:
        messages: list[dict[str, Any]] = []
        for alert in payload.alerts[: settings.max_alerts_per_request]:
            enrichment = await enrich_alert(alert, deps.cluster, settings)
            rendered = render_alert(
                alert,
                enrichment,
                alertmanager_url=external_url,
                grafana_url=settings.grafana_url,
            )
            messages.append(
                {"alert": alert.alertname, "text": rendered.text, "blocks": rendered.blocks}
            )
        metrics.WEBHOOKS.labels(status="dry_run").inc()
        return {"dry_run": True, "received": len(payload.alerts), "messages": messages}

    # Fold in-payload duplicates before reserving, so a 20-alert burst for one
    # pod becomes one reservation and one message.
    groups: dict[str, list[Alert]] = {}
    for alert in payload.alerts:
        key = alert.dedup_key() + (":resolved" if alert.is_resolved else "")
        groups.setdefault(key, []).append(alert)

    limited = list(groups.items())[: settings.max_alerts_per_request]
    overflow = list(groups.items())[settings.max_alerts_per_request :]
    ignored = sum(len(items) for _, items in overflow)
    if ignored:
        metrics.ALERTS.labels(decision="ignored").inc(ignored)
        logger.warning("ignored %d alerts over MAX_ALERTS_PER_REQUEST", ignored)

    semaphore = asyncio.Semaphore(settings.max_concurrent_enrichments)

    async def process(key: str, alerts: list[Alert]) -> dict[str, Any]:
        async with semaphore:
            representative = alerts[0]
            decision = deps.dedup.reserve(key, settings.dedup_ttl_seconds)
            if decision.duplicate:
                metrics.ALERTS.labels(decision="suppressed").inc(len(alerts))
                logger.info(
                    "suppressed duplicate alert",
                    extra={"incident": key, "occurrences": decision.count},
                )
                return {
                    "key": key,
                    "status": "suppressed",
                    "alerts": len(alerts),
                    "occurrences": max(len(alerts), decision.count),
                }

            try:
                enrichment = await enrich_alert(representative, deps.cluster, settings)
                rendered = render_alert(
                    representative,
                    enrichment,
                    alertmanager_url=external_url,
                    grafana_url=settings.grafana_url,
                    occurrence=max(len(alerts), decision.count),
                )
                await deps.publisher.publish(rendered)
                deps.dedup.mark_sent(key)
                occurrence = max(len(alerts), decision.count)
                metrics.ALERTS.labels(decision="sent").inc(len(alerts))
                metrics.ENRICHMENT_SECONDS.observe(enrichment.duration_ms / 1000)
                metrics.SLACK_PUBLISH.labels(result="ok").inc()
                logger.info(
                    "published enriched alert",
                    extra={
                        "incident": key,
                        "alertname": representative.alertname,
                        "occurrences": occurrence,
                        "enrichment_ms": enrichment.duration_ms,
                        "partial": enrichment.partial,
                    },
                )
                return {
                    "key": key,
                    "status": "sent",
                    "alerts": len(alerts),
                    "occurrences": occurrence,
                    "enrichment_ms": enrichment.duration_ms,
                    "partial": enrichment.partial,
                }
            except Exception as exc:
                deps.dedup.release(key)
                metrics.ALERTS.labels(decision="error").inc(len(alerts))
                metrics.SLACK_PUBLISH.labels(result="error").inc()
                logger.exception(
                    "failed to process alert",
                    extra={"incident": key, "alertname": representative.alertname},
                )
                return {
                    "key": key,
                    "status": "error",
                    "alerts": len(alerts),
                    "error": str(exc)[:300],
                }

    results = await asyncio.gather(*(process(key, alerts) for key, alerts in limited))
    sent = sum(1 for result in results if result["status"] == "sent")
    suppressed = sum(1 for result in results if result["status"] == "suppressed")
    errors = [str(result["error"]) for result in results if result["status"] == "error"]

    if sent == 0 and errors:
        metrics.WEBHOOKS.labels(status="failed").inc()
        # Non-2xx tells Alertmanager to retry; the reservation was released.
        raise HTTPException(status_code=502, detail={"errors": errors})

    metrics.WEBHOOKS.labels(status="accepted").inc()
    return {
        "received": len(payload.alerts),
        "sent": sent,
        "suppressed": suppressed,
        "ignored": ignored,
        "errors": errors,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "results": results,
    }
