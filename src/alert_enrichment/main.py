"""FastAPI application factory.

Dependencies are injected through ``create_app`` so tests can run the real
routes against a fake cluster, a recording publisher and an in-memory dedup
store — no monkeypatching of module globals.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from . import __version__
from .api import health, webhook
from .dedup import DedupStore
from .errors import AlertEnrichmentError
from .k8s import ClusterClient
from .logging_setup import configure_logging
from .runtime import Deps, build_cluster, build_dedup, build_publisher
from .settings import Settings
from .slack import Publisher

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    cluster: ClusterClient | None = None,
    publisher: Publisher | None = None,
    dedup: DedupStore | None = None,
) -> FastAPI:
    resolved = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved.log_level, resolved.log_json)
        deps = Deps(
            settings=resolved,
            cluster=cluster or build_cluster(resolved),
            publisher=publisher or build_publisher(resolved),
            dedup=dedup or build_dedup(resolved),
        )
        app.state.deps = deps
        logger.info(
            "alert-enrichment started",
            extra={
                "version": __version__,
                "slack_mode": resolved.resolved_slack_mode(),
                "dedup_backend": resolved.dedup_backend,
            },
        )
        try:
            yield
        finally:
            await deps.aclose()
            app.state.deps = None

    app = FastAPI(
        title="Alert Enrichment Service",
        description=(
            "Turns Alertmanager webhooks into one actionable Slack message with "
            "Kubernetes context: pod state, restarts, rollout and prior-container logs."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.mount("/metrics", make_asgi_app())

    @app.exception_handler(AlertEnrichmentError)
    async def handle_domain_error(_: Request, exc: AlertEnrichmentError) -> JSONResponse:
        logger.warning("domain error: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return app


app = create_app()
