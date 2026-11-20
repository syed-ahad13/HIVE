# Gate G1 — serving spine smoke test

**Status: PASSED** · 2026-08-17 · host cluster `hive-host`

G1 proves the central serving spine works end to end, before any tenancy exists:
an `InferenceService` becomes a running, routable, autoscaling service that
returns a correct prediction and scales to zero when idle.

Tested in the `default` namespace deliberately — G1 is a platform gate, not a
tenant test. Tenant namespaces (`p-inf-*`) arrive in a later step.

---

## Versions under test

| Component | Version |
|---|---|
| Kubernetes (kind `hive-host`) | v1.36.1 |
| Calico | v3.32.1 |
| cert-manager | v1.21.1 |
| Knative Serving | knative-v1.23.0 |
| net-kourier | knative-v1.23.0 |
| KServe | v0.20.0 |
| Deployment mode | `Serverless` (Knative) |
| Serving runtime selected | `kserve-sklearnserver` → `kserve/sklearnserver:v0.20.0` |
| Runtime architecture | **arm64 native** (`uname -m` inside the container → `aarch64`) |

---

## What ran

```
host/serving/smoke/sklearn-iris.yaml     InferenceService, sklearn, minReplicas 0, no GPU
tests/iris-input.json                    {"instances": [[6.8,2.8,4.8,1.4],[6.0,3.4,4.5,1.6]]}
scripts/curl-isvc.sh                     port-forward + Host-header prediction request
```

```bash
kubectl apply -f host/serving/smoke/sklearn-iris.yaml
kubectl wait --for=condition=Ready inferenceservice/sklearn-iris -n default --timeout=600s
scripts/curl-isvc.sh default sklearn-iris tests/iris-input.json
```

### Prediction result

```
==> POST http://127.0.0.1:8080/v1/models/sklearn-iris:predict
    Host: sklearn-iris-predictor.default.hive.local

HTTP 200
response: {"predictions":[1,1]}
```

`[1,1]` is the expected class for both iris instances. The same body returned the
same predictions on every request, warm and cold.

---

## Measurements

| Request | Pods before | Time to 200 |
|---|---|---|
| Cold start (1st) | 0 | **13.98 s** |
| Cold start (2nd, independent) | 0 | **15.10 s** |
| Warm | 1 | 0.011 s |
| Warm | 1 | 0.055 s |

**Cold start ≈ 14–15 s. Warm ≈ 11–55 ms.** Roughly a 300–1300× difference.

Timings are curl's own `%{time_total}` / `%{time_starttransfer}` counters, so they
measure the request itself and exclude port-forward setup. In every cold case
`time_starttransfer` ≈ `time_total`: the entire cost is waiting at the Knative
activator for a pod, not transferring the response.

The cold start covers: activator holds the request → autoscaler scales 0→1 →
pod scheduled → `storage-initializer` init container pulls the model from GCS →
`sklearnserver` boots and loads it → queue-proxy reports ready → request forwarded.

### Scale-to-zero observed

Knative defaults were used for this gate (`stable-window` 60 s +
`scale-to-zero-grace-period` 30 s):

```
t+0s    pods=1  sklearn-iris-predictor-00002-...-598mg  2/2  Running
t+31s   pods=1  sklearn-iris-predictor-00002-...-598mg  2/2  Terminating
t+62s   pods=0  <none>
=== SCALED TO ZERO at t+62s after last request ===
```

Tenant T-SZ templates will pin `window` and `scale-down-delay` to 60 s explicitly
per CLAUDE.md §2.7 (production: 30 m) rather than relying on these defaults.

---

## Two findings that changed the build

### 1. KServe's advertised URL 404'd — it assumes Istio

The first request returned **HTTP 404 in 8 ms without waking a pod**, while the
`InferenceService` reported `READY=True`.

Cause: KServe ships `ingress.disableIstioVirtualHost: false`. In that mode it
derives the isvc URL by taking the predictor's Knative route and **deleting the
`-predictor` segment**:

```
real, served by Kourier:   sklearn-iris-predictor.default.hive.local
published in .status.url:  sklearn-iris.default.hive.local     <- nothing serves this
```

With Istio, a `VirtualService` maps the shortened host onto the predictor. HIVE-LOCAL
runs Kourier and has no Istio, and KServe *deliberately skips* VirtualService
reconciliation when the CRD is absent — so the URL it advertises is unroutable and
nothing reports unhealthy.

Fix: [host/serving/kserve/ingress-config-patch.yaml](../host/serving/kserve/ingress-config-patch.yaml)
sets `disableIstioVirtualHost: true`, making KServe publish the predictor route
unchanged. Applied and asserted by `scripts/install-serving.sh`, which now fails
loudly if the flag is not set.

**Migration consequence:** local URLs carry the `-predictor` segment; production
(Istio/Kong) will serve the shorter `<isvc>.<ns>.<domain>` form. Anything that
hard-codes a hostname must read `.status.url` instead. Logged in DEVIATIONS.md.

### 2. Three runtimes claim `sklearn`, and the default pick is amd64-only

| Runtime | autoSelect | priority | protocols | arch |
|---|---|---|---|---|
| `kserve-mlserver` | true | 2 | v2 only | **amd64 only** |
| `kserve-predictiveserver` | false | 3 | v1, v2 | arm64 |
| `kserve-sklearnserver` | true | 1 | v1, v2 | arm64 |

Priority semantics are "higher value wins", so `kserve-mlserver` **outranks**
`kserve-sklearnserver` — a v2 request would have selected an amd64-only image and
run the model under Rosetta emulation.

Declaring `protocolVersion: v1` on the predictor makes mlserver ineligible (it
supports v2 only), deterministically selecting the arm64-native
`kserve-sklearnserver`. Verified: `uname -m` inside the running container returns
`aarch64`, so **no emulation is involved in G1** and the timings above are native.

---

## Canary

`sklearn-iris` is left running as the standing canary. It costs nothing while idle
(scaled to zero) and is the fastest check that the spine still works after any
change:

```bash
scripts/curl-isvc.sh default sklearn-iris tests/iris-input.json
```

Expect `HTTP 200` and `{"predictions":[1,1]}` — in ~15 s cold, ~50 ms warm.

If it fails after a machine restart, check the fake GPUs first
(`scripts/fake-gpus.sh`); a kubelet restart wipes node-status patches, though G1
itself requests no GPU.
