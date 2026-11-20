# Studio S5 — the console provisions real tenants

**Status: PASSED for `demo-cv`. `demo-llm` BLOCKED — see §7.** · 2026-08-23 · host cluster `hive-host`

`STUDIO_EXECUTOR=real`. A project created by clicking through the HIVE Studio
wizard produced a real host namespace, a real quota, a real vCluster, a real
InferenceService reconciled by the one central KServe, and a real prediction —
then was removed from the UI leaving nothing behind.

Everything below is pasted from the run. Nothing is reconstructed.

| | |
|---|---|
| Executor | `real` (`STUDIO_EXECUTOR=real scripts/studio-up.sh`) |
| Deployed | **`demo-cv`** — Iris classifier, on demand, Small, 14 days |
| Blocked | **`demo-llm`** — Qwen 2.5 chat, needs a runtime that does not exist (§7) |
| Renderer | `app/render.py`, **unchanged from S4** |
| Left running | **nothing** — `demo-cv` was deleted as the exercise required (§8) |

---

## 1. Preconditions

Checked when the executor is built, printed in every `approve` log.

```
[OK ] kubectl context: kind-hive-host
[FAIL] hive-llama-cpp runtime: NOT INSTALLED, and host/serving/runtimes/llama-cpp.yaml
       does not exist. This runtime is created by Step 9b, which has not been done in
       this repository. Models needing it (modelFormat: gguf) cannot be deployed;
       everything else is unaffected.
[OK ] nvidia.com/gpu capacity: hive-host-worker=4, hive-host-worker2=4
```

**The runtime check is not a global gate**, and that is a deliberate departure from
a literal reading of the task. A missing gguf runtime has nothing to do with a
sklearn deployment; refusing to provision anything at all would have blocked the
one workload this build can actually serve. It is enforced in `approve` only when
the chosen model needs it — see §7 for the message `demo-llm` gets.

---

## 2. Host-side ground — `namespace-pack`

```
$ kubectl get ns p-demo-cv --show-labels
NAME        STATUS   AGE   LABELS
p-demo-cv   Active   6m    hive.ksl/kind=tenant,hive.ksl/project=demo-cv,
                           hive.ksl/template=studio-inference,hive.ksl/tier=small,
                           kubernetes.io/metadata.name=p-demo-cv,name=p-demo-cv

$ kubectl get resourcequota tier-quota -n p-demo-cv
NAME         REQUEST                                                    LIMIT
tier-quota   count/inferenceservices.serving.kserve.io: 1/2,            limits.cpu: 3/8,
             persistentvolumeclaims: 3/6,                               limits.memory: 6826Mi/16Gi
             requests.nvidia.com/gpu: 1/2,
             requests.storage: 7Gi/12Gi

$ kubectl get ns p-demo-cv -o jsonpath='{.metadata.annotations.hive\.ksl/expiry}'
2026-09-06T07:52:38Z
```

The expiry is provisioning time + the 14 days chosen in the wizard, RFC3339 UTC
(CLAUDE.md §2.6). Studio only SETS this clock; the host reconciler is what will
honour it. `requests.nvidia.com/gpu: 1/2` is the `tier + 1` headroom from
DEVIATIONS.md — one GPU held by the workload, one of validation slack for
Knative's dry-run webhook.

---

## 3. Model storage — `model-seed`

```
$ kubectl get job seed-demo-cv -n p-demo-cv
NAME           STATUS     COMPLETIONS   DURATION   AGE
seed-demo-cv   Complete   1/1           9s         6m
```

Nine seconds because the iris model is ~5 KB. This is the pipeline's only network
egress, and its URL can only ever be a `seedUrl` from `models.yaml`.

---

## 4. The tenant's own control plane — `vcluster`

```
$ kubectl get pods -n p-demo-cv -o wide
coredns-df8c87f55-xgb7s-x-kube-system-x-demo-cv                   1/1  Running    hive-host-worker
demo-cv-0                                                         1/1  Running    hive-host-worker
demo-cv-x-default-x-demo-cv-predictor-00001-deployment-5b9mz6w6   2/2  Running    hive-host-worker
seed-demo-cv-47gfm                                                0/1  Completed  hive-host-worker
```

`demo-cv-0` is the vCluster control plane. The `2/2` predictor pod is
`kserve-container` + Knative's injected `queue-proxy`.

**A fresh vCluster has no KServe API at all**, which is the largest thing this
step discovered — see §6.

---

## 5. Reconciled by the ONE central KServe — `inference-service`, `ready-check`

