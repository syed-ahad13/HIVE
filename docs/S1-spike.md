# S1 — vCluster sync contract spike

**Status: PASSED, via fallback A + B** · 2026-08-18 · host cluster `hive-host`

The architecture contract works end to end for both tenants:

> the tenant **AUTHORS** an InferenceService inside their own cluster;
> the **ONE** central KServe on the host **RECONCILES** it;
> readiness and URL flow **back** to the tenant.

The *mechanism* is not the intended one. vCluster's custom-resource sync is a paid
feature, so both directions run on scripted stand-ins. Details below.

| | |
|---|---|
| vcluster CLI | v0.36.1 (Homebrew) |
| vCluster chart | `vcluster-0.36.1`, app 0.36.1, virtual k8s v1.36.0 |
| Tenants | `inf-101` in `p-inf-101` · `inf-103` in `p-inf-103` |
| Templates | `templates/t-ao-values.yaml` · `templates/t-sz-values.yaml` |
| Catalogue sync | **fallback A** — `scripts/sync-catalog.sh` |
| InferenceService sync | **fallback B** — `scripts/sync-isvc.sh` |

---

## The blocker: custom-resource sync is vCluster Pro

Both required flows are the same paid feature, `Generic Sync`
(`vcp-distro-generic-sync`). Open-source v0.36.1 **accepts the configuration and
reports a successful `vcluster create`**, then the control-plane pod crash-loops.

`sync.toHost.customResources` (InferenceService, virtual → host):

```
error: start managers: init syncers: create pro syncers: register to host syncer
inferenceservices.serving.kserve.io: you are trying to use a vCluster pro feature
'Generic Sync' (vcp-distro-generic-sync) that is not available in the open source
vcluster or disabled in your license. Please use the vCluster pro image and specify
a license that allows using this feature or reach out to support@loft.sh
```

`sync.fromHost.customResources` (ClusterServingRuntime, host → virtual) — verified
**independently**, by upgrading with *only* the fromHost stanza:

```
error: start managers: init syncers: create pro syncers: register to host syncer
clusterservingruntimes.serving.kserve.io: you are trying to use a vCluster pro
feature 'Generic Sync' (vcp-distro-generic-sync) that is not available in the open
source vcluster or disabled in your license.
```

This is a licence gate, not a bug, so the fallback ladder was taken rather than
debugging further. The intended configuration is kept — commented out — in both
template files, so those files are the migration instruction.

### Two other blockers hit on the way

**`--add=false` is required.** `vcluster create` defaults to registering with
vCluster Platform:

```
fatal you have vCluster pro features enabled, but seems like you are not logged in
(create management client: not logged in ...). Please make sure to log into vCluster
Platform ... or run this command with --add=false
```

**The tenant ResourceQuota blocked the control plane.** vCluster provisions a 5Gi
etcd PVC *inside the tenant's host namespace*:

```
Create Pod inf-101-0 in StatefulSet inf-101 failed error: failed to create PVC
data-inf-101-0: persistentvolumeclaims "data-inf-101-0" is forbidden: exceeded
quota: tier-quota, requested: requests.storage=5Gi, used: requests.storage=2Gi,
limited: requests.storage=5Gi
```

Step 5's quota allowed 5Gi total with 2Gi already used by `models` + `scratch`.
Raised to `requests.storage: 12Gi` and `persistentvolumeclaims: 6` in both tenants.

**The failure shape is the notable part:** the StatefulSet retries forever and *no
pod is ever created*, so `kubectl get pods` is simply empty — nothing Pending, nothing
to describe. The explanation exists only in the StatefulSet's events. After fixing the
quota, the StatefulSet is in exponential backoff; `kubectl scale sts inf-101
--replicas=0` then `--replicas=1` forces an immediate retry.

---

## Both control planes running

```
$ kubectl get pods -n p-inf-101
NAME                                              READY   STATUS      AGE
coredns-df8c87f55-7stcd-x-kube-system-x-inf-101   1/1     Running     2m13s
inf-101-0                                         1/1     Running     2m49s
seed-models-rnpbc                                 0/1     Completed   94m

$ kubectl get pods -n p-inf-103
NAME                                              READY   STATUS      AGE
coredns-df8c87f55-vdgdj-x-kube-system-x-inf-103   1/1     Running     7s
inf-103-0                                         1/1     Running     2m46s
seed-models-ll8nn                                 0/1     Completed   94m
```

