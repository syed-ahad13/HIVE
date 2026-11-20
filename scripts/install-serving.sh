#!/usr/bin/env bash
#
# install-serving.sh — the ONE central serving stack for the HIVE-LOCAL host cluster.
#
# Installs, in the only order that works:
#   1. cert-manager                       (webhook certificates for Knative + KServe)
#   2. Knative Serving core + Kourier     (the serverless runtime and its ingress)
#   3. KServe                             (the single central inference control plane)
#
# CLAUDE.md invariant §2.2: exactly ONE KServe, on the host, in Serverless (Knative)
# mode. Never one per tenant, never RawDeployment/Standard.
#
# All manifests are VENDORED under host/serving/ rather than fetched from a URL at
# apply time, so the exact bytes installed are committed to git and a rebuild months
# from now installs the same thing. The version constants below document which
# upstream release each vendored file came from; refresh them with `--refetch`.
#
# Idempotent: every step is `kubectl apply` (declarative, converges) or a merge
# patch (asserts a value). Safe to re-run at any time.
#
# Usage:
#   scripts/install-serving.sh            # install / converge
#   scripts/install-serving.sh --refetch  # re-download the pinned manifests first
set -euo pipefail

# ---------------------------------------------------------------------------
# Pinned versions — keep in sync with VERSIONS.md
# ---------------------------------------------------------------------------
CERT_MANAGER_VERSION="v1.21.1"
KNATIVE_VERSION="knative-v1.23.0"   # serving + net-kourier share this tag
KSERVE_VERSION="v0.20.0"

CLUSTER_NAME="${CLUSTER_NAME:-hive-host}"
KCTX="kind-${CLUSTER_NAME}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVING_DIR="${REPO_ROOT}/host/serving"

kc() { kubectl --context "$KCTX" "$@"; }

banner() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# Optional: refresh the vendored manifests from upstream at the pinned versions
# ---------------------------------------------------------------------------
refetch() {
  banner "re-fetching pinned manifests into host/serving/"
  curl -fsSL -o "${SERVING_DIR}/cert-manager/cert-manager.yaml" \
    "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
  curl -fsSL -o "${SERVING_DIR}/knative/serving-crds.yaml" \
    "https://github.com/knative/serving/releases/download/${KNATIVE_VERSION}/serving-crds.yaml"
  curl -fsSL -o "${SERVING_DIR}/knative/serving-core.yaml" \
    "https://github.com/knative/serving/releases/download/${KNATIVE_VERSION}/serving-core.yaml"
  curl -fsSL -o "${SERVING_DIR}/knative/kourier.yaml" \
    "https://github.com/knative-extensions/net-kourier/releases/download/${KNATIVE_VERSION}/kourier.yaml"
  curl -fsSL -o "${SERVING_DIR}/kserve/kserve-crds.yaml" \
    "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve-crds.yaml"
  curl -fsSL -o "${SERVING_DIR}/kserve/kserve.yaml" \
    "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve.yaml"
  curl -fsSL -o "${SERVING_DIR}/kserve/kserve-cluster-resources.yaml" \
    "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve-cluster-resources.yaml"
}

[[ "${1:-}" == "--refetch" ]] && refetch

# Every manifest here is large enough that a client-side apply would overflow the
# 262144-byte last-applied-configuration annotation (cert-manager 1 MB, kserve.yaml
# 7 MB). --server-side is mandatory, not stylistic.
#
# --force-conflicts is required for RE-RUNS, and this is the subtle part. Several
# of these components mutate their own manifests after install: Knative's webhook
# reconciler writes .webhooks[].rules, and cert-manager's cainjector writes
# .webhooks[].clientConfig.caBundle. Server-side apply records those controllers as
# the field managers, so a second `apply --server-side` of the same file fails with
#   "Apply failed with 1 conflict: conflict with \"webhook\" ... .webhooks[].rules"
# and the script is no longer idempotent. --force-conflicts reclaims the fields to
# our manifest; the owning controller immediately re-reconciles its dynamic values,
# which is the documented resolution for exactly this situation.
apply_ss() { kc apply --server-side --force-conflicts -f "$1"; }

