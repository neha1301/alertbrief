"""Deduplication: one Slack message per incident, with a retry-safe reservation.

Two layers of protection:

1. Alertmanager already groups alerts (``group_by``). That is not enough: a
   flapping pod produces a new webhook every evaluation interval, and an
   Alertmanager webhook timeout means *retry*, not "new incident".
2. This store debounces per (cluster, namespace, pod, container, severity) for
   ``ttl`` seconds and counts how many alerts were folded into the message.

The reservation is written *before* enrichment so a retried webhook (same
alert, Slack already got the message) does not produce a second message. A
reservation older than ``stale_after`` is reclaimed, so a crashed replica cannot
block an alert forever.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import AlertEnrichmentError


@dataclass(frozen=True)
class DedupDecision:
    key: str
    duplicate: bool
    count: int
    first_seen: float
    state: str


class DedupStore(Protocol):
    def reserve(self, key: str, ttl_seconds: float) -> DedupDecision: ...

    def mark_sent(self, key: str) -> None: ...

    def release(self, key: str) -> None: ...

    def close(self) -> None: ...


@dataclass
class _Entry:
    count: int
    first_seen: float
    updated: float
    state: str


class MemoryDedupStore:
    """Single-process, threaded, TTL + count. Default backend."""

    def __init__(
        self,
        *,
        stale_after: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._stale_after = stale_after
        self._clock = clock

    def reserve(self, key: str, ttl_seconds: float) -> DedupDecision:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry.updated >= ttl_seconds:
                self._entries.pop(key, None)
                entry = None

            if entry is None:
                self._entries[key] = _Entry(count=1, first_seen=now, updated=now, state="reserved")
                return DedupDecision(
                    key, duplicate=False, count=1, first_seen=now, state="reserved"
                )

            if entry.state == "reserved" and now - entry.updated > self._stale_after:
                entry.count += 1
                entry.updated = now
                return DedupDecision(
                    key,
                    duplicate=False,
                    count=entry.count,
                    first_seen=entry.first_seen,
                    state="reserved",
                )

            entry.count += 1
            entry.updated = now
            return DedupDecision(
                key,
                duplicate=True,
                count=entry.count,
                first_seen=entry.first_seen,
                state=entry.state,
            )

    def mark_sent(self, key: str) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.state = "sent"
                entry.updated = self._clock()

    def release(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def close(self) -> None:
        with self._lock:
            self._entries.clear()


_RESERVE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local stale = tonumber(ARGV[3])
local raw = redis.call('GET', key)

if raw then
  local entry = cjson.decode(raw)
  if now - entry['updated'] > ttl then
    redis.call('DEL', key)
  else
    local duplicate = 1
    if entry['state'] == 'reserved' and now - entry['updated'] > stale then
      duplicate = 0
    end
    entry['count'] = entry['count'] + 1
    entry['updated'] = now
    redis.call('SET', key, cjson.encode(entry), 'EX', ttl)
    return {duplicate, entry['count'], tostring(entry['first_seen'])}
  end
end

local fresh = {count=1, first_seen=now, updated=now, state='reserved'}
redis.call('SET', key, cjson.encode(fresh), 'EX', ttl)
return {0, 1, tostring(now)}
"""


class RedisDedupStore:
    """Shared dedup for multi-replica deployments.

    The read-modify-write happens inside a Lua script, so two replicas receiving
    the same alert at the same time cannot both reserve it.
    """

    def __init__(
        self,
        url: str,
        *,
        stale_after: float = 30.0,
        prefix: str = "alert-enrichment:dedup:",
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - exercised only without extra
                raise AlertEnrichmentError(
                    "DEDUP_BACKEND=redis requires the 'redis' extra: pip install '.[redis]'"
                ) from exc
            client = redis.Redis.from_url(url, decode_responses=True)
        self._client: Any = client
        self._script: Any = self._client.register_script(_RESERVE_LUA)
        self._stale_after = stale_after
        self._prefix = prefix

    def reserve(self, key: str, ttl_seconds: float) -> DedupDecision:
        full_key = self._prefix + key
        duplicate, count, first_seen = self._script(
            keys=[full_key],
            args=[time.time(), int(ttl_seconds), self._stale_after],
        )
        return DedupDecision(
            key=key,
            duplicate=bool(int(duplicate)),
            count=int(count),
            first_seen=float(first_seen),
            state="sent" if int(duplicate) else "reserved",
        )

    def mark_sent(self, key: str) -> None:
        full_key = self._prefix + key
        raw = self._client.get(full_key)
        if not raw:
            return
        entry = json.loads(raw)
        entry["state"] = "sent"
        ttl = self._client.ttl(full_key)
        self._client.set(full_key, json.dumps(entry), ex=max(int(ttl), 1))

    def release(self, key: str) -> None:
        self._client.delete(self._prefix + key)

    def close(self) -> None:
        self._client.close()
