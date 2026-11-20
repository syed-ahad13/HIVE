#!/usr/bin/env bash
# Sanity test: pod networking and cluster DNS work on hive-host.
#
# This is the smoke test for the Calico CNI install (CLAUDE.md §2.5 depends on a
# CNI that actually programs the dataplane). It runs a throwaway busybox pod,
# resolves the in-cluster Service `kubernetes.default`, and cleans up after itself.
#
# Usage: tests/test-pod-networking.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"
POD="netcheck-$$"

# busybox:1.28 publishes linux/arm64 (this is macOS arm64 — no emulation).
#
# Pinned to 1.28 deliberately: busybox 1.29+ dropped `search`-path handling in its
# applet nslookup, so a short name like `kubernetes.default` returns NXDOMAIN there
# even on a perfectly healthy cluster (only the FQDN resolves). 1.28 is the tag the
# upstream Kubernetes DNS-debugging docs use for this reason. This is a quirk of the
# test image, not of the cluster.
IMAGE="busybox:1.28"

cleanup() {
  kubectl --context "$KCTX" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> running $POD ($IMAGE) and resolving kubernetes.default"
kubectl --context "$KCTX" run "$POD" \
  --image="$IMAGE" \
  --restart=Never \
  --attach \
  --rm \
  --quiet \
  --command -- nslookup kubernetes.default

echo "==> OK: pod networking and cluster DNS are working"
