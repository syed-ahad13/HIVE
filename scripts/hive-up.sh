#!/usr/bin/env bash
#
# hive-up.sh — bring the whole of HIVE-LOCAL up, in REAL mode, from cold.
#
#   Usage:
#     scripts/hive-up.sh                  # everything: platform + tenants + console
#     scripts/hive-up.sh --no-studio      # platform only, no console
#     scripts/hive-up.sh --skip-serving   # fast re-run after a Docker restart
#     scripts/hive-up.sh --help
#
# ===========================================================================
# WHAT THIS IS
# ===========================================================================
# The single entry point that replaces the hand-typed bring-up sequence in
# README.md ("Bring-up from cold", plus the by-hand steps 5-6 and the acts that
# no script recorded at all). It runs the platform the way it is meant to be
# run: REAL, end to end, against the host cluster.
#
# THERE IS NO MOCK PATH HERE. Studio's `mock` executor exists so the console can
# be demonstrated with Docker stopped; it is not a way to run HIVE. This script
# exports STUDIO_EXECUTOR=real and REFUSES to start if the environment asks for
# anything else, because a bring-up script that could silently produce a console
# creating nothing is worse than no script (see PHASE 9).
#
# ===========================================================================
# PHASES
# ===========================================================================
#   0  preflight     binaries, Docker, repo files, and the mode assertion
#   1  base image    kindest/node into the host Docker        (pull-images.sh)
#   2  host cluster  kind `hive-host` + Calico                (create-cluster.sh)
#   3  fake GPUs     nvidia.com/gpu on the workers            (fake-gpus.sh)
#   4  images        every workload image onto every node     (pull-images.sh)
#   5  serving       cert-manager -> Knative + Kourier -> the ONE KServe
#   6  kubecontext   kind-hive-host — Studio provisions nowhere else
#   7  tenancy       host namespaces, quotas, LimitRanges, PVCs, seed Jobs
#   8  vClusters     one control plane per tenant
#   9  tenant API    the catalogue + the KServe CRDs, inside each vCluster
#  10  workloads     each tenant's InferenceService, authored in its own cluster
#  11  console       HIVE Studio, STUDIO_EXECUTOR=real
#
# PHASES 1 AND 4 EXIST BECAUSE AN IMAGE CANNOT BE FETCHED WHERE IT IS NEEDED
# BEFORE THAT PLACE EXISTS. `kind create` reads the node image from the host
# Docker daemon, so phase 1 has to precede the cluster; every other image has to
# reach a node's containerd, which does not exist until the cluster does. Phase 4
# then has to precede the serving install, or `kubectl wait` spends its timeout
# watching pods that are only slowly pulling.
#
# Every phase is idempotent: re-running converges rather than duplicating. The
# script creates nothing imperatively that is not already wrapped by a committed
# script or a committed manifest — it is the scripts/ carve-out from the
# CLAUDE.md §5 working agreements, one level up.
#
# ===========================================================================
# WHAT IT DELIBERATELY DOES NOT DO
# ===========================================================================
#   * It never deletes the kind cluster, a p-inf-* namespace, or a vCluster
#     (§5: ask first). --skip-serving is as destructive as it gets.
#   * It does not install the §2.5 NetworkPolicies or the §2.6 expiry CronJob,
#     because neither exists in this repo yet. When host/reconciler/ lands, it
#     gets a phase here.
#   * It does not restore your previous kubecontext on exit. Phase 4 switches it
#     and says so: the console and every kubectl you type afterwards must agree
#     on which cluster they are talking to.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"

# tenant-name:host-namespace:isvc-manifest — the two hand-built reference
# tenants. Studio-provisioned projects are NOT in this list and must never be:
# Studio owns their lifecycle, and this script only ever reads them.
TENANTS=(
  "inf-101:p-inf-101:tenants/inf-101/legal-translation.yaml"
  "inf-103:p-inf-103:tenants/inf-103/species-classifier.yaml"
)

ISVC_CRD="inferenceservices.serving.kserve.io"

DO_STUDIO=1
DO_TENANTS=1
DO_IMAGES=1
DO_SYNC_LOOP=1
DO_VERIFY=0
SERVING_MODE="auto"      # auto | skip | force

LOG_DIR="${REPO_ROOT}/tmp"
TMP="$(mktemp -d -t hive-up)"
SYNC_PID=""
STARTED_AT="$(date +%s)"

