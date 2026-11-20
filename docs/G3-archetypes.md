# G3 — T-AO and T-SZ archetype services, live from inside the vClusters

**Status: PASSED** · 2026-08-18 · host cluster `hive-host`

Both tenant archetypes are deployed, reachable, and demonstrably behaving
*differently* under idle — which is the whole point of having two archetypes.

Each service was **authored inside its own tenant vCluster**, mirrored to the host,
and reconciled by the **one** central KServe. No tenant touched the host.

| | T-AO (Inf-101) | T-SZ (Inf-103) |
|---|---|---|
| Service | `legal-translation` | `species-classifier` |
| Template | `t-ao` | `t-sz` |
| min-scale | **1** (always-on) | **0** (scale-to-zero) |
| window / scale-down-delay | Knative defaults | **60s / 60s** (production: 30m/30m) |
| Idle behaviour | 1 pod after **347s** idle | **0 pods** after 41s, GPU released |
| Latency | 0.079s (never cold) | cold **9.74s** · warm 0.026s |
| GPU | 1, held permanently | 1, only while serving |

---

## Names, as seen from each side

Inside the tenant's own cluster, the tenant sees the name they chose:

```
$ vcluster connect inf-101 -n p-inf-101 -- kubectl get isvc -n default
NAME                HOST_READY   HOST_NAME
legal-translation   True         legal-translation-x-default-x-inf-101

$ vcluster connect inf-103 -n p-inf-103 -- kubectl get isvc -n default
NAME                 HOST_READY   HOST_NAME
species-classifier   True         species-classifier-x-default-x-inf-103
```

On the host, the mirrored names carry the vCluster suffix:

| Inside the vCluster | Synced name on the host | Host namespace |
|---|---|---|
| `legal-translation` | `legal-translation-x-default-x-inf-101` | `p-inf-101` |
| `species-classifier` | `species-classifier-x-default-x-inf-103` | `p-inf-103` |

```
p-inf-101  legal-translation-x-default-x-inf-101   READY=True
p-inf-103  species-classifier-x-default-x-inf-103  READY=True
```

Host URLs (Kourier, `-predictor` per the step-4 deviation):

```
http://legal-translation-x-default-x-inf-101-predictor.p-inf-101.hive.local
http://species-classifier-x-default-x-inf-103-predictor.p-inf-103.hive.local
```

---

## Predictor pods on the shared workers, with GPUs allocated

```
p-inf-101   legal-translation-x-c841fdde87b353dd74ac0528e3598008-deplov5qc6  2/2  Running  hive-host-worker2
p-inf-103   species-classifier-x006446a0dd7b85d9626a28f1c8d384b6-deplot4lm6  2/2  Running  hive-host-worker
```

Both tenants' workloads run on the **shared** worker nodes — separate tenants,
separate namespaces, one physical pool. `2/2` is `kserve-container` + Knative's
injected `queue-proxy`. (Pod names are hashed because the mirrored isvc name is long.)

`kubectl describe node` excerpts, with both services up:

```
### hive-host-worker  (Allocated resources)
  Resource           Requests      Limits
  --------           --------      ------
  cpu                2025m (25%)   4700m (58%)
  memory             3292Mi (42%)  9156Mi (116%)
  nvidia.com/gpu     1             1

### hive-host-worker2  (Allocated resources)
  Resource           Requests      Limits
  --------           --------      ------
  cpu                1765m (22%)   7100m (88%)
  memory             3160Mi (40%)  9796Mi (125%)
  nvidia.com/gpu     1             1
```

One fake GPU allocated on each worker, one per tenant. Note `Requests` equals
`Limits` for `nvidia.com/gpu` — extended resources cannot be overcommitted, so
Kubernetes derived the request from the `limits` the tenant wrote (§2.3).

---

## Data path — both return correct predictions

```
$ scripts/curl-isvc.sh p-inf-101 legal-translation-x-default-x-inf-101 tests/iris-input.json
HTTP 200
response: {"predictions":[1,1]}
timing:  total 0.079222s   time-to-first-byte 0.079094s
         running predictor pods before request: 1

$ LOCAL_PORT=8081 scripts/curl-isvc.sh p-inf-103 species-classifier-x-default-x-inf-103 tests/iris-input.json
HTTP 200
response: {"predictions":[1,1]}
timing:  total 0.079203s   time-to-first-byte 0.078927s
```

These predictions also prove the **`pvc://models`** storage URI resolved correctly —
the model was read from the tenant's own PVC (seeded in step 5), not downloaded.

---

## Archetype behaviour under idle

Observed with zero traffic to either service:

```
ELAPSED    T-SZ inf-103 (min-scale 0)         T-AO inf-101 (min-scale 1)
t+0       s running=1  [Running Terminating ]   running=1  [Running ]
t+21      s running=1  [Running ]               running=1  [Running ]
t+41      s running=0  [Terminating ]           running=1  [Running ]
           >>> T-SZ SCALED TO ZERO at t+41s (GPU released)
t+82      s running=0  []                       running=1  [Running ]
t+163     s running=0  []                       running=1  [Running ]
t+245     s running=0  []                       running=1  [Running ]
t+347     s running=0  []                       running=1  [Running ]

=== after 347s with zero traffic ===
  T-SZ scaled to zero at t+41s; running now: 0
  T-AO running now: 1  <-- always-on requires >= 1
```

