#!/usr/bin/env bash
#
# pull-images.sh — fetch every container image HIVE-LOCAL runs, from the project
#                  that publishes it, and make it available to the kind nodes.
#
#   Usage:
#     scripts/pull-images.sh                 # pull + make available to the nodes
#     scripts/pull-images.sh --refresh       # regenerate host/images.txt (no network)
#     scripts/pull-images.sh --list          # show the plan, pull nothing
#     scripts/pull-images.sh --node-image    # just the kindest/node image (host Docker)
#     scripts/pull-images.sh --record        # add what the live cluster is running
#
# ===========================================================================
# WHERE THE IMAGE LIST COMES FROM — AND WHY IT IS NOT TYPED BY HAND
# ===========================================================================
# Every reference in host/images.txt is EXTRACTED by `--refresh` from a file
# already committed in this repo:
#
#   kindest/node          the VERSIONS.md row that recorded it at install time,
#                         including the digest
#   Tigera operator       host/cni/tigera-operator.yaml
#   cert-manager          host/serving/cert-manager/cert-manager.yaml
#   Knative + Kourier     host/serving/knative/{serving-core,kourier}.yaml
#   KServe + Envoy        host/serving/kserve/kserve.yaml, kourier.yaml
#   sklearn runtime       host/serving/kserve/kserve-cluster-resources.yaml
#   model-seed Job        host/tenancy/*/40-seed-models-job.yaml
#
# Nobody retypes a tag, and nobody invents one. A hand-written image list is a
# second, divergent source of truth that drifts from the manifests the moment
# either changes — and a wrong tag here would silently install a version nothing
# in VERSIONS.md records.
#
# THE ALLOW-LIST BELOW IS THE OTHER HALF: this script refuses to fetch from any
# registry that is not the upstream publisher of the component in question. If a
# --refresh or a --record ever produces a reference outside it, the run STOPS and
# names the reference rather than pulling it.
#
# ===========================================================================
# WHAT IS DELIBERATELY *NOT* PULLED
# ===========================================================================
# 13 of KServe's 14 ClusterServingRuntimes are CATALOGUE ENTRIES, not workloads:
# nothing in this repo selects them. Several are tens of gigabytes (vllm,
# tritonserver, llm-d-cuda) and two — `seldonio/mlserver` and
# `kserve/huggingfaceserver` — are amd64-only. Pulling them would cost hours and
# disk for images no pod will ever reference.
#
# The one that IS pulled is `kserve/sklearnserver`, and that is not a guess: both
# tenants and the smoke canary use `modelFormat: sklearn` with the default
# `protocolVersion: v1`, which makes the higher-priority amd64-only
# `kserve-mlserver` ineligible and selects `kserve-sklearnserver` deterministically
# — verified at S1 (docs/S1-spike.md §"kserve-container: index.docker.io/
# kserve/sklearnserver@sha256:...") and explained in host/serving/smoke/sklearn-iris.yaml.
#
# ===========================================================================
# TWO MECHANISMS, AND THE REASON THERE HAS TO BE TWO
# ===========================================================================
#   TAG-PINNED images   ->  `docker pull` on the host, then `kind load docker-image`
#   DIGEST-PINNED images ->  `crictl pull` inside each node
#
# This is not a preference. `kind load` works by `docker save` | `ctr images
# import`, and save/import RE-BUILDS the image manifest, so the manifest digest
# changes. A pod asking for `gcr.io/knative-releases/...@sha256:abc` would then
# not match what is on the node, and the kubelet would pull from the registry
# anyway. A digest reference can only be satisfied by a containerd that fetched
# that exact manifest — which means pulling on the node itself.
#
# The consequence, stated plainly: the five digest-pinned Knative images are
# re-fetched from the network every time the kind cluster is RECREATED, because a
# new node container is a new containerd. The tag-pinned ones survive in the host
# Docker cache and are loaded offline. Nothing can be done about the first half
# without running a registry mirror, which production has and a laptop does not.
#
# ...AND SOMETIMES ONE, BECAUSE OF A DOCKER DESKTOP SETTING
#
# If Docker Desktop's CONTAINERD IMAGE STORE is enabled, `kind load` cannot work
# for anything at all, and the script pulls everything on the nodes instead. The
# detection and the full explanation are in containerd_image_store() below. The
# cost is only the host cache: correctness is unaffected, and the fast path comes
# back by turning that setting off. `HIVE_LOAD_MODE=kind-load|node-pull` overrides
# the detection.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
IMAGES_FILE="${REPO_ROOT}/host/images.txt"
LOCK_FILE="${REPO_ROOT}/host/images.lock"

