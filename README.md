# HIVE-LOCAL

A single-laptop rehearsal of **HIVE**, the multi-tenant GPU inference platform being
built by the KSL AI Platforms team. In production, ~20 university research groups
(*PIs*) each get their own Kubernetes control plane as a **vCluster** on one shared
physical cluster, all serving models through **one central KServe**.

This repository reproduces that architecture on a MacBook M2 Pro with **two** tenants,
so the design can be exercised, measured and corrected before it is migrated to the
real platform:

| Tenant | Archetype | Behaviour | Tier | Allocation |
|---|---|---|---|---|
| **Inf-101** | PI A (T-AO) | always-on LLM service | Medium — 1 GPU | 90 days |
| **Inf-103** | PI C (T-SZ) | bursty, scales to zero | Small — 1 GPU | 60 days |

Because it is a rehearsal, two rules dominate: **every artifact is a declarative file
committed to git**, and **every local substitution for a production component is logged
in [DEVIATIONS.md](DEVIATIONS.md)** — that file is the migration checklist.

> **Start with [CLAUDE.md](CLAUDE.md).** It is the architecture contract: the
> non-negotiable invariants (§2), the local-vs-production deviations (§3), the working
> agreements (§5) and the HIVE Studio design (§6). This README only tells you how to
> run what is there.

---

## Status

| Step | What landed | Gate |
|---|---|---|
| 1 | `kind` cluster `hive-host` (1 control-plane + 2 workers, arm64) + Calico CNI | — |
| 2 | Fake `nvidia.com/gpu` extended resource, 4/worker | — |
| 3 | cert-manager + Knative Serving + Kourier + **one** KServe, Serverless mode | — |
| 4 | End-to-end prediction, cold start ~14 s, scale-to-zero observed | [G1](docs/G1-smoke.md) ✅ |
| 5 | Tenant namespaces, quotas, LimitRanges, seeded model PVCs | [T2](docs/T2-quota-proof.md) ✅ |
| 6 | Two tenant vClusters + InferenceService / catalogue sync | [S1](docs/S1-spike.md) ✅ *(via fallbacks)* |
| 9 | T-AO and T-SZ archetypes live, authored from inside the vClusters | [G3](docs/G3-archetypes.md) ✅ |
| S0 | HIVE Studio catalogue: templates, model list, render fragments | — |

**Invariants not yet implemented** (both from [CLAUDE.md](CLAUDE.md) §2):

- **§2.5 network isolation** — no `NetworkPolicy` exists anywhere yet. Every pod can
  still reach every pod across tenants.
- **§2.6 lifetime** — [host/reconciler/](host/reconciler/) is empty, and the
  `hive.ksl/expiry` annotation is deliberately not yet set on the two hand-built tenant
  namespaces. Only the Studio namespace-pack template writes it.

**HIVE Studio is data-only so far.** [templates/catalog/](templates/catalog/) holds the
use-case templates, the closed model list and the render fragments;
[studio/backend/](studio/backend/) and [studio/frontend/](studio/frontend/) are empty.

---

## Prerequisites

Docker Desktop, plus these on `PATH` — see [VERSIONS.md](VERSIONS.md) for the exact
versions this was built against:

```
kind        v0.32.0        kubectl   v1.36.1
vcluster    v0.36.1
node        >= 20          python3   >= 3.11
```

Everything runs natively on Apple Silicon. Two `ClusterServingRuntime` images
(`huggingfaceserver`, `mlserver`) are `amd64`-only and would need Rosetta — nothing
currently uses them, and the moment something does it goes in
[DEVIATIONS.md](DEVIATIONS.md).

---

## Bring-up from cold

One command, and it is the recommended one. It runs the platform the way it is meant
to be run — **real, end to end** — and there is no mock path through it:

```bash
scripts/hive-up.sh             # cluster -> GPUs -> serving -> tenants -> console
scripts/hive-up.sh --help      # flags: --no-studio, --no-tenants, --skip-serving, --verify
```