# ---------------------------------------------------------------------------
# 1. cert-manager
#
# Knative's and KServe's admission webhooks need serving certificates. This must
# be fully Ready before anything that registers a webhook, or those installs race
# against an unavailable CA injector.
# ---------------------------------------------------------------------------
banner "1/3 cert-manager ${CERT_MANAGER_VERSION}"
apply_ss "${SERVING_DIR}/cert-manager/cert-manager.yaml" >/dev/null
kc wait --for=condition=Available deploy --all -n cert-manager --timeout=600s

# ---------------------------------------------------------------------------
# 2. Knative Serving + Kourier
#
# CRDs first, then core, then the networking layer. Kourier's controller runs in
# the knative-serving namespace; only its Envoy gateway lives in kourier-system.
# ---------------------------------------------------------------------------
banner "2/3 Knative Serving + Kourier ${KNATIVE_VERSION}"
apply_ss "${SERVING_DIR}/knative/serving-crds.yaml" >/dev/null
apply_ss "${SERVING_DIR}/knative/serving-core.yaml" >/dev/null
kc wait --for=condition=Available deploy --all -n knative-serving --timeout=600s

apply_ss "${SERVING_DIR}/knative/kourier.yaml" >/dev/null

# These two patches are what make Knative usable here, and neither is optional.
#
# config-network: Knative ships this ConfigMap with only an `_example` block, so
# ingress-class effectively defaults to Istio. Without this patch every Route waits
# forever on an Istio ingress that does not exist.
#
# config-domain: sets the default domain to hive.local. It deliberately does not
# resolve — we reach services via port-forward with an explicit Host header.
banner "patching knative-serving ConfigMaps"
kc patch configmap config-network -n knative-serving \
  --type merge --patch-file "${SERVING_DIR}/knative/config-network-patch.yaml"
kc patch configmap config-domain -n knative-serving \
  --type merge --patch-file "${SERVING_DIR}/knative/config-domain-patch.yaml"

kc wait --for=condition=Available deploy --all -n knative-serving --timeout=600s
kc wait --for=condition=Available deploy --all -n kourier-system --timeout=600s

# ---------------------------------------------------------------------------
# 3. KServe
# ---------------------------------------------------------------------------
banner "3/3 KServe ${KSERVE_VERSION} (Serverless / Knative mode)"

# The released kserve.yaml contains NO Namespace object — it assumes `kserve`
# already exists. Applying it to a fresh cluster without this produces a wall of
# `namespaces "kserve" not found`. Create it first.
kc apply -f "${SERVING_DIR}/kserve/namespace.yaml" >/dev/null

apply_ss "${SERVING_DIR}/kserve/kserve-crds.yaml" >/dev/null
apply_ss "${SERVING_DIR}/kserve/kserve.yaml" >/dev/null

banner "waiting for the KServe controllers and webhooks"
kc wait --for=condition=Available deploy --all -n kserve --timeout=600s

# kserve-cluster-resources.yaml contains ClusterServingRuntimes and
# LLMInferenceServiceConfigs — CUSTOM RESOURCES, which the KServe validating
# webhook must admit. A Deployment reporting Available is not sufficient: the
# webhook Service's endpoints need a moment more, and applying too early fails with
# `connection refused` from the admission webhook. Retry rather than sleep-and-hope.
banner "installing default ClusterServingRuntimes"
# Note this uses apply_ss, i.e. --force-conflicts: on a re-run the KServe controller
# ("manager") has normalised quantities such as
# .spec.router.scheduler.template.containers[].resources.limits.cpu on the
# LLMInferenceServiceConfigs, so a plain server-side apply conflicts exactly the way
# the Knative webhook configurations do above.
runtimes_applied=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if apply_ss "${SERVING_DIR}/kserve/kserve-cluster-resources.yaml" >/dev/null 2>&1; then
    runtimes_applied=1
    break
  fi
  echo "    webhook not admitting yet (attempt ${attempt}/10) — retrying in 10s"
  sleep 10