TMP="$(mktemp -d -t pull-images)"
trap 'rm -rf "$TMP"' EXIT

bold=$'\033[1m'; red=$'\033[31m'; grn=$'\033[32m'; amb=$'\033[33m'; off=$'\033[0m'
say()  { printf '    %s\n' "$*"; }
head_() { printf '\n%s==> %s%s\n' "$bold" "$*" "$off"; }
good() { printf '    %s%s%s\n' "$grn" "$*" "$off"; }
warn() { printf '    %s! %s%s\n' "$amb" "$*" "$off"; }
die()  { printf '\n%serror: %s%s\n' "$red" "$*" "$off" >&2; exit 1; }

# ---------------------------------------------------------------------------
# THE ALLOW-LIST — the only registries this script will fetch from.
#
# One line per publisher, and each is the project's own registry, not a mirror
# and not a re-publisher. Adding a line is a deliberate act: it means "I have
# checked who controls this namespace".
# ---------------------------------------------------------------------------
ALLOWED_PREFIXES="
docker.io/library/          Docker Official Images (python, for the model-seed Job)
docker.io/kindest/          kind, published by Kubernetes SIGs
docker.io/kserve/           the KServe project
docker.io/envoyproxy/       the Envoy Proxy project
gcr.io/knative-releases/    the Knative project's release registry
quay.io/jetstack/           cert-manager, published by its maintainers
quay.io/brancz/             kube-rbac-proxy, the upstream repo KServe vendors
quay.io/tigera/             the Tigera operator (Calico)
quay.io/calico/             Project Calico
docker.io/loft-sh/          Loft Labs, the vCluster publisher
registry.k8s.io/            the Kubernetes registry (CoreDNS inside a vCluster)
"