# ---------------------------------------------------------------------------
# Output helpers. Phases are loud because this script runs for minutes and the
# question you have while watching it is always "which part is slow this time".
# ---------------------------------------------------------------------------
bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; grn=$'\033[32m'; amb=$'\033[33m'; off=$'\033[0m'

phase() { printf '\n%s==> [%s] %s%s\n' "$bold" "$(elapsed)" "$*" "$off"; }
note()  { printf '    %s\n' "$*"; }
warn()  { printf '    %s! %s%s\n' "$amb" "$*" "$off"; }
good()  { printf '    %s%s%s\n' "$grn" "$*" "$off"; }
die()   { printf '\n%serror: %s%s\n' "$red" "$*" "$off" >&2; exit 1; }

elapsed() {
  local s=$(( $(date +%s) - STARTED_AT ))
  printf '%02d:%02d' $(( s / 60 )) $(( s % 60 ))
}

cleanup() {
  local rc=$?
  if [[ -n "$SYNC_PID" ]] && kill -0 "$SYNC_PID" 2>/dev/null; then
    printf '\n%s==> stopping the InferenceService syncer (pid %s)%s\n' "$bold" "$SYNC_PID" "$off"
    kill "$SYNC_PID" 2>/dev/null || true
    wait "$SYNC_PID" 2>/dev/null || true
    # Said plainly because it is the one thing that stops working the moment
    # this script exits, and nothing in the cluster will report it: the syncer
    # is a stand-in for a vCluster Pro feature and lives only as this process.
    warn "nothing is mirroring tenant InferenceServices any more."
    warn "re-run scripts/sync-isvc.sh (or scripts/hive-up.sh) to resume it."
  fi
  rm -rf "$TMP"
  return $rc
}
trap cleanup EXIT
trap 'exit 130' INT TERM

kc() { kubectl --context "$KCTX" "$@"; }

usage() {
  sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'
Flags:
  --no-studio       bring the platform up but do not start the console
  --no-tenants      skip the two hand-built reference tenants (inf-101, inf-103)
  --no-sync         run one mirror pass instead of leaving the syncer running
  --skip-images     assume every container image is already pulled and loaded
  --skip-serving    assume cert-manager/Knative/KServe are installed
  --force-serving   re-apply the serving stack even if it looks healthy
  --verify          send a real prediction through inf-101 before the console
  -h, --help        this

Env:
  CLUSTER_NAME (hive-host)  GPUS_PER_WORKER (4)  INTERVAL (10, syncer period)
  API_PORT (8000)           UI_PORT (5173)

The executor is always `real`. STUDIO_EXECUTOR=mock is rejected, not honoured.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-studio)     DO_STUDIO=0 ;;
    --skip-images)   DO_IMAGES=0 ;;
    --no-tenants)    DO_TENANTS=0 ;;
    --no-sync)       DO_SYNC_LOOP=0 ;;
    --skip-serving)  SERVING_MODE="skip" ;;
    --force-serving) SERVING_MODE="force" ;;
    --verify)        DO_VERIFY=1 ;;
    -h|--help)       usage; exit 0 ;;
    *)               die "unknown flag '$1' (try --help)" ;;
  esac
  shift
done

# ===========================================================================
# PHASE 0 — preflight
# ===========================================================================
phase "phase 0/11  preflight"

# The mode assertion comes FIRST, before anything slow. Getting five minutes
# into a bring-up and then discovering the console will be a mock is exactly the
# failure this script exists to remove.
if [[ -n "${STUDIO_EXECUTOR:-}" && "${STUDIO_EXECUTOR}" != "real" ]]; then
  die "STUDIO_EXECUTOR is '${STUDIO_EXECUTOR}', and this script only runs real mode.
       Unset it, or run scripts/studio-up.sh directly if a mock console is what you want."
fi
export STUDIO_EXECUTOR=real

if [[ -n "${STUDIO_FAIL_STEP:-}" ]]; then
  # Failure injection is a feature of the failure-path proof, not of a bring-up.
  # Unset rather than honoured: a deliberately broken pipeline in a script whose
  # whole promise is "the platform is up" would be indistinguishable from a bug.
  warn "STUDIO_FAIL_STEP='${STUDIO_FAIL_STEP}' — unset for this run (failure injection is off)"
  unset STUDIO_FAIL_STEP
