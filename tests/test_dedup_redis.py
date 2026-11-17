from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from alert_enrichment.dedup import RedisDedupStore  # noqa: E402


@pytest.fixture
def store() -> RedisDedupStore:
    client = fakeredis.FakeRedis(decode_responses=True)
    dedup = RedisDedupStore("redis://unused/0", stale_after=30, client=client)
    yield dedup
    dedup.close()


def test_reservation_is_shared_across_replicas(store: RedisDedupStore) -> None:
    first = store.reserve("incident-key", ttl_seconds=600)
    second = store.reserve("incident-key", ttl_seconds=600)

    assert first.duplicate is False
    assert first.count == 1
    assert second.duplicate is True
    assert second.count == 2
    assert second.first_seen == first.first_seen


def test_mark_sent_keeps_the_ttl(store: RedisDedupStore) -> None:
    store.reserve("k", ttl_seconds=600)
    store.mark_sent("k")

    assert store._client.ttl("alert-enrichment:dedup:k") > 0
    assert store.reserve("k", ttl_seconds=600).duplicate is True


def test_mark_sent_on_missing_key_is_a_noop(store: RedisDedupStore) -> None:
    store.mark_sent("never-seen")

    assert store._client.exists("alert-enrichment:dedup:never-seen") == 0


def test_release_allows_an_immediate_retry(store: RedisDedupStore) -> None:
    store.reserve("k", ttl_seconds=600)
    store.release("k")

    decision = store.reserve("k", ttl_seconds=600)

    assert decision.duplicate is False
    assert decision.count == 1


def test_keys_are_namespaced(store: RedisDedupStore) -> None:
    store.reserve("a", ttl_seconds=60)
    store.reserve("b", ttl_seconds=60)

    assert store._client.exists("alert-enrichment:dedup:a") == 1
    assert store._client.exists("alert-enrichment:dedup:b") == 1
