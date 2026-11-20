# T2 — Tenant quota enforcement proof

**Status: PASSED** · 2026-08-17 · host cluster `hive-host`

Evidence that CLAUDE.md **§2.4** holds: tenant ceilings are enforced by a
`ResourceQuota` in the tenant's **host** namespace, **at admission time** — the API
server refuses to create an over-quota pod, rather than something reconciling it
away afterwards.

Nothing over-quota ever ran, not even briefly. All probe pods were deleted; the
tenant namespaces are left with only their completed `seed-models` Jobs.

---

## The ceiling under test

`ResourceQuota/tier-quota` in `p-inf-101` (Medium tier):

```
Resource                                   Used  Hard
--------                                   ----  ----
count/inferenceservices.serving.kserve.io  0     2
limits.cpu                                 0     8
limits.memory                              0     16Gi
persistentvolumeclaims                     2     4
requests.nvidia.com/gpu                    0     1
requests.storage                           2Gi   5Gi
```

`requests.nvidia.com/gpu: 1` is the binding constraint — the number that actually
rations the scarce resource. `persistentvolumeclaims 2/4` and `requests.storage
2Gi/5Gi` are already consumed by the tenant's `models` and `scratch` claims.

---

## Test A — a single pod asking for more than the whole tier

A pod requesting `limits.nvidia.com/gpu: "2"` against a ceiling of 1.

```bash
kubectl apply -f probe-2gpu.yaml    # limits.nvidia.com/gpu: "2", namespace p-inf-101
```

**Rejected. Exit code 1. Verbatim error:**

```
Error from server (Forbidden): error when creating "probe-2gpu.yaml": pods
"quota-probe-2gpu" is forbidden: exceeded quota: tier-quota, requested:
requests.nvidia.com/gpu=2, used: requests.nvidia.com/gpu=0, limited:
requests.nvidia.com/gpu=1
```

```bash
$ kubectl get pod quota-probe-2gpu -n p-inf-101
Error from server (NotFound): pods "quota-probe-2gpu" not found
```

The pod was **never created**. This is admission-time enforcement, not eviction.

Note also what the message proves about the request surface: the pod asked under
`limits`, and the quota counted `requests.nvidia.com/gpu=2`. Because extended
resources may never be overcommitted, Kubernetes derived an equal request from the
limit. Tenant-facing syntax (§2.3, always `limits`) and quota accounting (always
`requests.`) are two halves of the same always-equal pair.

---

## Test B — filling the quota, then exceeding it by one

### B1: the first 1-GPU pod is admitted

```bash
kubectl apply -f probe-1gpu-1.yaml   # limits.nvidia.com/gpu: "1"
```

```
pod/quota-probe-1gpu-1 created
pod/quota-probe-1gpu-1 condition met
pod=quota-probe-1gpu-1 status=Running node=hive-host-worker2
```

Quota after admission:

```
limits.cpu                                 500m   8
limits.memory                              512Mi  16Gi
requests.nvidia.com/gpu                    1      1
```

**Two things worth noticing.** The GPU is now fully consumed (`1/1`). And
`limits.cpu`/`limits.memory` were charged **even though the pod specified neither** —
those are the values `LimitRange/defaults` injected at admission. Test C below
isolates that behaviour.

### B2: the second identical pod is rejected

```bash
kubectl apply -f probe-1gpu-2.yaml   # identical spec
```

**Rejected. Exit code 1. Verbatim error:**

```
Error from server (Forbidden): error when creating "probe-1gpu-2.yaml": pods
"quota-probe-1gpu-2" is forbidden: exceeded quota: tier-quota, requested:
requests.nvidia.com/gpu=1, used: requests.nvidia.com/gpu=1, limited:
requests.nvidia.com/gpu=1
```

`used=1, limited=1` — the ceiling is exact, not approximate. A tenant gets one GPU
and the second request fails immediately with an explanation.

### Cleanup released the quota

```bash
kubectl delete pod quota-probe-1gpu-1 -n p-inf-101
```

```
limits.cpu                                 0     8
limits.memory                              0     16Gi
requests.nvidia.com/gpu                    0     1
```

Quota accounting follows pod lifetime, so a tenant that deletes a workload
immediately regains its allowance.

---

## Test C — why the LimitRange is mandatory, demonstrated

Not required by the task, but it verifies the claim asserted in
[10-limitrange.yaml](../host/tenancy/inf-103/10-limitrange.yaml): once a quota
constrains `limits.*`, pods that omit resources are **defaulted, not rejected**.

Run in `p-inf-103` with a pod carrying **no `resources` stanza at all**. The
LimitRange was deleted, the pod applied, then the LimitRange restored from its
committed file and the *identical* pod applied again.

**Without the LimitRange — rejected:**

```
Error from server (Forbidden): error when creating "bare-pod.yaml": pods
"limitrange-probe" is forbidden: failed quota: tier-quota: must specify
limits.cpu for: sleeper; limits.memory for: sleeper
```

**With the LimitRange — admitted, and defaulted:**

```
pod/limitrange-probe created
requests={"cpu":"100m","memory":"128Mi"}  limits={"cpu":"500m","memory":"512Mi"}
```

Same pod, opposite outcome. The rule to carry forward:

> **A ResourceQuota on `limits.cpu` / `limits.memory` REQUIRES a LimitRange in the
> same namespace**, or every pod that does not spell out resources is refused —
> including pods you do not author: the vCluster control-plane pod, Knative's
> `queue-proxy` sidecar, KServe's `storage-initializer` init container, and Jobs.

The LimitRange was restored immediately and is present in both tenant namespaces.

---

## Seeded model storage

Both tenants have a `models` PVC seeded by a one-time Job, verified from a pod that
mounts it **read-only**:

```
total 16
drwxrwxrwx    2 root     root          4096 Aug 17 18:21 .
drwxr-xr-x    3 root     root          4096 Aug 17 18:46 ..
-rw-r--r--    1 root     root          5408 Aug 17 18:21 model.joblib
--- readonly check ---
touch: /mnt/models/should-fail: Read-only file system
write correctly refused: mount is readOnly
```

Identical in `p-inf-101` and `p-inf-103`. 5,408 bytes, matching the
`content-length` of the upstream object. The `readOnly: true` mount is genuinely
enforced by the kernel, so a serving pod cannot corrupt the seeded artefact.

---

## What this does and does not prove

**Proved:** the GPU ceiling is exact and enforced at admission; quota accounting
tracks pod lifetime; the LimitRange is load-bearing; tenant storage is seeded and
read-only-mountable.

**Not yet proved — deliberately out of scope for T2:**

- **Isolation.** 0 NetworkPolicies exist. Every pod can still reach every other pod
  across tenants (§2.5 outstanding).
- **Tenant self-service.** No vClusters yet, so nothing tests whether a tenant can
  reach around their own ceiling from inside their own control plane (§2.1). The
  quota is on the host side specifically so they cannot — but that is an argument,
  not yet a test.
- **Lifetime.** No `hive.ksl/expiry` annotation and no reconciler CronJob (§2.6).