The synced coredns pod shows vCluster's host name-translation scheme,
`<name>-x-<namespace>-x-<vcluster>`. `scripts/sync-isvc.sh` deliberately reuses that
convention for mirrored InferenceServices, so a host-side reader can tell at a glance
where an object came from.

---

## 4a — the host catalogue is visible inside the vCluster

```
$ vcluster connect inf-101 -n p-inf-101 -- kubectl get clusterservingruntimes
NAME                                 DISABLED   MODELTYPE     CONTAINERS         AGE
kserve-autogluonserver                          autogluon     kserve-container   2m22s
kserve-huggingfaceserver                        huggingface   kserve-container   2m22s
kserve-huggingfaceserver-multinode              huggingface   kserve-container   2m22s
kserve-lgbserver                                lightgbm      kserve-container   2m22s
kserve-mlserver                                 sklearn       kserve-container   2m22s
kserve-paddleserver                             paddle        kserve-container   2m22s
kserve-pmmlserver                               pmml          kserve-container   2m22s
kserve-predictiveserver                         sklearn       kserve-container   2m22s
kserve-sklearnserver                            sklearn       kserve-container   2m22s
kserve-tensorflow-serving                       tensorflow    kserve-container   2m22s
kserve-torchserve                               pytorch       kserve-container   2m22s
kserve-tritonserver                             tensorrt      kserve-container   2m22s
kserve-vllmserver                               vLLM          kserve-container   2m22s
kserve-xgbserver                                xgboost       kserve-container   2m22s
```

14 runtimes in each tenant, matching the host exactly:

```
inf-101: 14 runtimes
inf-103: 14 runtimes
host:    14
```

> **Counting gotcha:** `vcluster connect` prints a `done vCluster is up and running`
> banner to **stdout**, so `... | wc -l` reports 15. Any script parsing this output must
> filter the banner (`grep '^kserve-'`, or `sed -n '/^{/,$p'` before a JSON parse).

---

## 4b — the full round trip, tenant `inf-101`

### (i) tenant authors inside their own cluster

```
$ vcluster connect inf-101 -n p-inf-101 -- kubectl apply -f tests/sync-probe-isvc.yaml
inferenceservice.serving.kserve.io/sync-probe created

$ vcluster connect inf-101 -n p-inf-101 -- kubectl get isvc -n default
NAME         URL   READY   ...   AGE
sync-probe                       2s
```

No status yet — correct, the host has not seen it.

### (i cont.) the mirror appears in the host namespace

```
$ scripts/sync-isvc.sh --once inf-101
==> inf-101 (host namespace p-inf-101)
    host p-inf-101/sync-probe-x-default-x-inf-101: created
    virtual default/sync-probe: ready=Unknown url=<none>
```

**Exact synced name, as recorded:**

```
HOST_NAME                        VCLUSTER   VIRT_NS   VIRT_NAME
sync-probe-x-default-x-inf-101   inf-101    default   sync-probe
```

For `inf-103` it is `sync-probe-x-default-x-inf-103`.

### (ii) it becomes READY on the host

```
  t+10s  READY=<none>  pods=1
  t+20s  READY=<none>  pods=1
  t+30s  READY=True    pods=1

NAME                             URL                                                                    READY
sync-probe-x-default-x-inf-101   http://sync-probe-x-default-x-inf-101-predictor.p-inf-101.hive.local   True
```

Reconciled by the **one central KServe** in the `kserve` namespace — no per-tenant
KServe exists (§2.2 intact). The runtime it selected:

```
kserve-container: index.docker.io/kserve/sklearnserver@sha256:2838a5d7…
queue-proxy:      gcr.io/knative-releases/knative.dev/serving/cmd/queue@sha256:a3cc71ce…
```

`kserve/sklearnserver` is the **arm64-native** runtime. The probe does not set
`protocolVersion`, so this also confirms KServe's default is `v1` — which is what
excludes the amd64-only `kserve-mlserver` despite its higher priority (the step-4
finding, now verified rather than assumed).

### (iii) READY and URL propagate back inside the vCluster

