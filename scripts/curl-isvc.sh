#!/usr/bin/env bash
#
# curl-isvc.sh — send a prediction request to a KServe InferenceService.
#
#   Usage: scripts/curl-isvc.sh <namespace> <isvc-name> <json-file>
#   e.g.   scripts/curl-isvc.sh default sklearn-iris tests/iris-input.json
#
# ---------------------------------------------------------------------------
# WHY A PORT-FORWARD AND A Host HEADER
# ---------------------------------------------------------------------------
# Knative routes by HTTP Host header, not by path or IP. Every InferenceService
# gets a hostname like sklearn-iris.default.hive.local, and the Kourier gateway
# decides where to send a request by matching that header against its Routes.
#
# HIVE-LOCAL's domain (hive.local) deliberately does not resolve in DNS — see
# host/serving/knative/config-domain-patch.yaml. So we do what production does with
# real DNS + MetalLB + Kong, but by hand:
#
#   1. port-forward the Kourier gateway Service to localhost
#   2. send the request to localhost
#   3. carry the real hostname in an explicit `Host:` header
#
# Kourier cannot tell the difference — the header is all it looks at. Omit the
# header and Kourier has no matching Route and returns 404.
#
# Production replaces this whole script with an ordinary HTTPS call to a resolvable
# hostname, with a Keycloak bearer token.
#
# ---------------------------------------------------------------------------
# TIMING
# ---------------------------------------------------------------------------
# Timings come from curl's own -w counters, not from wrapping `date` around the
# call, so they are millisecond-accurate and exclude the port-forward setup:
#
#   time_connect       TCP connect to the local forwarder
#   time_starttransfer time to first response byte — this is where a COLD START
#                      shows up: the request waits at the Knative activator while
#                      a pod is scheduled, the model is downloaded and the server
#                      boots
#   time_total         whole request
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $(basename "$0") <namespace> <isvc-name> <json-file>" >&2
  exit 2
fi

NS="$1"
NAME="$2"
JSON_FILE="$3"

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
# Generous: a cold start includes pulling the model from GCS and booting the server.
MAX_TIME="${MAX_TIME:-300}"

kc() { kubectl --context "$KCTX" "$@"; }

[[ -f "$JSON_FILE" ]] || { echo "ERROR: no such json file: ${JSON_FILE}" >&2; exit 1; }

# --- resolve the hostname Knative expects -----------------------------------
# .status.url is the authoritative value — it already reflects config-domain, so
# this keeps working if the domain changes.
ISVC_URL="$(kc get inferenceservice "$NAME" -n "$NS" -o jsonpath='{.status.url}')"
if [[ -z "$ISVC_URL" ]]; then
  echo "ERROR: inferenceservice ${NS}/${NAME} has no .status.url yet — is it Ready?" >&2
  kc get inferenceservice "$NAME" -n "$NS" >&2 || true
  exit 1
fi

# http://sklearn-iris.default.hive.local  ->  sklearn-iris.default.hive.local
HOST="${ISVC_URL#http://}"
HOST="${HOST#https://}"
HOST="${HOST%%/*}"

# --- port-forward lifecycle --------------------------------------------------
PF_PID=""
PF_LOG=""

cleanup() {
  if [[ -n "$PF_PID" ]] && kill -0 "$PF_PID" 2>/dev/null; then
    kill "$PF_PID" 2>/dev/null || true
    wait "$PF_PID" 2>/dev/null || true
  fi
  [[ -n "$PF_LOG" && -f "$PF_LOG" ]] && rm -f "$PF_LOG"
  return 0
}
trap cleanup EXIT INT TERM

PF_LOG="$(mktemp -t curl-isvc-pf)"

# Invoke kubectl DIRECTLY, not through the kc() helper: backgrounding a shell
# function makes $! the PID of the wrapping subshell, so the trap would kill the
# wrapper and orphan the real port-forward, leaving :8080 held after every run.
kubectl --context "$KCTX" port-forward -n kourier-system svc/kourier \
  "${LOCAL_PORT}:80" >"$PF_LOG" 2>&1 &
