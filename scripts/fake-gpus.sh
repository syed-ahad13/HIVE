#!/usr/bin/env bash
#
# fake-gpus.sh — advertise a fake `nvidia.com/gpu` extended resource on the workers.
#
# -----------------------------------------------------------------------------
# (a) WHAT THIS STANDS IN FOR
# -----------------------------------------------------------------------------
# This is the LOCAL stand-in for the NVIDIA GPU Operator.
#
# In production, HIVE runs the NVIDIA GPU Operator together with DRA (Dynamic
# Resource Allocation) on real GPU hardware. There are no GPUs on a MacBook M2
# Pro, so locally we hand-advertise an extended resource on each worker node
# using Kubernetes' documented node-status PATCH mechanism for extended
# resources ("Advertise Extended Resources for a Node").
#
# What matters is that the REQUEST SURFACE is byte-identical either way. A
# tenant asks for a GPU with exactly this, locally and in production:
#
#     resources:
#       limits:
#         nvidia.com/gpu: "1"
#
# so tenant manifests migrate to the official platform unchanged. That identity
# is the whole point of the substitution (CLAUDE.md invariant §2.3).
#
# There is deliberately NO DEVICE PLUGIN here, and there must never be one.
# A device plugin would need real hardware to discover; more importantly, the
# invariant fixes the request surface, not the mechanism behind it. Extended
# resources patched onto node capacity give the scheduler exactly the same
# arithmetic — it counts requests against capacity and refuses to overcommit —
# without pretending any hardware exists. Nothing is ever isolated or executed
# on a "GPU" locally; only the scheduling accounting is real.
#
# -----------------------------------------------------------------------------
# (b) THIS IS NOT PERMANENT — RE-RUN AFTER ANY KUBELET RESTART
# -----------------------------------------------------------------------------
# Capacity patched onto node status lives in the API server's copy of the Node
# object, but the kubelet is the authority for its own node status. When a
# kubelet restarts it republishes status from what it can actually observe —
# and it observes no GPUs — so the patch is WIPED.
#
# In practice that means the fake GPUs disappear after:
#   * restarting Docker Desktop
#   * restarting or recreating the kind cluster
#   * restarting a node container (`docker restart hive-host-worker`)
#   * any kubelet crash or upgrade
#
# The symptom is pods stuck Pending with
# "0/3 nodes are available: Insufficient nvidia.com/gpu".
#
# Just re-run this script. It is idempotent: a JSON Patch `add` on a member that
# already exists replaces its value (RFC 6902 §4.1), so re-running restores the
# advertised capacity without duplicating anything or erroring.
#
# Usage:  scripts/fake-gpus.sh
# Env:    CLUSTER_NAME (default hive-host), GPUS_PER_WORKER (default 4)
# -----------------------------------------------------------------------------
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"
GPUS_PER_WORKER="${GPUS_PER_WORKER:-4}"

# The resource name as tenants write it, and the same name as a JSON Pointer.
# In a JSON Pointer, "/" is a path separator, so a literal "/" inside a key must
# be escaped as "~1" (RFC 6901 §3). Hence nvidia.com/gpu -> nvidia.com~1gpu.
GPU_RESOURCE="nvidia.com/gpu"
GPU_POINTER="nvidia.com~1gpu"

GPU_POOL_LABEL_KEY="hive.ksl/gpu-pool"
GPU_POOL_LABEL_VALUE="shared"

kc() { kubectl --context "$KCTX" "$@"; }

# --- kubectl proxy lifecycle -------------------------------------------------
# The node *status* subresource is patched over raw HTTP, so we need an
# authenticated endpoint. `kubectl proxy` provides one on localhost using the
# credentials already in ~/.kube/config, which avoids handling tokens or CA
# certs here.

PROXY_PID=""
PROXY_LOG=""

