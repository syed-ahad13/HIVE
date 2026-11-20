#!/usr/bin/env bash
#
# sync-isvc.sh — FALLBACK B: syncer stand-in pending a vCluster Pro licence.
#
#   Mirrors InferenceServices authored INSIDE each tenant vCluster into that
#   tenant's HOST namespace, where the one central KServe reconciles them, and
#   mirrors the resulting status back into the virtual object.
#
# ===========================================================================
# WHY THIS SCRIPT EXISTS
# ===========================================================================
# The intended mechanism is vCluster's `sync.toHost.customResources`, which is a
# vCluster PRO feature ('Generic Sync'). On open-source v0.36.1 the control-plane pod
# refuses to start and logs:
#
#   register to host syncer inferenceservices.serving.kserve.io: you are trying to
#   use a vCluster pro feature 'Generic Sync' (vcp-distro-generic-sync) that is not
#   available in the open source vcluster or disabled in your license.
#
# See templates/t-ao-values.yaml for the intended config (kept, commented out) and
# DEVIATIONS.md, where this is logged as "syncer stand-in pending vcluster fix".
#
# ===========================================================================
# THE ARCHITECTURE IS PRESERVED; THE MECHANISM IS NOT
# ===========================================================================
# What is preserved — and this is the part that must survive to production:
#   * the tenant AUTHORS an InferenceService inside their own cluster, using the
#     ordinary KServe API and the ordinary GPU request surface (§2.3);
#   * the HOST RECONCILES it with the ONE central KServe (§2.2);
#   * the tenant sees readiness and the URL without any host access.
#
# What is NOT equivalent to a real syncer, stated plainly:
#   * it POLLS on an interval rather than watching, so propagation is delayed by up
#     to one interval in each direction;
#   * status arrives as ANNOTATIONS on the virtual object, not in .status, because
#     writing a foreign controller's status subresource from a script is not
#     something a tenant's kubectl would ever see as authentic;
#   * the virtual object is not validated by KServe's admission webhook (that webhook
#     lives on the host), so a malformed spec is rejected late — when the host mirror
#     is applied — rather than at the tenant's kubectl;
#   * if this script is not running, nothing is mirrored. A real syncer lives inside
#     the vCluster control plane and shares its lifecycle.
#
# Usage:
#   scripts/sync-isvc.sh --once              # one reconcile pass over both tenants
#   scripts/sync-isvc.sh                     # loop forever (default 10s interval)
#   scripts/sync-isvc.sh --once inf-101      # one tenant
#   INTERVAL=5 scripts/sync-isvc.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"
INTERVAL="${INTERVAL:-10}"

TENANTS_DEFAULT=("inf-101:p-inf-101" "inf-103:p-inf-103")
ISVC_CRD="inferenceservices.serving.kserve.io"

kc() { kubectl --context "$KCTX" "$@"; }

TMPDIR_SELF="$(mktemp -d -t sync-isvc)"
cleanup() { rm -rf "$TMPDIR_SELF"; }
trap cleanup EXIT INT TERM

ONCE=0
declare -a TENANTS=()
for arg in "$@"; do
  case "$arg" in
    --once) ONCE=1 ;;
    inf-101) TENANTS+=("inf-101:p-inf-101") ;;
    inf-103) TENANTS+=("inf-103:p-inf-103") ;;
    *) echo "usage: $(basename "$0") [--once] [inf-101|inf-103]..." >&2; exit 2 ;;
  esac
