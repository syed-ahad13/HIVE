#!/usr/bin/env bash
#
# build-rag-toolkit.sh — build the RAG toolkit image and make it available to
#                        the kind nodes.
#
#   Usage:
#     scripts/build-rag-toolkit.sh          # build if missing, then load
#     FORCE=1 scripts/build-rag-toolkit.sh  # rebuild even if it already exists
#     scripts/build-rag-toolkit.sh --check  # report status, build nothing
#
# ===========================================================================
# WHY THIS EXISTS AT ALL
# ===========================================================================
# CLAUDE.md §5: "All cluster changes go through files committed in this repo...
# No imperative one-offs, except where a script in scripts/ wraps them."
# Building an image and pushing it into a cluster is imperative by nature, so it
# is wrapped here — the same carve-out `vcluster create` gets inside the Studio
# pipeline.
#
# It is called TWICE, on purpose, and both callers matter:
#
#   * by a human, once, before a demo;
#   * by RealExecutor.approve, on every RAG deployment.
#
# The second is why this script must be IDEMPOTENT AND CHEAP. A precondition
# that rebuilt a 700MB image on every deploy would be unusable; a precondition
# that only *checked* would fail the first deploy on a fresh machine with an
# error a PI cannot act on. So: check first, build only if needed, and always
# make sure the nodes have it.
#
# ===========================================================================
# TWO PLACES THE IMAGE HAS TO EXIST
# ===========================================================================
# The host Docker daemon and the kind nodes' containerd are separate image
# stores. `docker build` fills the first; `kind load docker-image` copies it
# into the second. A pod cannot see the first at all — which is the failure this
# script exists to prevent, because its symptom is ImagePullBackOff on an image
# that `docker images` clearly shows is present.
#
# The node check is therefore NOT skipped when the build is: an image can be
# present on the host and absent from the nodes after a `kind delete`/`create`,
# and that is exactly the state a rebuild would not fix.
# ===========================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT="${REPO_ROOT}/studio/images/rag-toolkit"

# ---------------------------------------------------------------------------
# THE TAG IS ALSO WRITTEN IN studio/backend/app/render.py (RAG_TOOLKIT_IMAGE),
# WHICH IS WHAT PUTS IT INTO EVERY RENDERED MANIFEST. The two must agree.
#
# They are not derived from one another because the renderer must stay pure and
# importable without a shell, and this script must run without Python. The drift
# is caught rather than prevented: `--check` compares them, and
# RealExecutor.approve runs this script and then verifies the image the RENDERER
# will name is the one on the nodes.
# ---------------------------------------------------------------------------
IMAGE="${IMAGE:-hive-local/rag-toolkit:0.1.0}"
CLUSTER="${CLUSTER:-hive-host}"
PLATFORM="${PLATFORM:-linux/arm64}"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

say()  { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }

echo "==> rag-toolkit image: ${IMAGE}"

# --- the renderer must name the same tag -------------------------------------
# A mismatch means every rendered manifest points at an image that was never
# built, and the failure appears three steps later as ImagePullBackOff.
RENDER_PY="${REPO_ROOT}/studio/backend/app/render.py"
if [[ -f "${RENDER_PY}" ]]; then
  RENDER_TAG="$(sed -n 's/^RAG_TOOLKIT_IMAGE[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "${RENDER_PY}" | head -1)"
  if [[ -n "${RENDER_TAG}" && "${RENDER_TAG}" != "${IMAGE}" ]]; then
    die "app/render.py renders '${RENDER_TAG}' but this script builds '${IMAGE}'.
        Every manifest would name an image that does not exist. Make them agree."
  fi
  [[ -n "${RENDER_TAG}" ]] && ok "app/render.py renders the same tag"
fi

# --- prerequisites -----------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running"
command -v kind >/dev/null 2>&1 || die "kind is not on PATH"

# --- is it on the host? ------------------------------------------------------
HAVE_HOST=0
docker image inspect "${IMAGE}" >/dev/null 2>&1 && HAVE_HOST=1

# --- is it on the nodes? -----------------------------------------------------
# crictl inside a node, because that is the store the kubelet actually pulls
# from. Only the control-plane node is checked: kind load pushes to every node,
# so one node's answer is representative, and querying three costs three execs
# on a path that runs before every deployment.
NODE="${CLUSTER}-control-plane"
HAVE_NODE=0
if docker ps --format '{{.Names}}' | grep -qx "${NODE}"; then
  if docker exec "${NODE}" crictl images 2>/dev/null | awk '{print $1":"$2}' | grep -qx "docker.io/${IMAGE}"; then
    HAVE_NODE=1
  elif docker exec "${NODE}" crictl images 2>/dev/null | awk '{print $1":"$2}' | grep -qx "${IMAGE}"; then
    HAVE_NODE=1
  fi
else
  warn "kind node '${NODE}' is not running — cannot check or load"
fi

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  [[ "${HAVE_HOST}" -eq 1 ]] && ok "present on the host Docker daemon" || warn "NOT built on the host"
  [[ "${HAVE_NODE}" -eq 1 ]] && ok "present on kind node ${NODE}"      || warn "NOT loaded into the kind nodes"
  [[ "${HAVE_HOST}" -eq 1 && "${HAVE_NODE}" -eq 1 ]] || exit 1
  exit 0
fi

# --- build -------------------------------------------------------------------
if [[ "${HAVE_HOST}" -eq 1 && "${FORCE:-0}" != "1" ]]; then
  say "already built on the host — skipping (FORCE=1 to rebuild)"
else
  [[ "${FORCE:-0}" == "1" ]] && say "FORCE=1 — rebuilding"
  say "building for ${PLATFORM} from studio/images/rag-toolkit/"
  # --platform is explicit rather than implied by the host: it is the one thing
  # that would silently produce an amd64 image under Rosetta and a DEVIATIONS.md
  # row nobody wrote. See the Dockerfile header for the per-wheel arm64 audit.
  docker build --platform "${PLATFORM}" -t "${IMAGE}" "${CONTEXT}"
  ok "built ${IMAGE}"
  # A fresh build is a different image id, so whatever the nodes hold is stale.
  HAVE_NODE=0
fi

# --- verify the architecture -------------------------------------------------
# Cheap, and it catches the one mistake that is invisible until a pod starts:
# an image built for the wrong architecture runs under emulation, slowly, with
# no error and no DEVIATIONS.md row.
BUILT_ARCH="$(docker image inspect "${IMAGE}" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || echo unknown)"
if [[ "${BUILT_ARCH}" != "${PLATFORM}" ]]; then
  warn "image is ${BUILT_ARCH}, expected ${PLATFORM} — it will run under emulation."
  warn "If that is intended, add a row to DEVIATIONS.md (CLAUDE.md §5)."
else
  ok "architecture ${BUILT_ARCH}"
fi

# --- load into the nodes -----------------------------------------------------
if [[ "${HAVE_NODE}" -eq 1 ]]; then
  say "already loaded into the kind nodes — skipping"
else
  if ! docker ps --format '{{.Names}}' | grep -qx "${NODE}"; then
    die "kind cluster '${CLUSTER}' is not running; cannot load the image into it"
  fi
  say "loading into kind cluster '${CLUSTER}' (this copies ~1GB and takes a moment)"
  kind load docker-image "${IMAGE}" --name "${CLUSTER}"
  ok "loaded into ${CLUSTER}"
fi

echo
echo "==> ${IMAGE} is available to the cluster"
