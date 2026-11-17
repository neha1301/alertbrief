#!/usr/bin/env bash
# End-to-end local demo:
#   kind cluster -> deliberately crashing Deployment -> FastAPI service
#   -> Alertmanager payload -> Slack message (sink) + HTML preview.
#
# Requirements: docker, kind, kubectl, curl, python3.13 (for the venv).
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${CLUSTER:-alert-enrichment-demo}"
NAMESPACE="${NAMESPACE:-payments}"
PORT="${PORT:-8080}"
SINK="${SINK:-var/slack-sink.jsonl}"
PREVIEW="${PREVIEW:-var/slack-preview.html}"

for binary in kind kubectl curl; do
  command -v "$binary" >/dev/null || { echo "missing dependency: $binary"; exit 1; }
done

if [[ ! -x .venv/bin/python ]]; then
  echo "==> creating virtualenv"
  python3.13 -m venv .venv
  .venv/bin/pip install -q -e '.[dev]'
fi

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "==> creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER" --wait 120s
else
  echo "==> reusing kind cluster '$CLUSTER'"
  kubectl config use-context "kind-$CLUSTER" >/dev/null
fi

echo "==> deploying the deliberately crash-looping checkout app"
kubectl apply -f demo/broken-app.yaml
kubectl -n "$NAMESPACE" rollout status deployment/checkout --timeout=60s >/dev/null 2>&1 || true

echo -n "==> waiting for real restarts"
restarts=0
for _ in $(seq 1 90); do
  metric=$(kubectl -n "$NAMESPACE" get pods -l app=checkout -o json 2>/dev/null \
    | .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(sum(c.get("restartCount",0) for p in d["items"] for c in (p["status"].get("containerStatuses") or [])))' \
    || echo 0)
  restarts="${metric:-0}"
  if [[ "$restarts" -ge 3 ]]; then break; fi
  printf "."
  sleep 2
done
echo " (restarts=$restarts)"

if [[ "$restarts" -lt 1 ]]; then
  echo "pod never restarted; inspect with: kubectl -n $NAMESPACE get pods" >&2
  exit 1
fi

mkdir -p var
rm -f "$SINK"

export KUBERNETES_IN_CLUSTER=false
export SLACK_MODE="${SLACK_MODE:-sink}"
export ALERTMANAGER_URL="${ALERTMANAGER_URL:-https://alertmanager.example.com}"
export LOG_JSON=false
if [[ "$SLACK_MODE" == "sink" ]]; then
  export SLACK_SINK_PATH="$SINK"
  echo "==> Slack mode: sink (set SLACK_WEBHOOK_URL to send a real message)"
else
  if [[ -z "${SLACK_WEBHOOK_URL:-}" ]]; then
    echo "SLACK_MODE=$SLACK_MODE but SLACK_WEBHOOK_URL is not set" >&2
    exit 1
  fi
  echo "==> Slack mode: real webhook"
fi

echo "==> starting the service on :$PORT"
.venv/bin/python -m uvicorn alert_enrichment.main:app \
  --host 127.0.0.1 --port "$PORT" --log-level warning &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.2
done

echo "==> building an Alertmanager payload from the live pod"
.venv/bin/python demo/make_payload.py \
  --namespace "$NAMESPACE" --cluster "kind-$CLUSTER" --output var/payload.json

echo "==> POST /webhook/alertmanager (3 alerts about one pod)"
curl -sS -X POST "http://127.0.0.1:$PORT/webhook/alertmanager" \
  -H 'content-type: application/json' --data @var/payload.json \
  | .venv/bin/python -m json.tool

echo "==> POST the same batch again (deduplicated)"
curl -sS -X POST "http://127.0.0.1:$PORT/webhook/alertmanager" \
  -H 'content-type: application/json' --data @var/payload.json \
  | .venv/bin/python -m json.tool

echo "==> rendering the Slack message"
if [[ -f "$SINK" ]]; then
  .venv/bin/python demo/render_sink.py "$SINK" --html "$PREVIEW"
  echo
  echo "Done. Slack message JSON : $SINK"
  echo "     HTML preview        : $PREVIEW"
else
  echo
  echo "Done. Check the Slack channel."
fi
echo "     Cleanup             : make kind-down"
