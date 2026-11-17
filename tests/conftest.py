from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alert_enrichment.dedup import MemoryDedupStore
from alert_enrichment.main import create_app
from alert_enrichment.settings import Settings
from support import FakeCluster, RecordingPublisher, crashloop_cluster, load_json


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, slack_mode="sink", dedup_ttl_seconds=600, log_json=False)


@pytest.fixture
def cluster() -> FakeCluster:
    return crashloop_cluster()


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def dedup() -> MemoryDedupStore:
    return MemoryDedupStore()


@pytest.fixture
def app(settings: Settings, cluster: FakeCluster, publisher: RecordingPublisher, dedup: MemoryDedupStore) -> Any:
    return create_app(settings, cluster=cluster, publisher=publisher, dedup=dedup)


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def payload() -> dict[str, Any]:
    result: dict[str, Any] = load_json("webhook_crashloop.json")
    return result
