#!/usr/bin/env bash
# Create the HIVE-LOCAL host cluster `hive-host` and install Calico as its CNI.
#
# Reproduces steps 2-4 of HIVE-LOCAL step 1 and is safe to re-run: cluster creation
# is skipped if the cluster already exists, and every manifest is applied with
# `kubectl apply`, which is declarative and converges rather than duplicating.
#
# Everything this script installs is a file committed in this repo (CLAUDE.md §5:
# no imperative one-offs outside scripts/).
#
# Usage: scripts/create-cluster.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"

KIND_CONFIG="${REPO_ROOT}/host/kind-config.yaml"
CNI_DIR="${REPO_ROOT}/host/cni"

kc() { kubectl --context "$KCTX" "$@"; }

# ---------------------------------------------------------------------------
# Step 2: the kind cluster (1 control-plane + 2 workers, default CNI disabled)
# ---------------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "==> kind cluster '${CLUSTER_NAME}' already exists — skipping creation"
else
  echo "==> creating kind cluster '${CLUSTER_NAME}'"
  kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG"
fi

# ---------------------------------------------------------------------------
# Step 3: Calico via the Tigera operator
#
# Three manifests, not two: as of Calico v3.30 the operator's CRDs are shipped in
# their own operator-crds.yaml and are no longer bundled inside tigera-operator.yaml.
#
# --server-side is required, not cosmetic: operator-crds.yaml is ~3 MB, which
# overflows the 262144-byte `kubectl.kubernetes.io/last-applied-configuration`
# annotation that a client-side apply would try to write.
# ---------------------------------------------------------------------------
echo "==> applying Calico operator CRDs"
kc apply --server-side -f "${CNI_DIR}/operator-crds.yaml" >/dev/null

echo "==> applying Tigera operator"
kc apply --server-side -f "${CNI_DIR}/tigera-operator.yaml"

echo "==> waiting for the Tigera operator to be Ready"
kc wait --for=condition=Available deploy/tigera-operator -n tigera-operator --timeout=300s

echo "==> applying Calico Installation + APIServer CRs"
kc apply -f "${CNI_DIR}/custom-resources.yaml"

# ---------------------------------------------------------------------------
# Step 4: converge — Calico pods Running, all 3 nodes Ready, coredns Running
#
# coredns sits Pending until a CNI works, so it is the last thing to come up and
# the clearest signal that the dataplane is actually programmed.
# ---------------------------------------------------------------------------
echo "==> waiting for the calico-system namespace to appear"
for _ in $(seq 1 60); do
  kc get ns calico-system >/dev/null 2>&1 && break
  sleep 5
done

echo "==> waiting for Calico to report Available (tigerastatus)"
# `tigerastatus` is the operator's own rollup: apiserver, calico, ippools, tiers.
for name in calico apiserver ippools tiers; do
  kc wait --for=condition=Available "tigerastatus/${name}" --timeout=600s || true
done

echo "==> waiting for all calico-system pods to be Ready"
kc wait --for=condition=Ready pods --all -n calico-system --timeout=600s

echo "==> waiting for all 3 nodes to be Ready"
kc wait --for=condition=Ready nodes --all --timeout=300s

echo "==> waiting for coredns to be Ready"
kc wait --for=condition=Ready pods -l k8s-app=kube-dns -n kube-system --timeout=300s

echo
echo "==> cluster '${CLUSTER_NAME}' is up"
kc get nodes -o wide
echo
kc get pods -A