```
$ scripts/sync-isvc.sh --once inf-101
    host p-inf-101/sync-probe-x-default-x-inf-101: unchanged
    virtual default/sync-probe: ready=True url=http://sync-probe-x-default-x-inf-101-predictor.p-inf-101.hive.local
```

As the **tenant** sees it, inside their own cluster, with no host access:

```
NAME         HOST_READY   HOST_URL
sync-probe   True         http://sync-probe-x-default-x-inf-101-predictor.p-inf-101.hive.local
```

Full annotation set available to the tenant:

```
hive.ksl/host-name              = sync-probe-x-default-x-inf-101
hive.ksl/host-namespace         = p-inf-101
hive.ksl/host-ready             = True
hive.ksl/host-status-synced-at  = 2026-08-17T20:01:22+00:00
hive.ksl/host-url               = http://sync-probe-x-default-x-inf-101-predictor.p-inf-101.hive.local
```

### (iv) deleting it inside garbage-collects the host copy

```
$ vcluster connect inf-101 -n p-inf-101 -- kubectl delete isvc sync-probe -n default
inferenceservice.serving.kserve.io "sync-probe" deleted from default namespace

$ scripts/sync-isvc.sh --once inf-101
    host p-inf-101/sync-probe-x-default-x-inf-101: DELETED (virtual source gone)

$ kubectl get isvc -n p-inf-101
No resources found in p-inf-101 namespace.

$ kubectl get ksvc -n p-inf-101
No resources found in p-inf-101 namespace.
```

The Knative Service went too — KServe owns it by `ownerReference`, so deleting the
mirrored InferenceService cascades correctly. Garbage collection is not just deleting
the mirror; it unwinds the whole reconciled tree.

---

## 4b repeated for tenant `inf-103`

```
runtimes visible: 14
inferenceservice.serving.kserve.io/sync-probe created
    host p-inf-103/sync-probe-x-default-x-inf-103: created
    virtual default/sync-probe: ready=Unknown url=<none>
    t+10s READY=<none>
    t+20s READY=<none>
    t+30s READY=<none>
    READY=True at t+40s
    sync-probe-x-default-x-inf-103 READY=True
    virtual default/sync-probe: ready=True url=http://sync-probe-x-default-x-inf-103-predictor.p-inf-103.hive.local
inferenceservice.serving.kserve.io "sync-probe" deleted from default namespace
    host p-inf-103/sync-probe-x-default-x-inf-103: DELETED (virtual source gone)
No resources found in p-inf-103 namespace.
```

Identical behaviour, independent tenant, independent host namespace.

---

## What is proven, and what the fallbacks cost

**Proven:**

- Two tenant vClusters run inside their host namespaces, under the tenant quota.
- A tenant authors an ordinary `InferenceService` with ordinary KServe API syntax.
- The one central host KServe reconciles it to READY with a URL.
- Readiness and URL are visible to the tenant without host access.
- Deleting inside cascades: mirror, Knative Service, and pods all go.
- The host runtime catalogue is visible in both vClusters, and matches the host.

**What the fallbacks are weaker at — the migration checklist:**

| Aspect | Real vCluster sync | Fallback here |
|---|---|---|
| Trigger | watch, event-driven | **poll**, default 10s, so lag in both directions |
| Status | real `.status` on the virtual object | `hive.ksl/host-*` **annotations** |
| Validation | KServe webhook sees the virtual object | webhook is host-side only; bad specs rejected **late**, at mirror time |
| Lifecycle | inside the vCluster control plane | **an external script; if it is not running, nothing syncs** |
| Catalogue read-only | enforced by the syncer | **convention only** — a tenant is cluster-admin inside their vCluster and can edit the copies; the next run overwrites them |

The last row is the one to re-verify after migration: it is the only place where a
tenant can currently do something the architecture says they should not.

---

## Operating notes

```bash
# publish the host catalogue into both vClusters (re-run when host runtimes change)
scripts/sync-catalog.sh

# one reconcile pass
scripts/sync-isvc.sh --once

# continuous, while developing tenant workloads
scripts/sync-isvc.sh                  # 10s; INTERVAL=5 to tighten
```

`scripts/sync-isvc.sh` also bootstraps the `inferenceservices.serving.kserve.io` CRD
into each vCluster on first run — without it the tenant's API server does not know the
kind, and real toHost sync would have copied the definition automatically.