# normalize_ref — expand Docker Hub's implicit defaults so every reference is
# fully qualified. `python:3.11-slim` and `kserve/sklearnserver:v0.20.0` are the
# SAME images as their docker.io/... forms; spelling them out is what lets the
# allow-list check mean anything, and crictl wants the long form anyway.
normalize_ref() {
  local ref="$1" first="${1%%/*}"
  case "$ref" in
    */*) # has a path separator: is the first segment a registry host?
      case "$first" in
        *.*|*:*|localhost) printf '%s\n' "$ref" ;;
        *)                 printf 'docker.io/%s\n' "$ref" ;;
      esac ;;
    *)   printf 'docker.io/library/%s\n' "$ref" ;;
  esac
}

assert_allowed() {
  local ref="$1" prefix
  while read -r prefix _; do
    [[ -z "$prefix" ]] && continue
    case "$ref" in "${prefix}"*) return 0 ;; esac
  done <<< "$ALLOWED_PREFIXES"
  die "refusing to fetch '${ref}': its registry is not on the allow-list in
       $(basename "${BASH_SOURCE[0]}"). Nothing was pulled. If this reference is
       legitimate, add its publisher to ALLOWED_PREFIXES deliberately — do not
       widen the list to make an unexpected image go away."
}

# ---------------------------------------------------------------------------
# --refresh — regenerate host/images.txt from the committed manifests
#
# Format is two columns so the file can say WHERE each image has to end up:
#   host   only the host Docker daemon needs it (the kind node image)
#   node   every kind node needs it (everything that runs in the cluster)
# ---------------------------------------------------------------------------
images_from() {   # images_from <file> [...] — every `image:` value, selection-free
  grep -hoE '^[[:space:]]*image:[[:space:]]*\S+' "$@" 2>/dev/null \
    | awk '{print $2}' | tr -d '"' | sort -u
}

refresh() {
  head_ "regenerating $(basename "$IMAGES_FILE") from the committed manifests"
  local out="${TMP}/images.txt"

  {
    echo "# host/images.txt — every container image HIVE-LOCAL runs."
    echo "#"
    echo "# GENERATED by scripts/pull-images.sh --refresh. Do not hand-edit: every"
    echo "# reference below is extracted from a manifest committed in this repo, so"
    echo "# the list cannot drift from what is actually installed. Re-run --refresh"
    echo "# after changing anything under host/serving/, host/cni/ or host/tenancy/."
    echo "#"
    echo "# columns:  <scope>  <image reference>"
    echo "#   host    only the host Docker daemon needs it (kind reads it from there)"
    echo "#   node    every kind node needs it"
    echo "#"
    echo "# Regenerated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo

    echo "# --- the cluster itself: VERSIONS.md, the row written at install time ---"
    local node_image
    node_image="$(sed -n 's/.*`kindest\/node:\([^`]*\)`.*digest `\(sha256:[a-f0-9]\{64\}\)`.*/kindest\/node:\1@\2/p' \
      VERSIONS.md | head -1)"
    [[ -n "$node_image" ]] || die "could not read the kindest/node reference out of VERSIONS.md"
    echo "host  $(normalize_ref "$node_image")"
    echo

    echo "# --- CNI: host/cni/tigera-operator.yaml ---"
    local img
    for img in $(images_from host/cni/tigera-operator.yaml); do
      echo "node  $(normalize_ref "$img")"
    done
    echo "#"
    echo "# The Calico dataplane images (quay.io/calico/*) are chosen by the operator"
    echo "# at runtime, not named in any manifest here, so they CANNOT be extracted."
    echo "# They are added by \`--record\` after the first bring-up that had network,"
    echo "# which reads them from the pods the operator actually created. Guessing the"
    echo "# names would be exactly the drift this file exists to prevent."
    echo

    echo "# --- cert-manager: host/serving/cert-manager/cert-manager.yaml ---"
    for img in $(images_from host/serving/cert-manager/cert-manager.yaml); do
      echo "node  $(normalize_ref "$img")"
    done
    echo

    echo "# --- Knative Serving + Kourier: host/serving/knative/ ---"
    echo "# Digest-pinned upstream: the digest IS the verification, and it is also"
    echo "# why these cannot be \`kind load\`ed (see the script header)."
    echo "#"
    echo "# EXCEPT envoyproxy/envoy:v1.37-latest, which is a FLOATING tag — upstream"
    echo "# Kourier's choice, not ours. It is the only mutable reference in this file:"
    echo "# host/images.lock records the digest that was actually pulled, so a silent"
    echo "# upstream re-push shows up as a diff there instead of as a mystery."
    for img in $(images_from host/serving/knative/serving-core.yaml host/serving/knative/kourier.yaml); do
      echo "node  $(normalize_ref "$img")"
    done
    echo

    echo "# --- KServe: host/serving/kserve/kserve.yaml ---"
    for img in $(images_from host/serving/kserve/kserve.yaml); do
      echo "node  $(normalize_ref "$img")"
    done
    echo

    echo "# --- the ONE serving runtime in use: kserve-cluster-resources.yaml ---"
    echo "# 13 of the 14 ClusterServingRuntimes are catalogue entries no workload"
    echo "# selects; see the script header for why only this one is pulled."
    local sklearn
    sklearn="$(grep -hoE 'kserve/sklearnserver:\S+' host/serving/kserve/kserve-cluster-resources.yaml \
      | sort -u | head -1)"
    [[ -n "$sklearn" ]] || die "could not find the sklearnserver runtime image in kserve-cluster-resources.yaml"
    echo "node  $(normalize_ref "$sklearn")"
    echo

    echo "# --- the model-seed Jobs: host/tenancy/*/40-seed-models-job.yaml ---"
    for img in $(images_from host/tenancy/inf-101/40-seed-models-job.yaml host/tenancy/inf-103/40-seed-models-job.yaml); do
      echo "node  $(normalize_ref "$img")"
    done
    echo
    echo "# --- recorded from the live cluster by --record (appended below) ---"
  } > "$out"

  # Check every generated reference against the allow-list BEFORE the file is
  # written, so an unexpected registry can never end up committed.
  local ref
  while read -r _ ref; do
    [[ -z "${ref:-}" ]] && continue
    assert_allowed "$ref"
  done < <(grep -vE '^\s*(#|$)' "$out")

  # Carry forward anything a previous --record added, so --refresh is not
  # destructive of the one part of this file that cannot be regenerated — but
  # drop any recorded reference the regenerated part above now covers. An image
  # can move from operator-chosen to manifest-named (or be recorded on a machine
  # where a manifest names it), and the same reference listed twice would be
  # pulled twice and written to the lock twice.
  if [[ -f "$IMAGES_FILE" ]]; then
    awk '/^# --- recorded from the live cluster/{found=1; next} found' "$IMAGES_FILE" > "${TMP}/recorded"
    grep -vE '^[[:space:]]*(#|$)' "$out" | awk '{print $2}' | sort -u > "${TMP}/fresh-refs"
    awk 'NR==FNR { have[$0] = 1; next }
         /^[[:space:]]*(#|$)/ { print; next }
         !have[$2] { print }' "${TMP}/fresh-refs" "${TMP}/recorded" >> "$out"
  fi

  mv "$out" "$IMAGES_FILE"
  good "$(grep -cvE '^\s*(#|$)' "$IMAGES_FILE") images listed in host/images.txt"
}

