#!/usr/bin/env python3
"""Build an Alertmanager v4 payload from a pod that is actually crashing.

The demo intentionally sends three alerts about the same pod, the way
Alertmanager would after a few evaluation intervals. The service folds them into
one Slack message.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def kubectl_json(*args: str) -> dict[str, object]:
    output = subprocess.check_output(["kubectl", *args], text=True)
    result: dict[str, object] = json.loads(output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="payments")
    parser.add_argument("--cluster", default="alert-enrichment-demo")
    parser.add_argument("--alertmanager-url", default="https://alertmanager.example.com")
    parser.add_argument("--output", default="-")
    args = parser.parse_args()

    pods = kubectl_json("get", "pods", "-n", args.namespace, "-o", "json").get("items", [])
    if not pods:
        print(f"no pods in namespace {args.namespace}", file=sys.stderr)
        return 1

    def restarts(pod: dict[str, object]) -> int:
        status = pod.get("status") or {}
        statuses = status.get("containerStatuses") or []  # type: ignore[union-attr]
        return sum(int(item.get("restartCount", 0)) for item in statuses)

    pod = max(pods, key=restarts)  # type: ignore[arg-type]
    name = pod["metadata"]["name"]  # type: ignore[index]
    starts = (datetime.now(tz=timezone.utc) - timedelta(minutes=6)).replace(microsecond=0)
    starts_at = starts.isoformat().replace("+00:00", "Z")
    common = {
        "namespace": args.namespace,
        "pod": name,
        "cluster": args.cluster,
        "severity": "warning",
        "deployment": "checkout",
    }

    def alert(alertname: str, summary: str, fingerprint: str) -> dict[str, object]:
        return {
            "status": "firing",
            "labels": {"alertname": alertname, **common},
            "annotations": {
                "summary": summary,
                "runbook_url": "https://runbooks.example.com/payments/checkout-crashloop",
                "dashboard_url": "https://grafana.example.com/d/payments/checkout",
            },
            "startsAt": starts_at,
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "https://prometheus.example.com/graph",
            "fingerprint": fingerprint,
        }

    payload = {
        "version": "4",
        "groupKey": f'{{}}/{{severity="warning"}}:{{namespace="{args.namespace}"}}',
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "alert-enrichment",
        "groupLabels": {"namespace": args.namespace},
        "commonLabels": common,
        "commonAnnotations": {
            "runbook_url": "https://runbooks.example.com/payments/checkout-crashloop"
        },
        "externalURL": args.alertmanager_url,
        "alerts": [
            alert(
                "PodCrashLooping",
                f"Pod {name} is restarting more than 5 times in 15 minutes",
                "demo0001",
            ),
            alert("PodNotReady", f"Pod {name} has been unready for 10 minutes", "demo0002"),
            alert("HighRestartCount", "Container checkout restarted 12 times in the last hour", "demo0003"),
        ],
    }

    encoded = json.dumps(payload, indent=2)
    if args.output == "-":
        print(encoded)
    else:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