fi

for bin in docker kind kubectl vcluster curl python3; do
  command -v "$bin" >/dev/null 2>&1 || die "'$bin' is not on PATH — see README.md prerequisites"
done
if [[ $DO_STUDIO -eq 1 ]]; then
  for bin in node npm; do
    command -v "$bin" >/dev/null 2>&1 || die "'$bin' is not on PATH (needed for the console; --no-studio skips it)"
  done
fi

docker info >/dev/null 2>&1 || die "the Docker daemon is not responding — start Docker Desktop first"

# ---------------------------------------------------------------------------
# THE DOCKER VM'S MEMORY — THE RESOURCE THAT ACTUALLY RUNS OUT
# ---------------------------------------------------------------------------
# Every tenant is a whole Kubernetes control plane. Measured on 2026-09-29, the
# platform alone holds ~4GiB of pods before any Studio project exists: the host
# kube-apiserver ~1.2GiB, Calico ~0.9GiB, the two reference vClusters ~0.55GiB
# each, Knative + KServe ~0.4GiB. Each Studio project then adds another vCluster
# (~0.55GiB) plus its workload — a RAG project is ~1.1GiB in all.
#
# In a 7.7GiB VM that fits two or three projects, and the failure past that
# point is NOT an out-of-memory error anyone would recognise. The VM swaps, etcd
# requests stretch to seconds, kube-controller-manager and kube-scheduler miss
# their 10-second lease renewals and crash-loop — and then provisioning "times
# out at vcluster" and deleting "times out on the namespace", with nothing in
# either message pointing at memory. See docs/control-plane-recovery.md.
#
# A warning, not a refusal: a lighter workload can live in less. Studio's own
# `approve` step independently refuses to provision while the control plane is
# unhealthy, which is the check that actually protects a deployment.
MIN_DOCKER_GIB=10
docker_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
docker_gib=$(( docker_bytes / 1073741824 ))
if (( docker_gib < MIN_DOCKER_GIB )); then
  warn "Docker Desktop's VM has ${docker_gib}GiB of memory; HIVE-LOCAL needs ${MIN_DOCKER_GIB}GiB or more."
  warn "  Below that the VM swaps, the host control plane crash-loops, and Studio"
  warn "  deploys and deletes time out. Raise it in Docker Desktop -> Settings ->"
  warn "  Resources -> Memory (then re-run this script). See docs/control-plane-recovery.md"
fi

# ---------------------------------------------------------------------------
# ANYTHING ELSE IN THE SAME VM IS COMPETING FOR IT
# ---------------------------------------------------------------------------
# The memory check above assumes HIVE has the VM to itself, and on 2026-09-29 it
# did not. A second kind cluster (`gtl-control-plane`) and a docker-compose
# stack came back with Docker's own restart — kind nodes are `on-failure` and
# compose services `unless-stopped`, so they return uninvited. The other cluster
# alone took 214% CPU and 1.7GiB, more than all three HIVE nodes together, and
# its control plane was crash-looping for the same reason HIVE's was. A new
# project then failed with no other HIVE project running at all.
#
# Named, not stopped: those are someone else's projects, and whether they may
# be paused is not this script's decision.
other_kind="$(kind get clusters 2>/dev/null | grep -vx "${CLUSTER_NAME:-hive-host}" | tr '\n' ' ')"
other_running="$(docker ps --format '{{.Names}}' | grep -vE "^(${CLUSTER_NAME:-hive-host}-|vcluster_)" | tr '\n' ' ')"
if [[ -n "${other_running// /}" ]]; then
  warn "Other containers share this Docker VM and compete with HIVE for its CPU and memory:"
  [[ -n "${other_kind// /}" ]] && warn "  other kind clusters: ${other_kind}"
  warn "  ${other_running}"
  warn "  If deploys time out, pause them first:  docker stop <name>   (docker start <name> to resume)"
fi

for f in host/kind-config.yaml host/images.txt scripts/create-cluster.sh \
         scripts/pull-images.sh scripts/fake-gpus.sh scripts/install-serving.sh \
         scripts/sync-catalog.sh scripts/sync-isvc.sh; do
  [[ -e "$f" ]] || die "missing $f — is this the hive-local repo?"