PF_PID=$!

# Wait for readiness rather than sleeping a fixed interval.
ready=0
for _ in $(seq 1 100); do
  if grep -q "Forwarding from" "$PF_LOG" 2>/dev/null; then ready=1; break; fi
  if ! kill -0 "$PF_PID" 2>/dev/null; then
    echo "ERROR: port-forward exited before it was ready:" >&2
    cat "$PF_LOG" >&2
    echo >&2
    echo "If this says 'address already in use', another process holds ${LOCAL_PORT}." >&2
    echo "Re-run with LOCAL_PORT=8081 $(basename "$0") ..." >&2
    exit 1
  fi
  sleep 0.1
done
[[ $ready -eq 1 ]] || { echo "ERROR: timed out waiting for port-forward" >&2; cat "$PF_LOG" >&2; exit 1; }

PREDICT_PATH="/v1/models/${NAME}:predict"

echo "==> POST http://127.0.0.1:${LOCAL_PORT}${PREDICT_PATH}"
echo "    Host: ${HOST}"
echo "    body: ${JSON_FILE}"

# How many predictor pods are up right now — this is what tells you whether the
# number you are about to read is a cold start or a warm request.
REPLICAS_BEFORE="$(kc get pods -n "$NS" \
  -l "serving.kserve.io/inferenceservice=${NAME}" \
  --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')"
echo "    running predictor pods before request: ${REPLICAS_BEFORE}"
if [[ "$REPLICAS_BEFORE" == "0" ]]; then
  echo "    => scaled to zero, so this request pays a COLD START"
fi
echo

BODY_FILE="$(mktemp -t curl-isvc-body)"
TIMING_FILE="$(mktemp -t curl-isvc-timing)"

set +e
curl -sS \
  -X POST \
  -H "Host: ${HOST}" \
  -H "Content-Type: application/json" \
  --data-binary "@${JSON_FILE}" \
  --max-time "$MAX_TIME" \
  -o "$BODY_FILE" \
  -w '%{http_code} %{time_total} %{time_connect} %{time_starttransfer}\n' \
  "http://127.0.0.1:${LOCAL_PORT}${PREDICT_PATH}" >"$TIMING_FILE"
CURL_RC=$?
set -e

if [[ $CURL_RC -ne 0 ]]; then
  echo "ERROR: curl failed (exit ${CURL_RC})" >&2
  [[ -s "$BODY_FILE" ]] && cat "$BODY_FILE" >&2
  rm -f "$BODY_FILE" "$TIMING_FILE"
  exit 1
fi

# `|| true` is required, not defensive: `read` returns non-zero when it consumes the
# final line of a file with no trailing newline — it still assigns the variables,
# but under `set -e` that non-zero status silently kills the script right here.
# (The -w format above now ends in \n, so this is belt and braces.)
read -r HTTP_CODE TIME_TOTAL TIME_CONNECT TIME_TTFB < "$TIMING_FILE" || true

echo "HTTP ${HTTP_CODE}"
echo "response: $(cat "$BODY_FILE")"
echo
printf 'timing:  total %ss   connect %ss   time-to-first-byte %ss\n' \
  "$TIME_TOTAL" "$TIME_CONNECT" "$TIME_TTFB"

REPLICAS_AFTER="$(kc get pods -n "$NS" \
  -l "serving.kserve.io/inferenceservice=${NAME}" \
  --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')"
echo "         running predictor pods after request: ${REPLICAS_AFTER}"

rm -f "$BODY_FILE" "$TIMING_FILE"

# Make the exit status useful in a test pipeline.
[[ "$HTTP_CODE" == "200" ]] || { echo "FAIL: expected HTTP 200" >&2; exit 1; }
echo
echo "PASS: HTTP 200 from ${NS}/${NAME}"
