from __future__ import annotations

from alert_enrichment.dedup import MemoryDedupStore


def test_first_reservation_wins_and_counting_works() -> None:
    store = MemoryDedupStore(stale_after=30, clock=lambda: 100.0)

    first = store.reserve("incident-key", ttl_seconds=600)
    second = store.reserve("incident-key", ttl_seconds=600)
    third = store.reserve("incident-key", ttl_seconds=600)

    assert first.duplicate is False
    assert first.count == 1
    assert second.duplicate is True
    assert second.count == 2
    assert third.count == 3
    assert third.first_seen == first.first_seen


def test_ttl_expiry_starts_a_new_incident() -> None:
    now = [1000.0]
    store = MemoryDedupStore(clock=lambda: now[0])
    store.reserve("key", ttl_seconds=60)
    store.mark_sent("key")

    now[0] += 59
    assert store.reserve("key", ttl_seconds=60).duplicate is True

    # The sliding window is refreshed by every duplicate; wait it out fully.
    now[0] += 61
    fresh = store.reserve("key", ttl_seconds=60)
    assert fresh.duplicate is False
    assert fresh.count == 1


def test_in_flight_duplicate_is_suppressed_but_stale_reservation_is_reclaimed() -> None:
    now = [0.0]
    store = MemoryDedupStore(stale_after=30, clock=lambda: now[0])

    store.reserve("key", ttl_seconds=600)  # replica A is still enriching
    now[0] = 10
    assert store.reserve("key", ttl_seconds=600).duplicate is True

    # Replica A died before publishing; a retry must be able to take over.
    now[0] = 45
    reclaimed = store.reserve("key", ttl_seconds=600)
    assert reclaimed.duplicate is False
    assert reclaimed.count == 3


def test_release_allows_immediate_retry() -> None:
    store = MemoryDedupStore(clock=lambda: 5.0)
    store.reserve("key", ttl_seconds=600)
    store.release("key")

    decision = store.reserve("key", ttl_seconds=600)

    assert decision.duplicate is False
    assert decision.count == 1


def test_independent_keys_do_not_collide() -> None:
    store = MemoryDedupStore(clock=lambda: 5.0)

    assert store.reserve("a", ttl_seconds=600).duplicate is False
    assert store.reserve("b", ttl_seconds=600).duplicate is False
    assert store.reserve("a", ttl_seconds=600).count == 2


def test_close_clears_state() -> None:
    store = MemoryDedupStore(clock=lambda: 5.0)
    store.reserve("key", ttl_seconds=600)
    store.close()

    assert store.reserve("key", ttl_seconds=600).duplicate is False