done

mkdir -p "$LOG_DIR"
good "docker up, tools present, executor=real"

# ===========================================================================
# PHASE 1 — the kind node image, into the host Docker daemon
#
# Before the cluster and not with the other images, because `kind create` reads
# this one from the HOST daemon — there are no nodes to pull onto yet. It is
# pinned BY DIGEST in VERSIONS.md and that exact reference is what gets pulled,
# so a bring-up cannot quietly install a different Kubernetes than the one the
# gate reports in docs/ were written against.
# ===========================================================================
phase "phase 1/11  kind node image"
if [[ $DO_IMAGES -eq 0 ]]; then
  warn "--skip-images: assuming the node image is already in the host Docker cache"
else
  CLUSTER_NAME="$CLUSTER_NAME" scripts/pull-images.sh --node-image
fi

# ===========================================================================
# PHASE 2 — the host cluster
# ===========================================================================
phase "phase 2/11  host cluster '${CLUSTER_NAME}' + Calico"
CLUSTER_NAME="$CLUSTER_NAME" scripts/create-cluster.sh

# ===========================================================================
# PHASE 3 — fake GPUs
#
# ALWAYS re-run, never conditionally. Extended resources patched onto node
# status are republished by the kubelet from what it can observe — and it
# observes no GPUs — so every Docker Desktop restart wipes them. The symptom is
# pods Pending with "Insufficient nvidia.com/gpu", which does not point at the
# cause. Re-patching costs a second.
# ===========================================================================
phase "phase 3/11  fake nvidia.com/gpu on the workers"
CLUSTER_NAME="$CLUSTER_NAME" scripts/fake-gpus.sh

# ===========================================================================
# PHASE 4 — every workload image, onto every node
#
# BEFORE the serving install, deliberately. install-serving.sh ends in `kubectl
# wait` on the cert-manager, Knative and KServe rollouts, and a pod that is still
# pulling a few hundred megabytes looks exactly like a pod that is broken — same
# Pending, same silence, for minutes. Pulling first turns that into a progress
# list, and on a re-run into a no-op.
#
# scripts/pull-images.sh explains why there have to be two mechanisms: tag-pinned
# images are served from the host Docker cache by `kind load` and need no network
# after the first time; digest-pinned ones must be pulled by the node's own
# containerd, because `docker save` rebuilds the manifest and the digest a pod
# asks for would no longer match.
# ===========================================================================
phase "phase 4/11  container images"
if [[ $DO_IMAGES -eq 0 ]]; then
  warn "--skip-images: pulling and loading nothing"
else
  CLUSTER_NAME="$CLUSTER_NAME" scripts/pull-images.sh --nodes
  # Calico's dataplane images and the vCluster control-plane images are chosen by
  # an operator and a Helm chart, so they are named in no file here and cannot be
  # pre-pulled from a list. They are already on the nodes by now — the kubelet
  # fetched them during phase 2 — and --record is how they get INTO the list, for
  # the next rebuild. Not run automatically: it edits a file that is committed.
  note "to pre-pull Calico + vCluster images next time: scripts/pull-images.sh --record"
fi

# ===========================================================================
# PHASE 5 — the ONE central serving stack (CLAUDE.md §2.2)
#
# install-serving.sh is idempotent but re-applies ~8 MB of vendored YAML and
# waits on several rollouts, so `auto` skips it when the three controllers that
# matter are already Available. That check is about SAVING TIME, not about
# correctness: --force-serving re-converges, and after changing anything under
# host/serving/ you want it.
# ===========================================================================
serving_healthy() {
  kc get deploy kserve-controller-manager -n kserve \
     -o jsonpath='{.status.availableReplicas}' 2>/dev/null | grep -q '^[1-9]' || return 1
  kc get deploy controller -n knative-serving \
     -o jsonpath='{.status.availableReplicas}' 2>/dev/null | grep -q '^[1-9]' || return 1
  kc get deploy net-kourier-controller -n knative-serving \
     -o jsonpath='{.status.availableReplicas}' 2>/dev/null | grep -q '^[1-9]' || return 1
}

