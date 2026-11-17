"""Slack delivery: real incoming webhook, plus a file sink for local demos."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..errors import PublishError
from .blocks import RenderedMessage

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    async def publish(self, message: RenderedMessage) -> None: ...

    async def aclose(self) -> None: ...


class SlackPublisher:
    """Posts to a Slack incoming webhook.

    Slack answers 429 with ``Retry-After`` during bursts; honouring it is the
    difference between "silently dropped alerts" and a working integration.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 5.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def publish(self, message: RenderedMessage) -> None:
        payload: dict[str, Any] = {"text": message.text, "blocks": message.blocks}
        last_error: str = "unknown error"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._webhook_url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise PublishError(last_error) from exc

            if response.status_code == 200:
                return
            if response.status_code == 429 and attempt < self._max_retries:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                logger.warning("slack rate limited, retrying in %.1fs", retry_after)
                await asyncio.sleep(retry_after)
                continue
            if response.status_code >= 500 and attempt < self._max_retries:
                await asyncio.sleep(self._backoff(attempt))
                continue
            last_error = f"slack returned {response.status_code}: {response.text[:200]}"
            raise PublishError(last_error)
        raise PublishError(last_error)

    def _backoff(self, attempt: int) -> float:
        base = 0.5 * (2**attempt)
        return float(base + random.uniform(0, 0.25))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _retry_after_seconds(header: str | None) -> float:
    try:
        return max(float(header or "1"), 0.5)
    except ValueError:
        return 1.0


class SinkPublisher:
    """Writes the exact Slack webhook body as JSON lines.

    Used by the local demo, the kind e2e test and CI, so the whole pipeline is
    verifiable without a Slack workspace or a secret in the repository.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def publish(self, message: RenderedMessage) -> None:
        await asyncio.to_thread(self._write, message)

    def _write(self, message: RenderedMessage) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {"text": message.text, "blocks": message.blocks}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(body, ensure_ascii=False) + "\n")
        logger.info("slack payload written to %s", self._path)

    async def aclose(self) -> None:
        return None
