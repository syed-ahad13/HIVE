#!/usr/bin/env bash
#
# sync-catalog.sh — FALLBACK A: publish the host's ClusterServingRuntime catalogue
#                   into each tenant vCluster, read-only by convention.
#
# ===========================================================================
# WHY THIS SCRIPT EXISTS
# ===========================================================================
# The intended mechanism is vCluster's `sync.fromHost.customResources`, which syncs
# host custom resources read-only into the virtual cluster. That is a vCluster PRO
# feature ('Generic Sync'). On open-source v0.36.1 the control-plane pod refuses to
# start and logs:
#
#   register to host syncer clusterservingruntimes.serving.kserve.io: you are trying
#   to use a vCluster pro feature 'Generic Sync' (vcp-distro-generic-sync) that is
#   not available in the open source vcluster or disabled in your license.
#
# So this script does the same job with kubectl. See templates/t-ao-values.yaml for
# the intended configuration (kept, commented out) and DEVIATIONS.md for the log.
#
# ===========================================================================
# WHAT "READ-ONLY BY CONVENTION" MEANS, AND WHAT IT DOES NOT
# ===========================================================================
# Real fromHost sync is read-only BY CONSTRUCTION — the syncer owns the objects and
# reverts tenant edits. This script cannot offer that. It marks each copy with
#
#     hive.ksl/managed-by: host-catalog-sync
#     hive.ksl/read-only:  "true"
#
# plus an annotation saying edits will be overwritten. A determined tenant with
# cluster-admin inside their own vCluster CAN still edit these objects; the next run
# of this script simply overwrites them. That is strictly weaker than the real thing
# and is recorded honestly in DEVIATIONS.md as such.
#
# What it DOES preserve is the architectural shape: the catalogue is authored on the
# host and merely visible to tenants. No tenant can change what any other tenant's
# `modelFormat: sklearn` resolves to, because the host copy is the only source.
#
# Usage:
#   scripts/sync-catalog.sh                 # both tenants
#   scripts/sync-catalog.sh inf-101         # one tenant
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"

# tenant-name:host-namespace
TENANTS_DEFAULT=("inf-101:p-inf-101" "inf-103:p-inf-103")

CRD_NAME="clusterservingruntimes.serving.kserve.io"

kc() { kubectl --context "$KCTX" "$@"; }

TMPDIR_SELF="$(mktemp -d -t sync-catalog)"
cleanup() { rm -rf "$TMPDIR_SELF"; }
trap cleanup EXIT INT TERM

# Obtain a reusable kubeconfig for a vCluster once, rather than paying ~2s of
# `vcluster connect` setup on every kubectl call.
vc_kubeconfig() {
  local name="$1" ns="$2" out="$3"
  vcluster connect "$name" -n "$ns" --print > "$out" 2>/dev/null
  KUBECONFIG="$out" kubectl get ns >/dev/null 2>&1 \
    || { echo "ERROR: cannot reach vCluster ${name} in ${ns}" >&2; return 1; }
}

sync_one() {
  local name="$1" ns="$2"
  local vkc="${TMPDIR_SELF}/kc-${name}.yaml"

  echo "==> ${name} (host namespace ${ns})"
  vc_kubeconfig "$name" "$ns" "$vkc"

  # 1. The CRD itself. Real fromHost sync copies the definition automatically; here
  #    we must do it, or the tenant's API server has no such kind. --server-side
  #    because KServe CRDs are far too large for a client-side apply annotation.
  echo "    copying CRD ${CRD_NAME}"
  kc get crd "$CRD_NAME" -o json \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
# Strip server-populated fields so the object is applyable elsewhere.
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "selfLink"):
    d["metadata"].pop(k, None)
d["metadata"].setdefault("labels", {})["hive.ksl/managed-by"] = "host-catalog-sync"
d.pop("status", None)
json.dump(d, sys.stdout)
' > "${TMPDIR_SELF}/crd.json"
  KUBECONFIG="$vkc" kubectl apply --server-side --force-conflicts -f "${TMPDIR_SELF}/crd.json" >/dev/null

  # 2. The catalogue objects.
  local count
  count="$(kc get clusterservingruntimes -o json \
    | python3 -c '
import json, sys
src = json.load(sys.stdin)
out = []
for item in src.get("items", []):
    m = item["metadata"]
    for k in ("resourceVersion", "uid", "creationTimestamp", "generation",
              "managedFields", "selfLink", "ownerReferences"):
        m.pop(k, None)
    m.setdefault("labels", {})
    m["labels"]["hive.ksl/managed-by"] = "host-catalog-sync"
    m["labels"]["hive.ksl/read-only"] = "true"
    m.setdefault("annotations", {})
    m["annotations"]["hive.ksl/source"] = "host cluster hive-host"
    m["annotations"]["hive.ksl/notice"] = (
        "Read-only catalogue entry owned by the HIVE host. Local edits are "
        "overwritten by scripts/sync-catalog.sh. Stand-in for vCluster Pro "
        "sync.fromHost.customResources."
    )
    # status is host-owned and meaningless in the virtual cluster.
    item.pop("status", None)
    out.append(item)
json.dump({"apiVersion": "v1", "kind": "List", "items": out}, sys.stdout)
print(len(out), file=sys.stderr)
' 2> "${TMPDIR_SELF}/count" > "${TMPDIR_SELF}/catalog.json"; cat "${TMPDIR_SELF}/count")"

  echo "    applying ${count} ClusterServingRuntime objects"
  KUBECONFIG="$vkc" kubectl apply --server-side --force-conflicts -f "${TMPDIR_SELF}/catalog.json" >/dev/null

  local seen
  seen="$(KUBECONFIG="$vkc" kubectl get clusterservingruntimes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  echo "    visible inside ${name}: ${seen}"
  [[ "$seen" == "$count" ]] || { echo "ERROR: expected ${count}, saw ${seen}" >&2; return 1; }
}

main() {
  local -a tenants
  if [[ $# -gt 0 ]]; then
    tenants=()
    for t in "$@"; do
      case "$t" in
        inf-101) tenants+=("inf-101:p-inf-101") ;;
        inf-103) tenants+=("inf-103:p-inf-103") ;;
        *) echo "ERROR: unknown tenant '${t}' (expected inf-101 or inf-103)" >&2; exit 2 ;;
      esac
    done
  else
    tenants=("${TENANTS_DEFAULT[@]}")
  fi

  for entry in "${tenants[@]}"; do
    sync_one "${entry%%:*}" "${entry##*:}"
  done
  echo
  echo "==> catalogue synced. Re-run after installing or changing host ClusterServingRuntimes."
}

main "$@"