phase "phase 5/11  cert-manager -> Knative + Kourier -> the ONE KServe"
case "$SERVING_MODE" in
  skip)
    warn "--skip-serving: not applying host/serving/. Verifying what is there instead."
    serving_healthy || die "the serving stack is not healthy, and --skip-serving told me not to install it.
       Re-run without --skip-serving."
    good "cert-manager / Knative / Kourier / KServe all report Available"
    ;;
  force)
    CLUSTER_NAME="$CLUSTER_NAME" scripts/install-serving.sh
    ;;
  auto)
    if serving_healthy; then
      good "already installed and Available — skipping (--force-serving re-applies)"
    else
      CLUSTER_NAME="$CLUSTER_NAME" scripts/install-serving.sh
    fi
    ;;
esac

# ===========================================================================
# PHASE 6 — the kubecontext
#
# Studio's real executor checks `kubectl config current-context` against
# kind-hive-host and refuses to provision anywhere else (app/main.py,
# app/executor_real.py). That check is a safety property, so this script
# satisfies it rather than working around it — and says what it changed, because
# silently repointing someone's kubectl is not a thing a script should do
# quietly.
# ===========================================================================
phase "phase 6/11  kubecontext"
PREV_CTX="$(kubectl config current-context 2>/dev/null || true)"
if [[ "$PREV_CTX" == "$KCTX" ]]; then
  good "already ${KCTX}"
else
  kubectl config use-context "$KCTX" >/dev/null
  warn "switched your current context: ${PREV_CTX:-<none>} -> ${KCTX} (not restored on exit)"
fi

if [[ $DO_TENANTS -eq 0 ]]; then
  warn "--no-tenants: skipping phases 7-10 (inf-101 and inf-103 are left as they are)"
else

