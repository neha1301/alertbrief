from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/", include_in_schema=False)
async def index() -> dict[str, str]:
    return {
        "service": "alert-enrichment",
        "docs": "/docs",
        "health": "/healthz",
        "ready": "/readyz",
        "metrics": "/metrics",
    }


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Must never call Kubernetes or Slack."""

    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> dict[str, Any]:
    """Readiness: dependencies were built. Checked by the Helm probe."""

    deps = getattr(request.app.state, "deps", None)
    if deps is None:
        raise HTTPException(status_code=503, detail="dependencies not initialised")
    return {
        "status": "ready",
        "slack_mode": deps.settings.resolved_slack_mode(),
        "dedup_backend": deps.settings.dedup_backend,
    }