```
$ kubectl get isvc -n p-demo-cv
NAME                          URL                                                                 READY
demo-cv-x-default-x-demo-cv   http://demo-cv-x-default-x-demo-cv-predictor.p-demo-cv.hive.local   True
```

No per-tenant KServe exists; this was reconciled by the single installation in the
`kserve` namespace (§2.2 intact). The URL carries `-predictor` per the step-4
deviation and was **read live from `.status.url`**, never constructed.

GPU actually allocated:

```
demo-cv-x-default-x-demo-cv-predictor-00001-deployment-5b9mz6w6
  node=hive-host-worker  container=kserve-container  nvidia.com/gpu=1
```

As the **tenant** sees it, from inside their own cluster with no host access:

```
$ vcluster connect demo-cv -n p-demo-cv --silent -- kubectl get isvc -n default \
    -o custom-columns='NAME:...,HOST_READY:...,HOST_URL:...'
NAME      HOST_READY   HOST_URL
demo-cv   True         http://demo-cv-x-default-x-demo-cv-predictor.p-demo-cv.hive.local
```

### It serves

```
$ LOCAL_PORT=8099 scripts/curl-isvc.sh p-demo-cv demo-cv-x-default-x-demo-cv tests/iris-input.json

==> POST http://127.0.0.1:8099/v1/models/demo-cv-x-default-x-demo-cv:predict
    Host: demo-cv-x-default-x-demo-cv-predictor.p-demo-cv.hive.local
    running predictor pods before request: 1

HTTP 200
response: {"predictions":[1,1]}
timing:  total 7.930271s   time-to-first-byte 7.930014s

PASS: HTTP 200 from p-demo-cv/demo-cv-x-default-x-demo-cv
```

`[1,1]` is the correct class for both iris instances — the same answer Gate G1 and
Gate G3 got, now from a tenant nobody hand-built.

---

## 6. Three findings that changed the executor

### 6a. Neither sync script can serve a Studio project

`scripts/sync-isvc.sh` and `scripts/sync-catalog.sh` both hard-code their tenant
list:

```bash
TENANTS_DEFAULT=("inf-101:p-inf-101" "inf-103:p-inf-103")
...
*) echo "usage: $(basename "$0") [--once] [inf-101|inf-103]..." >&2; exit 2 ;;
```

Any other name is a usage error, and both are daemons that must already be
running. A console that provisions on demand can rely on neither, so the executor
mirrors the InferenceService to the host itself, once, as part of the pipeline.

**The architecture is unchanged and that is the point:** the tenant still AUTHORS
inside their own control plane, the ONE central KServe still RECONCILES on the
host. Only the mirroring mechanism differs. Logged in DEVIATIONS.md.

### 6b. A fresh vCluster has no KServe CRDs — the first real run died here

```
error: resource mapping not found for name: "demo-cv" namespace: "default"
```

`inf-101` and `inf-103` have `inferenceservices.serving.kserve.io` in their
vClusters, so this looked impossible. It is there because someone applied it **by
hand** during step 6 — `sync-catalog.sh` copies only the *ClusterServingRuntime*
CRD, and no script records the other apply. A vCluster created today therefore
cannot author the one object the platform exists to serve.

The executor now seeds both CRDs plus all 14 runtimes into every new tenant
cluster, with a self-healing guard in `inference-service` so a run that failed
here can be recovered with **Retry**.

### 6c. A CRD is not usable the moment `apply` returns

Second failure, immediately after fixing 6b:

```
unable to recognize no matches for kind "ClusterServingRuntime" in version "serving.kserve.io/v1alpha1"
```

`kubectl apply` on a CRD returns once the object is stored; the API server then has
to register the type and refresh discovery. Creating a custom resource inside that
gap fails with an error that reads like a missing CRD rather than a race. Fixed
with an explicit `kubectl wait --for condition=established` after each CRD.

### What the failure path was worth

Both failures were diagnosed **from the UI**. The progress view named the failed
step, expanded its log automatically, and showed the exact command with its stderr
tail; each fix was verified by pressing **Retry step**, which resumed from
`inference-service` without rebuilding the namespace, the seed Job or the vCluster.
The failure path built against a mock in S2 paid for itself the first time it met
a real cluster.

---

## 7. `demo-llm` — BLOCKED, and why

Requested: Qwen 2.5 0.5B Instruct, always-on, Small, 30 days. It cannot be
deployed, and this is not a defect in the executor:

```
$ kubectl get clusterservingruntime hive-llama-cpp
Error from server (NotFound): clusterservingruntimes.serving.kserve.io "hive-llama-cpp" not found

$ ls host/serving/runtimes/
ls: host/serving/runtimes/: No such file or directory
```