# ---------------------------------------------------------------------------
# reading the list
# ---------------------------------------------------------------------------
require_list() {
  [[ -f "$IMAGES_FILE" ]] || die "no ${IMAGES_FILE}. Generate it: scripts/pull-images.sh --refresh"
}

refs_for_scope() { # refs_for_scope host|node
  grep -vE '^\s*(#|$)' "$IMAGES_FILE" | awk -v s="$1" '$1 == s { print $2 }'
}

list() {
  require_list
  head_ "host/images.txt"
  local ref
  printf '    %-14s %s\n' "SCOPE/HOW" "IMAGE"
  for ref in $(refs_for_scope host); do printf '    %-14s %s\n' "host docker" "$ref"; done
  for ref in $(refs_for_scope node); do
    case "$ref" in
      *@sha256:*) printf '    %-14s %s\n' "node crictl" "$ref" ;;
      *)          printf '    %-14s %s\n' "kind load" "$ref" ;;
    esac
  done
  echo
  say "$(refs_for_scope node | wc -l | tr -d ' ') node images, $(refs_for_scope host | wc -l | tr -d ' ') host image(s)"
}

# ---------------------------------------------------------------------------
# --node-image — the kind node image, into the HOST Docker daemon
#
# Separate from everything else and it has to run BEFORE the cluster exists:
# `kind create` reads this image from the host daemon, and there are no nodes to
# pull onto yet.
# ---------------------------------------------------------------------------
node_image() {
  require_list
  local ref
  for ref in $(refs_for_scope host); do
    assert_allowed "$ref"
    if docker image inspect "$ref" >/dev/null 2>&1; then
      good "already present: ${ref}"
    else
      head_ "pulling the kind node image (this is the big one, ~1 GB)"
      say "$ref"
      docker pull --platform linux/arm64 "$ref" \
        || die "could not pull ${ref}. It is pinned by digest in VERSIONS.md, so a
       failure here is a network problem, not a wrong tag."
    fi
    assert_arm64 "$ref"
  done
}

assert_arm64() {
  local ref="$1" arch
  arch="$(docker image inspect --format '{{.Architecture}}' "$ref" 2>/dev/null || echo unknown)"
  if [[ "$arch" != "arm64" ]]; then
    # CLAUDE.md §5: relying on Rosetta is allowed, but only if it is LOGGED.
    # This is also usually fatal rather than slow: the kind nodes are arm64
    # containers with no binfmt handler inside them, so an amd64 image typically
    # does not run there at all.
    die "${ref} is ${arch}, not arm64.
       Nothing in HIVE-LOCAL is supposed to need emulation (VERSIONS.md notes).
       If this image is genuinely required, it needs a DEVIATIONS.md row per the
       CLAUDE.md §5 working agreement — and it will probably not run inside a
       kind node at all."
  fi
}

# ---------------------------------------------------------------------------
# the main event — make every `node`-scope image available on every node
# ---------------------------------------------------------------------------
nodes_of_cluster() {
  kind get nodes --name "$CLUSTER_NAME" 2>/dev/null | grep -v '^$' \
    || die "cluster '${CLUSTER_NAME}' has no nodes — create it first (scripts/create-cluster.sh)"
}