done
if [[ $runtimes_applied -ne 1 ]]; then
  echo "ERROR: could not apply cluster resources; last error:" >&2
  apply_ss "${SERVING_DIR}/kserve/kserve-cluster-resources.yaml" >&2 || true
  exit 1
fi

# Assert Serverless mode. The shipped manifest already sets this, but KServe's
# compiled-in fallback is Standard — so if the key were ever dropped, the invariant
# would break silently. Asserting on every run is the guard.
banner "asserting deploy.defaultDeploymentMode = Serverless"
kc patch configmap inferenceservice-config -n kserve \
  --type merge --patch-file "${SERVING_DIR}/kserve/inferenceservice-config-patch.yaml"

MODE="$(kc get cm inferenceservice-config -n kserve -o jsonpath='{.data.deploy}' | tr -d ' \n')"
case "$MODE" in
  *'"defaultDeploymentMode":"Serverless"'*) echo "    OK: Serverless" ;;
  *) echo "ERROR: unexpected deployment mode: ${MODE}" >&2; exit 1 ;;
esac

# Make KServe publish URLs that Kourier can actually serve.
#
# KServe defaults to assuming Istio: it strips "-predictor" from the Knative route
# host and publishes the shortened name, which only an Istio VirtualService would
# resolve. With Kourier and no Istio, the InferenceService reports READY=True while
# its advertised URL 404s at the gateway. disableIstioVirtualHost: true makes KServe
# publish the predictor route unchanged. See the patch file for the full reasoning.
banner "asserting ingress.disableIstioVirtualHost = true (Kourier, no Istio)"
kc patch configmap inferenceservice-config -n kserve \
  --type merge --patch-file "${SERVING_DIR}/kserve/ingress-config-patch.yaml"

VHOST="$(kc get cm inferenceservice-config -n kserve -o jsonpath='{.data.ingress}' | tr -d ' \n')"
case "$VHOST" in
  *'"disableIstioVirtualHost":true'*) echo "    OK: disableIstioVirtualHost=true" ;;
  *) echo "ERROR: disableIstioVirtualHost is not true — isvc URLs will 404" >&2; exit 1 ;;
esac

# The controller caches this ConfigMap, so config changes only take effect after a
# restart. Restart only when the effective config actually differs from what the
# running controller started with.
#
# Compare a CONTENT HASH, not metadata.resourceVersion. kserve.yaml *contains*
# inferenceservice-config, so the `apply --force-conflicts` above reverts the two
# patches back to upstream defaults, and the patches then restore them — which bumps
# resourceVersion on every single run. A resourceVersion guard therefore bounces the
# controller every time the script runs, while a content hash correctly stays put.
CM_REV="$(kc get cm inferenceservice-config -n kserve \
  -o jsonpath='{.data.deploy}{.data.ingress}' | shasum -a 256 | cut -c1-16)"
LAST_REV="$(kc get deploy kserve-controller-manager -n kserve \
  -o jsonpath='{.spec.template.metadata.annotations.hive\.ksl/isvc-config-hash}' 2>/dev/null || true)"
if [[ "$CM_REV" != "$LAST_REV" ]]; then
  banner "restarting kserve-controller-manager to pick up config changes"
  kc patch deploy kserve-controller-manager -n kserve --type merge -p \
    "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"hive.ksl/isvc-config-hash\":\"${CM_REV}\"}}}}}" >/dev/null
  kc rollout status deploy/kserve-controller-manager -n kserve --timeout=300s
else
  echo "    controller already running against this config — no restart needed"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
banner "serving stack is up"
for ns in cert-manager knative-serving kourier-system kserve; do
  echo
  echo "--- ${ns} ---"
  kc get pods -n "$ns"
done
echo
echo "--- clusterservingruntimes ---"
kc get clusterservingruntimes