cleanup() {
  if [[ -n "$PROXY_PID" ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
  [[ -n "$PROXY_LOG" && -f "$PROXY_LOG" ]] && rm -f "$PROXY_LOG"
  return 0
}
# Fires on normal exit, on error (set -e), and on Ctrl-C, so the proxy is never
# left running in the background.
trap cleanup EXIT INT TERM

start_proxy() {
  PROXY_LOG="$(mktemp -t fake-gpus-proxy)"
  # --port=0 tells kubectl to bind a random FREE port and print it. Asking the
  # kernel for a free port is race-free, unlike picking one and hoping.
  #
  # NOTE: invoke `kubectl` directly here, NOT the kc() helper. Backgrounding a
  # shell function makes $! the PID of the subshell wrapping it, not of kubectl
  # itself — so the trap would kill the wrapper and orphan the real proxy,
  # leaving a listener behind on every run.
  kubectl --context "$KCTX" proxy --port=0 >"$PROXY_LOG" 2>&1 &
  PROXY_PID=$!

  local i port=""
  for ((i = 0; i < 100; i++)); do
    # Expected line: "Starting to serve on 127.0.0.1:54321"
    port="$(sed -n 's/.*127\.0\.0\.1:\([0-9][0-9]*\).*/\1/p' "$PROXY_LOG" | head -1)"
    [[ -n "$port" ]] && break
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
      echo "ERROR: kubectl proxy exited before it was ready:" >&2
      cat "$PROXY_LOG" >&2
      exit 1
    fi
    sleep 0.1
  done

  if [[ -z "$port" ]]; then
    echo "ERROR: timed out waiting for kubectl proxy to report its port" >&2
    cat "$PROXY_LOG" >&2
    exit 1
  fi

  PROXY_PORT="$port"
  echo "==> kubectl proxy listening on 127.0.0.1:${PROXY_PORT} (pid ${PROXY_PID})"
}

# --- helpers -----------------------------------------------------------------

# Print one indented block of `kubectl describe node` output, e.g. "Capacity"
# or "Allocatable". Section headers start at column 0; their contents are
# indented, so a line starting with a non-space ends the block.
node_describe_section() {
  local node="$1" section="$2"
  kc describe node "$node" | awk -v sec="^${section}:" '
    $0 ~ sec  { inblock = 1; next }
    inblock && /^[^[:space:]]/ { inblock = 0 }
    inblock   { print }
  '
}

# True when `kubectl describe node <node>` lists the GPU resource in <section>.
describe_shows_gpu() {
  node_describe_section "$1" "$2" | grep -q "${GPU_RESOURCE}"
}

# Read a single resource quantity straight from the Node object.
node_resource() {
  local node="$1" field="$2" # field: capacity | allocatable
  kc get node "$node" -o "jsonpath={.status.${field}.nvidia\\.com/gpu}"
}

# --- 1. discover workers -----------------------------------------------------

# Read into an array with a while-read loop rather than `mapfile`: macOS ships
# bash 3.2, where `mapfile` does not exist.
WORKERS=()
while IFS= read -r node_name; do
  [[ -n "$node_name" ]] && WORKERS+=("$node_name")
done < <(
  kc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep worker || true
)

if [[ ${#WORKERS[@]} -eq 0 ]]; then
  echo "ERROR: no nodes with 'worker' in the name found in cluster '${CLUSTER_NAME}'" >&2
  exit 1
fi

echo "==> workers: ${WORKERS[*]}"

# --- 2. patch node status ----------------------------------------------------

start_proxy

PATCH_BODY="[{\"op\":\"add\",\"path\":\"/status/capacity/${GPU_POINTER}\",\"value\":\"${GPUS_PER_WORKER}\"}]"

for node in "${WORKERS[@]}"; do
  echo "==> patching ${node}: ${GPU_RESOURCE}=${GPUS_PER_WORKER}"
  # The /status subresource is what carries capacity; patching /api/v1/nodes/<n>
  # instead would silently not update it.
  curl -sS --fail-with-body \
    -X PATCH \
    -H "Content-Type: application/json-patch+json" \
    --data "$PATCH_BODY" \
    "http://127.0.0.1:${PROXY_PORT}/api/v1/nodes/${node}/status" \
    -o /dev/null
done

# --- 3. label the workers ----------------------------------------------------
# --overwrite keeps this idempotent; without it, a second run fails because the
# label already exists.

for node in "${WORKERS[@]}"; do
  kc label node "$node" "${GPU_POOL_LABEL_KEY}=${GPU_POOL_LABEL_VALUE}" --overwrite >/dev/null
  echo "==> labelled ${node} ${GPU_POOL_LABEL_KEY}=${GPU_POOL_LABEL_VALUE}"
done

# --- 4. wait until it is really visible --------------------------------------
# Capacity is patched directly, but Allocatable is DERIVED — the node status
# machinery recomputes it (capacity minus any reservations) a moment later. The
# scheduler subtracts pod requests from Allocatable, not Capacity, so the
# resource is not actually usable until it appears there too. Poll for both.

echo "==> waiting for ${GPU_RESOURCE} in Capacity and Allocatable on all workers"

DEADLINE=$((SECONDS + 120))
while :; do
  all_ready=1
  for node in "${WORKERS[@]}"; do
    if ! describe_shows_gpu "$node" "Capacity" || ! describe_shows_gpu "$node" "Allocatable"; then
      all_ready=0
      break
    fi
  done
  if [[ $all_ready -eq 1 ]]; then
    break
  fi

  if [[ $SECONDS -ge $DEADLINE ]]; then
    echo "ERROR: timed out after 120s waiting for ${GPU_RESOURCE} to appear on all workers" >&2
    for node in "${WORKERS[@]}"; do
      echo "--- ${node} ---" >&2
      node_describe_section "$node" "Capacity" >&2
      node_describe_section "$node" "Allocatable" >&2
    done
    exit 1
  fi
  sleep 2
done

# --- 5. report ---------------------------------------------------------------

echo
printf '%-26s | %-8s | %-11s\n' "NODE" "CAPACITY" "ALLOCATABLE"
printf '%-26s-+-%-8s-+-%-11s\n' "$(printf '%.0s-' {1..26})" "$(printf '%.0s-' {1..8})" "$(printf '%.0s-' {1..11})"
for node in "${WORKERS[@]}"; do
  printf '%-26s | %-8s | %-11s\n' \
    "$node" \
    "$(node_resource "$node" capacity)" \
    "$(node_resource "$node" allocatable)"
done
echo
echo "==> done. ${GPU_RESOURCE} advertised on ${#WORKERS[@]} worker(s)."
echo "    Re-run this script after any kubelet restart (see header)."
