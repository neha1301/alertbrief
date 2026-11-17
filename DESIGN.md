# Design notes

One page of decisions, the alternatives that were rejected, and the failure
modes each one buys protection against. This is the part of the repository that
matters when someone asks "why not just use Alertmanager's grouping?".

## 1. Where deduplication belongs

Alertmanager already has `group_by`, `group_wait` and `repeat_interval`. It does
**not** solve the three cases this service exists for:

| Case | Alertmanager behaviour | This service |
| --- | --- | --- |
| 4 different alert names on the same pod in one evaluation cycle | one webhook per *route group*, different groups stay separate | folds them into one message with `×4 alerts merged` |
| a flapping pod: firing → resolved → firing | a new webhook every cycle | one message per pod per `DEDUP_TTL_SECONDS` window, with an occurrence count |
| Alertmanager retries after a webhook timeout | the same payload arrives twice | reservation is written before enrichment, so the retry is suppressed |

So the layering is deliberate: **Alertmanager groups routes, this service
debounces the incident**. Cross-pod grouping stays Alertmanager's job; the README
does not pretend otherwise.

**Rejected:** doing dedup in the Slack publisher keyed by message text. Text
contains durations and timestamps, so the key changes on every retry.

## 2. Reservation before enrichment

The dedup key is reserved with a TTL *before* the Kubernetes API is touched:

```
reserve(key) ──duplicate?──▶ suppress, return 202
     │
     ▼
enrich (bounded) → render → publish → mark_sent(key)
     │
     └── failure ──▶ release(key)  # Alertmanager may retry
```

* A retried webhook cannot produce a second Slack message.
* A crashed replica cannot block an alert forever: a `reserved` entry older than
  `DEDUP_STALE_RESERVATION_SECONDS` is reclaimed by the next request.
* A failed publish releases the key, so the retry is not dropped. Covered by
  `test_publish_failure_returns_502_and_retry_is_allowed`.

**Rejected:** reserving after enrichment. Two replicas can finish enrichment at
the same time and both publish.

## 3. Bounded enrichment, never a dropped alert

Every Kubernetes call runs in a thread with a deadline
(`asyncio.to_thread` + `asyncio.wait_for`), inside a per-request budget
(`ENRICHMENT_BUDGET_SECONDS`, default 4s). Partial context is a first-class
state:

* pod missing (deleted between alert and webhook) → message still sent, with
  `Pod not found (deleted or renamed?)`;
* logs forbidden/timeout → `Partial context: log fetch failed: ...`;
* no pod label at all → raw alert details still reach Slack;
* all enrichment failed → HTTP 202, not 500, because the alert itself is valid.

**Rejected:** returning 5xx when enrichment fails. Alertmanager would retry
forever and the human sees nothing.

## 4. Sync Kubernetes client, async app

The official client is synchronous. Wrapping it with `asyncio.to_thread` and a
semaphore is simpler and better understood than adopting `kubernetes_asyncio`
(fewer maintainers, different error types). Logs for up to 3 containers are
fetched concurrently; independent incidents are processed concurrently up to
`MAX_CONCURRENT_ENRICHMENTS`.

**Trade-off:** `asyncio.wait_for` does not kill the worker thread; the HTTP call
keeps running until its own `_request_timeout` fires. That is why the client
gets a *hard* timeout (1.5s connect, 3s read) as well, instead of relying only on
the outer budget.

## 5. Logs: previous container first

A container that restarted has current logs that say "server starting" and
previous logs that contain the crash. When `restartCount > 0`, the service asks
for `previous=true`, falls back to the current container only if previous logs
are unavailable, and tells you which one it used in the message.

Lines are filtered with a deliberately broad error regex, keep two continuation
lines (stack traces), collapse repeated lines into `(xN)`, and return the newest
`MAX_ERROR_LINES`. ANSI colour codes are stripped; mrkdwn and code fences are
escaped because log lines are untrusted input.

## 6. Slack limits are enforced, not hoped for

Slack rejects the whole message for one bad button URL or an oversized block.
Therefore:

* text is truncated to 2.9k chars per block (Slack hard limit 3k);
* at most 50 blocks, log lines capped, fences balanced;
* buttons are only emitted for absolute `http(s)` URLs — otherwise the action
  block is omitted, never sent invalid;
* silence links are pre-filled with matchers
  (`/#/silences/new?filter=alertname%3D%22...%22`) so one click creates a
  correctly scoped silence.

`tests/test_blocks.py::test_slack_limits_hold_for_hostile_logs` throws 400
600-char lines at it and asserts the payload still fits.

## 7. Multi-replica dedup

`DEDUP_BACKEND=memory` is the default: zero dependencies, correct for a single
replica and honest about it. For `replicaCount > 1`, set `redis`; the
read-modify-write is a single Lua script (`SET`/`GET`/`INCR` semantics atomic),
so two replicas receiving the same alert cannot both reserve it.

**Rejected:** sticky sessions or leader election. Both add operational surface
for a problem Redis already solves.

## 8. What this service is not

* Not an alert router: it does not decide who gets paged.
* Not a log store: it quotes the last few lines, it does not index anything.
* Not a replacement for Alertmanager inhibition (planned integration, see
  roadmap).
* No auto-remediation: it never deletes pods or rolls back deployments. The
  RBAC in `deploy/helm` is `get`/`list` only, on four resource types.

## Roadmap (deliberately ordered by value)

1. Slack `chat.update` to edit the original message when the occurrence count
   grows, instead of only counting in the first message.
2. Group by owning workload, so 10 crashing pods of one Deployment become one
   message (needs an owner-first key plus a two-phase reservation).
3. Event-driven cache with a shared informer to remove per-alert API reads.
4. Alertmanager inhibition suggestions in the message (`Silence` already links
   to a pre-filled form).