Nine idempotent phases, each announced as it starts, ending with HIVE Studio on
[localhost:5173](http://localhost:5173) with `STUDIO_EXECUTOR=real`. Ctrl+C stops the
console, the backend and the InferenceService syncer together. `STUDIO_EXECUTOR=mock`
is **rejected**, not honoured: mock exists so the console can be demonstrated with
Docker stopped, and a bring-up script that could quietly produce a console creating
nothing would be worse than no script at all.

The rest of this section is what those phases do, if you would rather run them
yourself. Steps 1–3 are scripted and safe to re-run; each converges rather than
duplicating.

```bash
scripts/create-cluster.sh      # kind cluster + Calico, skipped if it already exists
scripts/fake-gpus.sh           # patch fake GPUs onto the workers
scripts/install-serving.sh     # cert-manager -> Knative + Kourier -> KServe
```

Steps 5–6 by hand (this is `hive-up.sh` phases 7–10):

```bash
# tenant host namespaces, quotas, LimitRanges, PVCs, model seed Jobs
kubectl apply -f host/tenancy/inf-101/
kubectl apply -f host/tenancy/inf-103/

# one control plane per tenant. --add=false is MANDATORY offline (VERSIONS.md, step 6)
vcluster create inf-101 --namespace p-inf-101 -f templates/t-ao-values.yaml \
  --connect=false --add=false
vcluster create inf-103 --namespace p-inf-103 -f templates/t-sz-values.yaml \
  --connect=false --add=false

# fallback A: publish the host runtime catalogue into both vClusters
scripts/sync-catalog.sh
```

Then the tenants author their own services, inside their own clusters:

```bash
vcluster connect inf-101 -n p-inf-101 -- kubectl apply -f - < tenants/inf-101/legal-translation.yaml
vcluster connect inf-103 -n p-inf-103 -- kubectl apply -f - < tenants/inf-103/species-classifier.yaml

# fallback B: mirror them to the host, where the ONE KServe reconciles them.
# --once is a single pass; without it the script loops every 10s and must KEEP RUNNING.
scripts/sync-isvc.sh --once
```

### Container images

The bring-up script fetches every image the platform runs before anything needs it
(phases 1 and 4), from the project that publishes it and nowhere else:

```bash
scripts/pull-images.sh --list     # the plan: what gets pulled, and how
scripts/pull-images.sh            # pull + make available to the nodes
scripts/pull-images.sh --refresh  # regenerate host/images.txt from the manifests
scripts/pull-images.sh --record   # add what the live cluster is running
```

[host/images.txt](host/images.txt) is **generated, not written**: every reference in it
is extracted from a manifest already committed here — the vendored `host/serving/**` and
`host/cni/**` YAML, the tenancy Jobs, and the digest-pinned `kindest/node` row in
[VERSIONS.md](VERSIONS.md). Nobody retypes a tag, so the list cannot drift from what is
installed, and the script **refuses to fetch from any registry not on its allow-list**.
`host/images.lock` is written at pull time with the digest each reference resolved to.

Two things worth knowing:

- **13 of KServe's 14 `ClusterServingRuntime` images are not pulled.** Nothing here
  selects them, several are tens of gigabytes, and two are `amd64`-only. The one that is
  pulled — `kserve/sklearnserver` — is the runtime KServe actually picks for
  `modelFormat: sklearn` at the default `protocolVersion: v1`, as observed in
  [S1](docs/S1-spike.md).
- **The five digest-pinned Knative images are re-fetched whenever the cluster is
  recreated.** `kind load` works via `docker save`, which rebuilds the image manifest and
  therefore changes its digest, so a digest reference can only ever be satisfied by a
  containerd that fetched that exact manifest itself. Tag-pinned images come from the host
  Docker cache and need no network after the first pull.
- **Unless Docker Desktop's containerd image store is on**, in which case `kind load`
  cannot be used for anything: `docker save` then emits an index listing every platform
  while only one is present locally, and kind's `ctr import --all-platforms` fails with
  `content digest ...: not found`. The script detects this and pulls everything on the
  nodes instead — same result, no host cache. Turning the setting off (Docker Desktop →
  Settings → General) restores the cached path.

### After any Docker Desktop or machine restart

`scripts/hive-up.sh --skip-serving` is the fast path: it re-patches the GPUs, re-waits
the vClusters and restarts the syncer without re-applying the ~8 MB serving stack. The
images survive a restart, so phases 1 and 4 come out as no-ops; `--skip-images` skips
even the checking.

By hand, **re-run `scripts/fake-gpus.sh` first.** The fake GPUs are a node-status patch and the
kubelet is the authority for its own status, so a restart wipes them. The symptom is
pods stuck `Pending` with `Insufficient nvidia.com/gpu`, which does not obviously point
at the cause.

**Then wait for the control plane to settle before deploying or deleting anything.** A
restart cold-starts every pod at once, and in a Docker VM with too little memory that
tips `kube-controller-manager` and `kube-scheduler` into a crash loop. Deploys then time
out at `vcluster` and deletes hang on a `Terminating` namespace. Check:

```bash
kubectl get pods -n kube-system -l tier=control-plane   # no CrashLoopBackOff
```

Give Docker Desktop **10GiB of memory or more**; `hive-up.sh` warns below that. The full
diagnosis and recovery are in [docs/control-plane-recovery.md](docs/control-plane-recovery.md).

---

## Verifying it works

```bash
# the standing canary — free while idle, since it is scaled to zero
scripts/curl-isvc.sh default sklearn-iris tests/iris-input.json
```

Expect `HTTP 200` and `{"predictions":[1,1]}`: ~15 s cold, ~50 ms warm. The two
archetype services answer the same way, at their mirrored host names:

```bash
scripts/curl-isvc.sh p-inf-101 legal-translation-x-default-x-inf-101 tests/iris-input.json
LOCAL_PORT=8081 scripts/curl-isvc.sh p-inf-103 species-classifier-x-default-x-inf-103 tests/iris-input.json
```

Other checks: [tests/test-pod-networking.sh](tests/test-pod-networking.sh) (CNI and DNS)
and [tests/test-gpu-scheduling.sh](tests/test-gpu-scheduling.sh) (the scheduler really
rations the fake resource).

---

## Three things to know before changing anything

1. **Never construct an InferenceService URL — read `.status.url`.** Locally KServe
   publishes `<isvc>-predictor.<ns>.hive.local`; production (Istio/Kong) serves the
   short `<isvc>.<ns>.<domain>` form. Anything that assembles a hostname by hand breaks
   at migration. This is set by `disableIstioVirtualHost: true` and cost a silent
   404-in-8ms during G1.

2. **Tenant GPU quota is `tier + 1`, not `tier`, and this is an open question.**
   Knative's validating webhook unconditionally dry-run-creates a pod requesting the
   same resources, so an exact-fit quota leaves the InferenceService pinned at
   `READY=False` forever while the pod runs perfectly. The consequence is that the
   ResourceQuota alone no longer proves "Medium = 1 GPU". **This needs a platform-level
   decision — it is not purely local.** See the first row of [DEVIATIONS.md](DEVIATIONS.md).

3. **vCluster custom-resource sync is a paid feature, so both directions are scripted
   stand-ins.** `scripts/sync-isvc.sh` and `scripts/sync-catalog.sh` poll; status comes
   back as `hive.ksl/host-*` annotations rather than real `.status`; the catalogue is
   read-only by convention only; and **if the script is not running, nothing syncs**.
   The intended configuration is kept commented out in
   [templates/t-ao-values.yaml](templates/t-ao-values.yaml) and
   [templates/t-sz-values.yaml](templates/t-sz-values.yaml), so those files are the
   migration instruction. Details in [docs/S1-spike.md](docs/S1-spike.md).

---

## Repository layout

```
host/serving/      the ONE central KServe + Knative + Kourier (mostly vendored upstream)
host/tenancy/      host namespaces, quotas, LimitRanges, PVCs — the tenant ceiling
host/reconciler/   the expiry CronJob (§2.6) — not yet built
templates/         t-ao / t-sz vCluster values, and the Studio catalogue
tenants/           per-tenant InferenceServices, authored inside each vCluster
studio/            HIVE Studio BFF + frontend — not yet built
scripts/           the ONLY sanctioned place for imperative commands (§5)
tests/             verification of the §2 invariants
docs/              gate reports: G1, T2, S1, G3
```

Three logs are append-only and are updated **as work happens, not from memory**:
[VERSIONS.md](VERSIONS.md) (every component, at install time),
[DEVIATIONS.md](DEVIATIONS.md) (every local substitution) and the gate reports in
[docs/](docs/).