# ===========================================================================
# PHASE 7 — host-side tenancy: the ceiling (CLAUDE.md §2.4)
#
# The seed Jobs are handled apart from the rest of the directory, and not for
# tidiness: a Job's spec is largely immutable once created, so `kubectl apply`
# over a Job that has already Completed fails on a field it cannot change. The
# namespace, LimitRange, ResourceQuota and PVCs all converge happily; the Job is
# create-once, and re-running is supposed to be free.
# ===========================================================================
phase "phase 7/11  tenant host namespaces, quotas, LimitRanges, PVCs"
for entry in "${TENANTS[@]}"; do
  name="${entry%%:*}"; rest="${entry#*:}"; ns="${rest%%:*}"
  note "${name} -> ${ns}"
  for f in host/tenancy/"${name}"/*.yaml; do
    [[ "$(basename "$f")" == 40-seed-models-job.yaml ]] && continue
    kc apply -f "$f" >/dev/null
  done

  job_file="host/tenancy/${name}/40-seed-models-job.yaml"
  # The Job's own name, read out of the manifest rather than assumed, so
  # renaming it in the file cannot silently turn this phase into a no-op. awk and
  # not a heredoc inside $( ): macOS ships bash 3.2, which cannot parse that.
  job_name="$(awk '/^kind: Job$/ { seen = 1 } seen && /^  name:/ { print $2; exit }' "$job_file")"
  if [[ -z "$job_name" ]]; then
    die "could not read the Job name out of ${job_file}"
  elif kc get job "$job_name" -n "$ns" >/dev/null 2>&1; then
    note "  job/${job_name} already exists — leaving it alone"
  else
    note "  creating job/${job_name} (downloads the model once)"
    kc apply -f "$job_file" >/dev/null
  fi
  note "  waiting for job/${job_name} to complete"
  kc wait --for=condition=complete "job/${job_name}" -n "$ns" --timeout=600s >/dev/null \
    || die "job/${job_name} in ${ns} did not complete. Its own log has the reason:
       kubectl logs job/${job_name} -n ${ns}"
done
good "both tenants have a quota, a LimitRange and a seeded models PVC"

# ===========================================================================
# PHASE 8 — one control plane per tenant (CLAUDE.md §1)
#
# The one imperative call in the whole bring-up, and unavoidable: a vCluster is
# not created by applying a manifest. Both flags are mandatory —
#   --connect=false  do not hijack the kubecontext phase 6 just fixed
#   --add=false      MANDATORY offline; the default registers with vCluster
#                    Platform and aborts when not logged in (VERSIONS.md, step 6)
#
# Existence is decided by the control-plane StatefulSet, not by `vcluster list`:
# one is a Kubernetes object with a stable name, the other is output whose shape
# has changed across vcluster releases.
# ===========================================================================
phase "phase 8/11  tenant vClusters"
for entry in "${TENANTS[@]}"; do
  name="${entry%%:*}"; rest="${entry#*:}"; ns="${rest%%:*}"
  values="templates/t-sz-values.yaml"
  [[ "$name" == "inf-101" ]] && values="templates/t-ao-values.yaml"

  if kc get statefulset "$name" -n "$ns" >/dev/null 2>&1; then
    note "${name}: control plane already exists"
  else
    note "${name}: creating with ${values}"
    vcluster create "$name" --namespace "$ns" -f "$values" --connect=false --add=false
  fi

  note "${name}: waiting for the control plane to be ready"
  ready=""
  for _ in $(seq 1 60); do
    ready="$(kc get statefulset "$name" -n "$ns" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    [[ "$ready" == "1" ]] && break
    sleep 5
  done
  if [[ "$ready" != "1" ]]; then
    # Worth knowing from step 6: when the ResourceQuota blocks it, NO pod is
    # created at all, so `kubectl get pods` is empty and the only explanation
    # lives in the StatefulSet's events.
    kc get events -n "$ns" --sort-by=.lastTimestamp | tail -15
    die "the ${name} vCluster control plane did not become ready in 300s (events above)"
  fi
done
good "inf-101 and inf-103 each have their own Kubernetes API"

# ===========================================================================
# PHASE 9 — the KServe API inside each tenant cluster
#
# A FRESH vCLUSTER HAS NO KSERVE AT ALL, so a tenant cannot author an
# InferenceService in it: `kubectl apply` fails with "resource mapping not
# found". Two halves, and only one of them was ever scripted:
#
#   * the ClusterServingRuntime CRD + catalogue — scripts/sync-catalog.sh
#     (Fallback A);
#   * the InferenceService CRD — NOTHING. During step 6 it was applied by hand
#     and no file recorded it, which is precisely why a cold rebuild used to
#     produce two vClusters that could not be used. It is copied here.
#
# Both are stand-ins for vCluster's sync.fromHost.customResources, a Pro
# feature; DEVIATIONS.md carries the entry.
# ===========================================================================
phase "phase 9/11  publishing the KServe API into each vCluster"
scripts/sync-catalog.sh

strip_crd() {
  # Strip the server-populated fields so a host CRD is applyable elsewhere.
  #
  # `python3 -c`, NOT `python3 - <<EOF`: the CRD arrives on stdin, and a heredoc
  # would hand the interpreter the heredoc as its stdin instead of the JSON —
  # which fails as a JSON parse error on the first line of the program.
  python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation",
          "managedFields", "selfLink"):
    d["metadata"].pop(k, None)
d["metadata"].setdefault("labels", {})["hive.ksl/managed-by"] = "hive-up"
d.pop("status", None)
json.dump(d, sys.stdout)
'
}

for entry in "${TENANTS[@]}"; do
  name="${entry%%:*}"; rest="${entry#*:}"; ns="${rest%%:*}"
  vkc="${TMP}/kc-${name}.yaml"
  vcluster connect "$name" -n "$ns" --print > "$vkc" 2>/dev/null \
    || die "could not get a kubeconfig for the ${name} vCluster"
  KUBECONFIG="$vkc" kubectl get ns >/dev/null 2>&1 \
    || die "the ${name} vCluster is not answering its own API"

  note "${name}: copying CRD ${ISVC_CRD}"
  # --server-side is not optional: the InferenceService CRD is ~700 KB, far past
  # the 262144-byte last-applied-configuration annotation a client-side apply
  # would try to write.
  kc get crd "$ISVC_CRD" -o json | strip_crd > "${TMP}/isvc-crd.json"
  KUBECONFIG="$vkc" kubectl apply --server-side --force-conflicts -f "${TMP}/isvc-crd.json" >/dev/null

  # POLLED, not `kubectl wait --for=condition=established`. In the gap between
  # storing a CRD and the API server registering the type, that command does not
  # wait — it FAILS, with ".status.conditions accessor error: <nil> is of the
  # type <nil>", because the status subresource does not exist yet. Treating "no
  # status" as "not ready" is what was meant all along.
  est=""
  for _ in $(seq 1 60); do
    est="$(KUBECONFIG="$vkc" kubectl get crd "$ISVC_CRD" \
      -o 'jsonpath={.status.conditions[?(@.type=="Established")].status}' 2>/dev/null || true)"
    [[ "$est" == "True" ]] && break
    sleep 2
  done
  [[ "$est" == "True" ]] || die "${ISVC_CRD} never became Established inside ${name}"
  note "${name}: InferenceService API established"
done
good "both tenants can author InferenceServices in their own cluster"

# ===========================================================================
# PHASE 10 — the workloads, authored INSIDE each tenant cluster
#
# Applied through the tenant's own kubeconfig, never to the host, because that
# is the whole architectural claim: the PI authors in their own control plane
# (§1) and the ONE central KServe reconciles it (§2.2). The mirror that carries
# it to the host is Fallback B.
# ===========================================================================
phase "phase 10/11  tenant InferenceServices + the host mirror"
for entry in "${TENANTS[@]}"; do
  name="${entry%%:*}"; rest="${entry#*:}"; manifest="${rest#*:}"
  vkc="${TMP}/kc-${name}.yaml"
  note "${name}: applying $(basename "$manifest") inside the vCluster"
  KUBECONFIG="$vkc" kubectl apply -f "$manifest" >/dev/null
done

note "one mirror pass now, so the host has something to reconcile"
scripts/sync-isvc.sh --once

if [[ $DO_SYNC_LOOP -eq 1 ]]; then
  SYNC_LOG="${LOG_DIR}/sync-isvc.log"
  # Backgrounded and supervised for the lifetime of THIS script. A real syncer
  # lives inside the vCluster control plane and shares its lifecycle; this one
  # is a polling stand-in, so while it is not running, nothing propagates in
  # either direction — tenant edits do not reach the host and status does not
  # come back. Leaving it running is the closest honest approximation.
  scripts/sync-isvc.sh >"$SYNC_LOG" 2>&1 &
  SYNC_PID=$!
  sleep 2
  kill -0 "$SYNC_PID" 2>/dev/null || die "the syncer exited immediately — see ${SYNC_LOG}"
  good "syncer running (pid ${SYNC_PID}, every ${INTERVAL:-10}s, log: tmp/sync-isvc.log)"
else
  warn "--no-sync: one pass only. Nothing will propagate after this."
fi

fi  # DO_TENANTS

# ===========================================================================
# OPTIONAL — prove it with a real prediction
# ===========================================================================
if [[ $DO_VERIFY -eq 1 ]]; then
  phase "verify  a real prediction through inf-101"
  if [[ $DO_TENANTS -eq 0 ]]; then
    warn "--verify needs the tenants; --no-tenants was given. Skipping."
  else
    # The mirrored host name, not the virtual one: the host is where the ONE
    # KServe reconciles it and where Kourier has a Route. ~15 s cold.
    note "waiting for the mirrored InferenceService to report Ready"
    kc wait --for=condition=Ready \
      inferenceservice/legal-translation-x-default-x-inf-101 -n p-inf-101 --timeout=300s \
      || die "legal-translation never became Ready on the host. Check:
       kubectl get isvc -n p-inf-101
       kubectl get events -n p-inf-101 --sort-by=.lastTimestamp | tail -20"
    scripts/curl-isvc.sh p-inf-101 legal-translation-x-default-x-inf-101 tests/iris-input.json
  fi
fi

# ===========================================================================
# PHASE 11 — the console, in real mode
#
# Delegated to scripts/studio-up.sh rather than reimplemented, so there is one
# place that knows how to start the BFF and the dev server. But studio-up.sh
# REUSES a backend already listening on :8000 and reports the mode that backend
# is actually in — which is right for it and wrong here. So the check happens
# BEFORE handing over: if a mock backend is already running, this script stops,
# rather than proceeding to a console that would create nothing while sitting on
# top of a cluster that is fully real. That is the one confusion this whole
# script exists to make impossible.
# ===========================================================================
if [[ $DO_STUDIO -eq 0 ]]; then
  phase "phase 11/11  console — skipped (--no-studio)"
  note "when you want it:  STUDIO_EXECUTOR=real scripts/studio-up.sh"
else
  phase "phase 11/11  HIVE Studio (executor=real)"

  if health="$(curl -sf "http://127.0.0.1:${API_PORT}/api/health" 2>/dev/null)"; then
    live_mode="$(printf '%s' "$health" | python3 -c \
      'import json,sys; print(json.load(sys.stdin).get("executor","?"))' 2>/dev/null || echo '?')"
    if [[ "$live_mode" != "real" ]]; then
      die "a backend is already running on :${API_PORT} with executor='${live_mode}'.
       This script only runs real mode, and studio-up.sh would reuse that process.
       Stop it and re-run:  lsof -ti :${API_PORT} | xargs kill"
    fi
    note "reusing the real backend already on :${API_PORT}"
  fi

  # Bootstrap the two machine-local, gitignored dependency trees. Both are built
  # from pinned manifests committed in the repo, so this creates no new choice.
  #
  # THE INTERPRETER IS CHOSEN EXPLICITLY, NOT INHERITED FROM `python3`.
  # A Mac has several: Apple's Command Line Tools 3.9 at
  # /Library/Developer/CommandLineTools/usr/bin, Homebrew's, sometimes conda's.
  # Which one `python3` means depends on PATH order and on zsh's command hash,
  # and building the venv on the wrong one fails LATE and confusingly: pip reports
  # "Ignored the following versions that require a different python version" and
  # "no matching distribution for fastapi==0.141.1", which reads like a bad pin
  # rather than a 3.9 interpreter. 3.10 is the real floor — every backend module
  # has `from __future__ import annotations`, but Pydantic v2 EVALUATES those
  # annotations to build each model, and `str | None` cannot be evaluated on 3.9.
  VENV="${REPO_ROOT}/studio/backend/.venv"
  pyver_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

  if [[ -d "$VENV" ]] && ! pyver_ok "${VENV}/bin/python"; then
    die "the venv at ${VENV} is Python $("${VENV}/bin/python" -V 2>&1 | awk '{print $2}'), which
       cannot install the pinned dependencies. Remove it and let this script rebuild it:
         rm -rf studio/backend/.venv"
  fi

  if [[ ! -x "${VENV}/bin/uvicorn" ]]; then
    PY=""
    for candidate in "${PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3; do
      [[ -n "$candidate" ]] || continue
      if command -v "$candidate" >/dev/null 2>&1 && pyver_ok "$candidate"; then PY="$candidate"; break; fi
    done
    [[ -n "$PY" ]] || die "no Python >= 3.11 on PATH (found: $(python3 -V 2>&1)).
       brew install python@3.14, or point this at one:  PYTHON=/path/to/python3 $0"

    note "creating the backend venv with $(command -v "$PY") ($("$PY" -V 2>&1))"
    "$PY" -m venv "$VENV"
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet -r studio/backend/requirements.txt \
      || die "pip install failed (network?). The venv is at ${VENV} if you want to retry by hand."
  fi
  if [[ ! -d "${REPO_ROOT}/studio/frontend/node_modules" ]]; then
    note "installing frontend dependencies (npm ci, from the committed lockfile)"
    ( cd studio/frontend && npm ci --silent ) \
      || die "npm ci failed (network?). Retry: (cd studio/frontend && npm ci)"
  fi

  printf '\n'
  note "cluster is up. Ctrl+C stops the console, the backend and the syncer."
  # Foreground, NOT exec: the trap above still has a syncer to take down.
  STUDIO_EXECUTOR=real API_PORT="$API_PORT" UI_PORT="$UI_PORT" scripts/studio-up.sh

  # studio-up.sh holds the foreground until the console is stopped, so reaching
  # this line means it returned and there is nothing left to bring up. Exit here
  # rather than falling into the --no-studio summary below, which would print
  # "done" and a list of next steps directly after a shutdown.
  phase "console stopped after $(elapsed)"
  exit 0
fi

# ===========================================================================
# Reached only with --no-studio (otherwise the console holds the foreground).
# ===========================================================================
phase "done in $(elapsed)"
kc get nodes -o wide
printf '\n'
kc get isvc -A 2>/dev/null || true
cat <<SUMMARY

  next:
    scripts/curl-isvc.sh p-inf-101 legal-translation-x-default-x-inf-101 tests/iris-input.json
    STUDIO_EXECUTOR=real scripts/studio-up.sh          # the console, real mode
SUMMARY
if [[ -n "$SYNC_PID" ]]; then
  printf '\n'
  warn "the syncer stops when this script exits — see the note below."
fi