# missing_on_node <node> <ref...> — prints the refs that node's containerd lacks.
#
# ONE `crictl images` call per node rather than one `crictl inspecti` per image:
# a `docker exec` costs ~200 ms, and 3 nodes x 16 images of those is half a minute
# of nothing on every single bring-up.
missing_on_node() {
  local node="$1"; shift
  docker exec "$node" crictl images -o json > "${TMP}/imgs-${node}.json" 2>/dev/null || echo '{}' > "${TMP}/imgs-${node}.json"
  python3 -c '
import json, sys
path, refs = sys.argv[1], sys.argv[2:]
try:
    data = json.load(open(path))
except Exception:
    data = {}
tags, digests = set(), set()
for img in data.get("images") or []:
    tags.update(img.get("repoTags") or [])
    digests.update(img.get("repoDigests") or [])
for ref in refs:
    have = ref in digests if "@sha256:" in ref else ref in tags
    if not have:
        print(ref)
' "${TMP}/imgs-${node}.json" "$@"
}

# Is Docker Desktop using the containerd image store?
#
# THIS DECIDES WHETHER `kind load` CAN WORK AT ALL, so it is checked rather than
# discovered halfway through a bring-up. With the containerd store, `docker save`
# emits an OCI archive whose index still lists EVERY platform of the original
# manifest list, while only the platform you pulled is actually in the content
# store. kind then runs `ctr images import --all-platforms`, which walks that
# index and asks for the amd64 blobs, and fails with:
#
#   ctr: content digest sha256:...: not found
#
# Nothing can be passed to kind to stop it: the `--all-platforms` is kind's, and
# `docker save` grew a `--platform` flag that kind does not use. So when the
# containerd store is on, every image is pulled by the node instead.
containerd_image_store() {
  docker info --format '{{json .DriverStatus}}' 2>/dev/null \
    | grep -q 'io.containerd.snapshotter'
}

# node_pull <node> <ref...> — pull straight into that node's containerd.
node_pull() {
  local node="$1"; shift
  local ref
  for ref in "$@"; do
    say "${node}: crictl pull ${ref}"
    docker exec "$node" crictl pull "$ref" >/dev/null \
      || die "crictl pull failed on ${node} for ${ref}.
       This path needs the network — it is the node's own containerd fetching from
       the registry. Nothing else was changed."
  done
}