done
[[ ${#TENANTS[@]} -eq 0 ]] && TENANTS=("${TENANTS_DEFAULT[@]}")

vc_kubeconfig() {
  local name="$1" ns="$2" out="$3"
  vcluster connect "$name" -n "$ns" --print > "$out" 2>/dev/null
  KUBECONFIG="$out" kubectl get ns >/dev/null 2>&1 \
    || { echo "ERROR: cannot reach vCluster ${name} in ${ns}" >&2; return 1; }
}

# The tenant cannot author an InferenceService unless their API server knows the
# kind. Real toHost sync copies the CRD automatically; here we do it, once, and it is
# idempotent. --server-side is mandatory: the KServe isvc CRD is far larger than the
# 262144-byte client-side apply annotation limit.
ensure_isvc_crd() {
  local vkc="$1" name="$2"
  if KUBECONFIG="$vkc" kubectl get crd "$ISVC_CRD" >/dev/null 2>&1; then return 0; fi
  echo "    bootstrapping CRD ${ISVC_CRD} inside ${name}"
  kc get crd "$ISVC_CRD" -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("resourceVersion","uid","creationTimestamp","generation","managedFields","selfLink"):
    d["metadata"].pop(k, None)
d["metadata"].setdefault("labels", {})["hive.ksl/managed-by"] = "sync-isvc"
d.pop("status", None)
json.dump(d, sys.stdout)
' > "${TMPDIR_SELF}/isvc-crd.json"
  KUBECONFIG="$vkc" kubectl apply --server-side --force-conflicts -f "${TMPDIR_SELF}/isvc-crd.json" >/dev/null
}

# The reconcile logic lives in Python: it is a three-way diff (virtual desired, host
# actual, host orphans) and expressing that in shell would be less clear, not more.
cat > "${TMPDIR_SELF}/mirror.py" <<'PYEOF'
import json, subprocess, sys, datetime

HOST_CTX, TENANT, HOST_NS, VKC = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

MANAGED_BY = "sync-isvc"
L_MANAGED  = "hive.ksl/managed-by"
L_VCLUSTER = "hive.ksl/vcluster"
L_VNS      = "hive.ksl/virtual-namespace"
L_VNAME    = "hive.ksl/virtual-name"


def host(*args, **kw):
    return subprocess.run(["kubectl", "--context", HOST_CTX, *args],
                          capture_output=True, text=True, **kw)


def virt(*args, **kw):
    return subprocess.run(["kubectl", "--kubeconfig", VKC, *args],
                          capture_output=True, text=True, **kw)


def mirrored_name(vns, vname):
    # Mirrors vCluster's own translation scheme (<name>-x-<namespace>-x-<vcluster>),
    # so a host-side reader can tell at a glance where an object came from.
    return f"{vname}-x-{vns}-x-{TENANT}"


# ---- 1. what the tenant wants ------------------------------------------------
r = virt("get", "inferenceservices", "-A", "-o", "json")
if r.returncode != 0:
    # No CRD yet, or nothing authored. Not an error.
    virtual_items = []
else:
    virtual_items = json.loads(r.stdout).get("items", [])

desired = {}
for item in virtual_items:
    m = item["metadata"]
    vns, vname = m.get("namespace", "default"), m["name"]
    hname = mirrored_name(vns, vname)
    desired[hname] = (vns, vname, item)

# ---- 2. push each desired object into the host namespace ---------------------
for hname, (vns, vname, item) in sorted(desired.items()):
    body = {
        "apiVersion": item["apiVersion"],
        "kind": item["kind"],
        "metadata": {
            "name": hname,
            "namespace": HOST_NS,
            "labels": {
                **{k: v for k, v in (item["metadata"].get("labels") or {}).items()},
                L_MANAGED: MANAGED_BY,
                L_VCLUSTER: TENANT,
                L_VNS: vns,
                L_VNAME: vname,
            },
            "annotations": {
                **{k: v for k, v in (item["metadata"].get("annotations") or {}).items()
                   if not k.startswith("kubectl.kubernetes.io/")
                   and not k.startswith("hive.ksl/host-")},
                "hive.ksl/mirrored-from": f"{TENANT}:{vns}/{vname}",
                "hive.ksl/mirror-notice": (
                    "Mirrored by scripts/sync-isvc.sh — stand-in for vCluster Pro "
                    "sync.toHost.customResources. Do not edit on the host; the "
                    "tenant's virtual object is the source of truth."
                ),
            },
        },
        # SPEC ONLY. Status is the host KServe controller's to write.
        "spec": item["spec"],
    }
    r = host("apply", "-f", "-", input=json.dumps(body))
    verb = "unchanged"
    if r.returncode != 0:
        print(f"    ERROR applying {HOST_NS}/{hname}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.stdout.strip()}")
        continue
    out = r.stdout.strip()
    if out.endswith("created"):
        verb = "created"
    elif out.endswith("configured"):
        verb = "updated"
    print(f"    host {HOST_NS}/{hname}: {verb}")

# ---- 3. garbage collect host mirrors whose virtual source is gone ------------
r = host("get", "inferenceservices", "-n", HOST_NS, "-l",
         f"{L_MANAGED}={MANAGED_BY},{L_VCLUSTER}={TENANT}", "-o", "json")
host_items = json.loads(r.stdout).get("items", []) if r.returncode == 0 else []
for item in host_items:
    hname = item["metadata"]["name"]
    if hname not in desired:
        host("delete", "inferenceservice", hname, "-n", HOST_NS, "--wait=false")
        print(f"    host {HOST_NS}/{hname}: DELETED (virtual source gone)")

# ---- 4. mirror host status back into the virtual object ---------------------
# As annotations, not .status — see the script header for why.
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
for hname, (vns, vname, _item) in sorted(desired.items()):
    r = host("get", "inferenceservice", hname, "-n", HOST_NS, "-o", "json")
    if r.returncode != 0:
        continue
    st = json.loads(r.stdout).get("status", {}) or {}
    ready = "Unknown"
    for c in st.get("conditions", []) or []:
        if c.get("type") == "Ready":
            ready = c.get("status", "Unknown")
    url = st.get("url", "")

    ann = [
        f"hive.ksl/host-name={hname}",
        f"hive.ksl/host-namespace={HOST_NS}",
        f"hive.ksl/host-ready={ready}",
        f"hive.ksl/host-url={url}",
        f"hive.ksl/host-status-synced-at={now}",
    ]
    virt("annotate", "inferenceservice", vname, "-n", vns, "--overwrite", *ann)
    print(f"    virtual {vns}/{vname}: ready={ready} url={url or '<none>'}")
PYEOF

reconcile() {
  local name="$1" ns="$2"
  local vkc="${TMPDIR_SELF}/kc-${name}.yaml"
  echo "==> ${name} (host namespace ${ns})"
  vc_kubeconfig "$name" "$ns" "$vkc" || return 1
  ensure_isvc_crd "$vkc" "$name"
  python3 "${TMPDIR_SELF}/mirror.py" "$KCTX" "$name" "$ns" "$vkc"
}

if [[ $ONCE -eq 1 ]]; then
  for entry in "${TENANTS[@]}"; do reconcile "${entry%%:*}" "${entry##*:}"; done
else
  echo "==> syncing every ${INTERVAL}s; Ctrl-C to stop"
  while :; do
    for entry in "${TENANTS[@]}"; do reconcile "${entry%%:*}" "${entry##*:}" || true; done
    sleep "$INTERVAL"
  done
fi
