#!/usr/bin/env bash
#
# Verifies CLAUDE.md invariant §2.3: a pod requesting
# `resources.limits["nvidia.com/gpu"]` schedules onto a worker and is charged
# against that worker's allocatable GPUs.
#
# This proves the SCHEDULER ARITHMETIC works against the fake extended resource
# advertised by scripts/fake-gpus.sh. It does not — and cannot — prove anything
# about GPU execution: there is no GPU on this machine.
#
# Prerequisite: scripts/fake-gpus.sh has been run since the last kubelet restart.
#
# Usage: tests/test-gpu-scheduling.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"
MANIFEST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gpu-scheduling-check.yaml"
POD="gpu-scheduling-check"

kc() { kubectl --context "$KCTX" "$@"; }

cleanup() {
  echo
  echo "==> cleaning up: deleting pod/${POD}"
  kc delete -f "$MANIFEST" --ignore-not-found --wait=true >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Start from a clean slate so a leftover pod from an interrupted run cannot
# make this test appear to pass.
kc delete -f "$MANIFEST" --ignore-not-found --wait=true >/dev/null 2>&1 || true

echo "==> advertised GPU capacity before scheduling"
kc get nodes -o custom-columns='NODE:.metadata.name,CAPACITY:.status.capacity.nvidia\.com/gpu,ALLOCATABLE:.status.allocatable.nvidia\.com/gpu'

echo
echo "==> applying ${MANIFEST##*/} (requests limits.nvidia.com/gpu: 1)"
kc apply -f "$MANIFEST"

echo
echo "==> waiting for the pod to be Running"
if ! kc wait --for=condition=Ready "pod/${POD}" --timeout=120s; then
  echo
  echo "ERROR: pod did not become Ready. Scheduler said:" >&2
  kc describe "pod/${POD}" | sed -n '/^Events:/,$p' >&2
  echo >&2
  echo "If this says 'Insufficient nvidia.com/gpu', the node status patch was" >&2
  echo "wiped by a kubelet restart — re-run scripts/fake-gpus.sh." >&2
  exit 1
fi

NODE="$(kc get "pod/${POD}" -o jsonpath='{.spec.nodeName}')"
PHASE="$(kc get "pod/${POD}" -o jsonpath='{.status.phase}')"

echo
echo "==> pod is ${PHASE} on node ${NODE}"

case "$NODE" in
  *worker*) echo "==> OK: landed on a worker, as required (control-plane advertises no GPUs)" ;;
  *) echo "ERROR: pod landed on '${NODE}', which is not a worker" >&2; exit 1 ;;
esac

echo
echo "==> 'Allocated resources' on ${NODE}, from kubectl describe node:"
# The "Allocated resources" block is what the scheduler actually reasons about:
# the sum of requests from pods already placed on this node.
kc describe node "$NODE" | awk '
  /^Allocated resources:/ { inblock = 1 }
  inblock                 { print "    " $0 }
  inblock && /nvidia\.com\/gpu/ { found = 1 }
  inblock && /^Events:/   { exit }
  END { if (!found) exit 1 }
' || {
  echo "ERROR: nvidia.com/gpu did not appear in Allocated resources on ${NODE}" >&2
  exit 1
}

echo
echo "==> PASS: the GPU request surface schedules and is charged correctly."