provision_nodes() {
  require_list
  local all_refs tag_refs digest_refs ref
  all_refs="$(refs_for_scope node)"
  [[ -n "$all_refs" ]] || die "host/images.txt lists no node-scope images"

  for ref in $all_refs; do assert_allowed "$ref"; done

  # HIVE_LOAD_MODE overrides the detection: `node-pull` forces everything through
  # the node, `kind-load` insists on the host-cache path. Auto is right almost
  # always; the override exists so a failure can be bisected without editing this.
  local mode="${HIVE_LOAD_MODE:-auto}"
  if [[ "$mode" == "auto" ]]; then
    if containerd_image_store; then mode="node-pull"; else mode="kind-load"; fi
  fi

  # --- everything through the node, because kind load cannot work here -----
  if [[ "$mode" == "node-pull" ]]; then
    head_ "pulling every image on the nodes themselves"
    warn "Docker Desktop's containerd image store is on, so \`kind load\` cannot be used"
    say  "(see containerd_image_store() above; turning that setting off restores the"
    say  " host-cache path, which survives a cluster rebuild. Nothing else changes.)"
    local node missing
    for node in $(nodes_of_cluster); do
      missing="$(missing_on_node "$node" $all_refs || true)"
      if [[ -z "$missing" ]]; then
        say "${node}: all present"
      else
        node_pull "$node" $missing
      fi
    done
    write_lock
    return 0
  fi

  tag_refs=""; digest_refs=""
  for ref in $all_refs; do
    case "$ref" in
      *@sha256:*) digest_refs="${digest_refs} ${ref}" ;;
      *)          tag_refs="${tag_refs} ${ref}" ;;
    esac
  done

  # --- (1) tag-pinned: host Docker, then kind load -------------------------
  head_ "tag-pinned images: host Docker cache -> kind load"
  local pulled=0
  for ref in $tag_refs; do
    if docker image inspect "$ref" >/dev/null 2>&1; then
      : # already in the host cache — the summary below covers it
    else
      say "pulling: ${ref}"
      docker pull --platform linux/arm64 "$ref" >/dev/null \
        || die "could not pull ${ref}.
       If this is 'no matching manifest for linux/arm64', the image publishes no
       arm64 build — see assert_arm64 above and CLAUDE.md §5."
      pulled=$((pulled + 1))
    fi
    assert_arm64 "$ref"
  done
  if [[ $pulled -eq 0 ]]; then
    good "all $(echo $tag_refs | wc -w | tr -d ' ') tag-pinned images were already in the host Docker cache"
  fi

  # Ask each node ONCE what it has, then invert: one `kind load` per image
  # covering every node that lacks it, rather than one per (node, image). kind
  # does a `docker save` per invocation, so the difference on a cold cache is
  # 13 saves instead of 39 — minutes, on the phase you are most likely to be
  # standing in front of an audience waiting for.
  local nodes node csv loaded=0 fellback=0
  nodes="$(nodes_of_cluster)"
  for node in $nodes; do
    missing_on_node "$node" $tag_refs > "${TMP}/missing-${node}" || true
  done
  for ref in $tag_refs; do
    csv=""
    for node in $nodes; do
      if grep -qxF "$ref" "${TMP}/missing-${node}"; then csv="${csv:+${csv},}${node}"; fi
    done
    [[ -z "$csv" ]] && continue
    say "loading ${ref}"
    say "  -> ${csv//,/ }"
    if kind load docker-image --name "$CLUSTER_NAME" --nodes "$csv" "$ref" >/dev/null 2>&1; then
      loaded=$((loaded + 1))
    else
      # Do not give up on the whole run for one image. The known cause is the
      # containerd store detected above, but a multi-platform image can trip the
      # same `ctr import --all-platforms` failure on its own — and the node can
      # always fetch it directly.
      warn "kind load failed for ${ref}; falling back to a pull on the node"
      for node in ${csv//,/ }; do node_pull "$node" "$ref"; done
      fellback=$((fellback + 1))
    fi
  done
  if [[ $loaded -eq 0 && $fellback -eq 0 ]]; then
    good "every node already had all $(echo $tag_refs | wc -w | tr -d ' ') tag-pinned images"
  else
    good "loaded ${loaded} image(s) from the host cache, ${fellback} pulled on the node"
  fi

  # --- (2) digest-pinned: pulled by the node's own containerd --------------
  head_ "digest-pinned images: pulled on the node (see the header for why)"
  local missing
  for node in $nodes; do
    missing="$(missing_on_node "$node" $digest_refs || true)"
    if [[ -z "$missing" ]]; then
      say "${node}: all present"
    else
      node_pull "$node" $missing
    fi
  done

  write_lock
}