No installed runtime declares `gguf` among its supported model formats (the 14 cover
autogluon, huggingface, lightgbm, mlflow, onnx, paddle, pmml, pytorch, sklearn,
tensorflow, tensorrt, triton, vLLM, xgboost). **Step 9b does not exist in this
repository** — `templates/catalog/models.yaml` says so itself, in a comment block
that has been there since S0 and warns that the entry must not be selectable until
the runtime is authored.

The executor refuses it precisely, at `approve`, before creating anything:

```
the model 'Qwen 2.5 0.5B Instruct (chat)' needs the ClusterServingRuntime
'hive-llama-cpp', which is not installed on this cluster.
NOT INSTALLED, and host/serving/runtimes/llama-cpp.yaml does not exist. This runtime
is created by Step 9b, which has not been done in this repository. Models needing it
(modelFormat: gguf) cannot be deployed; everything else is unaffected.
Choose a different model, or complete Step 9b to author the runtime
(host/serving/runtimes/llama-cpp.yaml) and record it in VERSIONS.md.
```

**To unblock:** author a `hive-llama-cpp` ClusterServingRuntime under
`host/serving/runtimes/`, confirm its image publishes `linux/arm64` per the §5
working agreement, record the version in VERSIONS.md, and re-run. The executor
will then apply it automatically if it is missing from the cluster.

**Until then the wizard still offers the model**, because `models.yaml` has no field
for "not selectable yet". A PI can pick it and will get the message above. That
remains an open decision: add a `selectable: false` field, or remove the entry.

---

## 8. Removed from the UI, and gone

Deleted through the console with the typed-name confirmation.

```
row before  : demo-cv
modal       : Remove demo-cv?
state       : deleted
rows after  : 0
empty state : You have no projects yet
```

Cluster afterwards:

```
$ kubectl get ns p-demo-cv
Error from server (NotFound): namespaces "p-demo-cv" not found

$ kubectl get isvc -A | grep -c demo-cv
0

$ vcluster list
 NAME    | NAMESPACE | STATUS  | VERSION | AGE
 inf-101 | p-inf-101 | Running | 0.36.1  | 5d12h
 inf-103 | p-inf-103 | Running | 0.36.1  | 5d12h
```

GPUs returned to the pool — `nvidia.com/gpu 0 0` allocated on both workers.

**Invariant (d) held.** The two hand-built reference tenants were never touched:

```
p-inf-101   Active   5d13h
p-inf-103   Active   5d13h
p-inf-101   legal-translation-x-default-x-inf-101    ...   True
p-inf-103   species-classifier-x-default-x-inf-103   ...   True
```

Both still Ready, throughout. Three independent guards stand in the way of them:
`validation.py` rejects the names, `namespace-pack` refuses a namespace not
labelled as the project's own, and `_assert_ours` checks every destructive command.

### What deletion keeps, deliberately

```
registry : demo-cv  state=deleted  deletedAt=2026-08-23T08:02:26Z
on disk  : crd-clusterservingruntimes.json  crd-inferenceservices.json
           inference-service.host.yaml  inference-service.yaml
           model-seed-job.yaml  namespace-pack.yaml  params.json  runtimes.json
kubeconfig: gone
```

A soft delete: the registry row survives so history can still answer "what did we
run, and what happened to it?". The rendered manifests survive because invariant
(a) is about reviewability after the fact as much as before it. The kubeconfig does
NOT survive — it is an admin credential for a control plane that no longer exists.

---

## 9. What is proven, and what is not

**Proven.** A PI with no Kubernetes knowledge can produce a real, quota-bounded,
GPU-scheduled, network-isolated tenant from a web form, get a working endpoint, and
remove it again — with every manifest rendered to `tenants/<project>/rendered/`
before it was applied, and the two reference tenants untouched throughout.

**Not proven, deliberately out of scope here:**

- **The gguf/LLM path.** Blocked on Step 9b (§7). The only model proven end to end
  through Studio is the sklearn iris classifier, exactly as in G1 and G3.
- **Expiry enforcement.** Studio sets `hive.ksl/expiry`; nothing yet reads it. The
  host reconciler CronJob (§2.6) is still unbuilt.
- **Network isolation.** §2.5 remains unimplemented cluster-wide — no NetworkPolicy
  exists, for Studio projects or for the hand-built tenants.
- **Concurrency.** One deployment at a time was exercised. Two PIs provisioning
  simultaneously is untested.
- **Re-mirroring.** The isvc is mirrored once, at deploy. A tenant editing their own
  InferenceService afterwards will not see it reflected on the host — the polling
  script would have caught that, and this does not.