GPU allocation with T-SZ idle — the GPU is genuinely returned to the pool:

```
hive-host-worker:  0 / 0     <-- T-SZ released it
hive-host-worker2: 1 / 1     <-- T-AO still holds it
```

**T-AO confirmed always-on:** 1 pod after **347s (5m47s)** of no traffic, well past
the 5-minute requirement, GPU held throughout.

### T-SZ cold start

```
    running predictor pods before request: 0
    => scaled to zero, so this request pays a COLD START
HTTP 200
response: {"predictions":[1,1]}
timing:  total 9.740307s   time-to-first-byte 9.740043s
         running predictor pods after request: 1
```

**Cold start: 9.74s.** Warm immediately after: **0.026s**.

Notably *faster* than Gate G1's 14–15s, and the reason is instructive: G1's model came
from `gs://`, so its cold start included a storage-initializer container downloading
from Google Cloud Storage. These archetypes use `pvc://models`, which KServe mounts
directly with **no init container at all** — the model is already on the node's disk.
Roughly 5 seconds of the cold start was network fetch, and moving models to a PVC
removed it. That is a real, transferable finding about cold-start budgets.

---

## Two findings that changed the manifests

### 1. `autoscaling.knative.dev/min-scale: "0"` does nothing on its own

The annotation is **silently overridden**. KServe derives the Knative `min-scale`
annotation from `spec.predictor.minReplicas`, which **defaults to 1** when unset. With
only the annotation, the generated Knative Service carried:

```
autoscaling.knative.dev/min-scale                    1     <-- clobbered, not 0
autoscaling.knative.dev/scale-down-delay             60s   <-- passed through
autoscaling.knative.dev/window                       60s   <-- passed through
```

The service then **sat at 1 pod for 347s of complete idleness, holding a GPU** — a
scale-to-zero archetype silently behaving as always-on. `window` and
`scale-down-delay` propagate untouched because KServe has no spec field for them;
only `min-scale` is overridden, which is exactly what makes it easy to miss.

Fix: set `spec.predictor.minReplicas: 0`. Both are now kept, with distinct jobs —
`minReplicas` is the mechanism KServe honours, the annotation declares archetype
intent at the tenant-facing layer. `minReplicas: 1` is also stated explicitly for
T-AO rather than relying on the coincidence that KServe's default happens to match.

### 2. A GPU quota equal to the tier makes the InferenceService un-Ready

With `requests.nvidia.com/gpu: "1"` and a 1-GPU workload running, KServe's reconcile
was rejected forever:

```
admission webhook "validation.webhook.serving.knative.dev" denied the request:
validation callback failed: dry run failed with pods "dry-run-validation..." is
forbidden: exceeded quota: tier-quota, requested: requests.nvidia.com/gpu=1,
used: requests.nvidia.com/gpu=1, limited: requests.nvidia.com/gpu=1
```

Knative's validating webhook **dry-run creates a sample pod** to validate every
Revision template, requesting the same resources as the real one. With the real pod
already holding the entire ceiling, the dry-run can never pass. The InferenceService
stayed `READY=False` *while the pod was Running and the Knative Service, Route and
Revision were all Ready* — the service worked; only its status could not converge.

The dry-run is **unconditional** in Knative v1.23 (`pkg/webhook/podspec_dryrun.go` has
no feature flag and no annotation gate), so it cannot be disabled. Confirmed by
experiment: raising the quota to 2 made the isvc go `READY=True` within 20s.

It hits T-AO hardest — an always-on service never releases its GPU, so it is
permanently stuck. T-SZ self-heals once idle, which is arguably worse: an intermittent
failure that looks like a flake.

Quota is now `tier + 1` GPU of validation headroom in both tenants.
**This needs a platform-level decision** — see DEVIATIONS.md. The enforced concurrent
ceiling is now 2 rather than 1, so the ResourceQuota alone no longer proves the tier.

---

## Reproducing

```bash
# author inside each tenant's own cluster
vcluster connect inf-101 -n p-inf-101 -- kubectl apply -f - < tenants/inf-101/legal-translation.yaml
vcluster connect inf-103 -n p-inf-103 -- kubectl apply -f - < tenants/inf-103/species-classifier.yaml

# mirror to host (fallback B; see docs/S1-spike.md)
scripts/sync-isvc.sh --once

# data path
scripts/curl-isvc.sh              p-inf-101 legal-translation-x-default-x-inf-101  tests/iris-input.json
LOCAL_PORT=8081 scripts/curl-isvc.sh p-inf-103 species-classifier-x-default-x-inf-103 tests/iris-input.json
```

Both services are left running as the archetype canaries. T-AO holds a GPU
continuously by design; T-SZ releases its GPU roughly two minutes after the last
request.