# ---------------------------------------------------------------------------
# host/images.lock — what the cluster ACTUALLY has, read back from the cluster
#
# The CLAUDE.md §5 agreement is to record versions at install time rather than
# from memory. This is that record, written by the thing doing the installing:
# every reference plus the digest containerd resolved it to, read out of a node
# after the pulls. A tag can be re-pushed; the digest here is what ran.
# ---------------------------------------------------------------------------
write_lock() {
  # Read every node and let python merge them. The UNION matters: digests are
  # identical everywhere, so any node can supply the digest for an image — but a
  # node may legitimately lack one (kind load only targets nodes that were
  # missing it), and reading a single node would report that as absent.
  local node files=""
  for node in $(nodes_of_cluster); do
    if docker exec "$node" crictl images -o json > "${TMP}/lock-${node}.json" 2>/dev/null; then
      files="${files} ${TMP}/lock-${node}.json"
    fi
  done
  [[ -n "$files" ]] || { warn "could not read images back from any node — no lock written"; return 0; }

  {
    echo "# host/images.lock — the digests of the images actually present in cluster"
    echo "# '${CLUSTER_NAME}', read back from its nodes by scripts/pull-images.sh."
    echo "#"
    echo "# This is the CLAUDE.md §5 record, written at install time by the thing doing"
    echo "# the installing rather than from memory afterwards. A tag can be re-pushed;"
    echo "# the digest on the line below is what ran. Commit it."
    echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo
    python3 -c '
import json, sys

listfile, jsonfiles = sys.argv[1], sys.argv[2:]
wanted = [l.split()[1] for l in open(listfile)
          if l.strip() and not l.startswith("#") and l.split()[0] == "node"]

byname = {}
for path in jsonfiles:
    try:
        images = json.load(open(path)).get("images") or []
    except Exception:
        continue
    for img in images:
        digests = img.get("repoDigests") or []
        for tag in (img.get("repoTags") or []):
            byname[tag] = digests[0] if digests else "(present, no digest recorded)"
        for digest in digests:
            byname[digest] = digest

for ref in sorted(wanted):
    print("%-104s %s" % (ref, byname.get(ref, "NOT PRESENT")))
' "$IMAGES_FILE" $files
  } > "$LOCK_FILE"

  local absent
  absent="$(grep -c 'NOT PRESENT' "$LOCK_FILE" || true)"
  if [[ "${absent:-0}" -gt 0 ]]; then
    # Not fatal, and not always wrong: on a first run nothing has applied
    # host/serving/ yet, so an image can be pulled and still not be reported by a
    # node that has not been asked to run it. But it has to be VISIBLE — a lock
    # file full of NOT PRESENT is not a record of anything.
    warn "${absent} listed image(s) are not on any node — see host/images.lock"
  else
    good "recorded the digests of $(grep -cvE '^[[:space:]]*(#|$)' "$LOCK_FILE") images in host/images.lock"
  fi
}

# ---------------------------------------------------------------------------
# --record — add the images the live cluster is running but no manifest names
#
# This is how the Calico dataplane and the vCluster control-plane images get into
# the list: their names are chosen by an operator and a Helm chart, so they exist
# nowhere in this repo. Read from the pods themselves — the cluster is the only
# honest source for "what did that operator actually deploy".
# ---------------------------------------------------------------------------
record() {
  require_list
  head_ "recording images the live cluster is running"
  local known found new ref
  # `tr` is load-bearing: the membership test below is a glob over " $known ",
  # so the entries have to be SPACE-separated. With the newlines refs_for_scope
  # emits, every ref looked new and --record appended duplicates of the whole
  # list on each run.
  known="$(refs_for_scope node | tr '\n' ' ')"
  found="$(kubectl --context "kind-${CLUSTER_NAME}" get pods -A \
    -o jsonpath='{range .items[*]}{range .spec.initContainers[*]}{.image}{"\n"}{end}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' \
    | grep -v '^$' | sort -u)" || die "could not read pod images from the cluster"

  new=""
  for ref in $found; do
    ref="$(normalize_ref "$ref")"
    case " $known $new " in *" $ref "*) continue ;; esac
    new="${new} ${ref}"
  done

  if [[ -z "$new" ]]; then
    good "nothing new — every running image is already listed"
    return 0
  fi

  for ref in $new; do assert_allowed "$ref"; done

  {
    echo "#"
    echo "# recorded $(date -u +%Y-%m-%dT%H:%M:%SZ) from the running pods of cluster '${CLUSTER_NAME}'"
    for ref in $new; do echo "node  ${ref}"; done
  } >> "$IMAGES_FILE"
  for ref in $new; do say "added ${ref}"; done
  good "$(echo $new | wc -w | tr -d ' ') image(s) appended to host/images.txt — commit it"
}

# ---------------------------------------------------------------------------
need() { for b in "$@"; do command -v "$b" >/dev/null 2>&1 || die "'$b' is not on PATH"; done; }

main() {
  # Checked per mode, not up front: --refresh and --list read committed files and
  # must work on a machine with no Docker and no network at all. That is not a
  # nicety — regenerating the list is exactly what you do when preparing for an
  # offline demo, possibly on a different machine from the one running the cluster.
  case "${1:-}" in
    --refresh)    refresh ;;
    --list)       list ;;
    --node-image) need docker kind; node_image ;;
    --record)     need kubectl; record ;;
    --nodes)      need docker kind python3; provision_nodes ;;
    -h|--help)    sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    "")           need docker kind python3; node_image; provision_nodes ;;
    *)            die "unknown flag '$1' (try --help)" ;;
  esac
}
main "$@"
