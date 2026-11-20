# Studio S6 — sleep, wake, and live status

**Status: PASSED** · 2026-08-23 · host cluster `hive-host` · `STUDIO_EXECUTOR=real`

The archetype moment, seen from the console: an on-demand project releases its GPU
when nobody is using it, and the next request wakes it. This is the thing the T-SZ
archetype exists to do, and the thing a PI most needs the UI to explain rather than
hide.

| | |
|---|---|
| Project | **`demo-cv`** — Iris classifier, **on demand**, Small, 14 days |
| Provisioned by | the Studio wizard, real executor (S5) |
| Data path | one supervised `kubectl port-forward svc/kourier 18080:80` |
| Measured cold start | **8.5 s**, three runs: 9.5 s · 9.4 s · 8.5 s |
| Warm | sub-second |

---

## 1. The timeline

Captured from the shell against the live API, one line per poll, with the invoke
running concurrently.

```
waiting for demo-cv to fall asleep…
SLEEPING at 08:20:44Z  (sleeping 0)

TIME  STATUS      PODS
t+00s  waking 0
t+02s  ready 1
t+04s  ready 1
t+05s  ready 1
t+07s  ready 1
t+09s  ready 1   <- request returned

RESULT: {"status":200,"latencyMs":8460,"coldStart":true,"body":{"predictions":[1]}}
```

Read that in order, because each line is a different fact:

- **`sleeping 0`** — the project is Ready on the host and has **zero** predictor
  pods. Its GPU is back in the shared pool. Nothing is wrong.
- **`waking 0`** — a request is in flight against a project with no pods. This
  state is held **in memory only**, for the duration of the request: it is a fact
  about something happening right now, not a property of the project, and it must
  not survive a restart or outlive the call that caused it.
- **`ready 1`** at t+02s — Knative has scheduled a pod. The request is still
  waiting: the pod exists but the runtime has not finished loading the model.
- **t+09s** — the answer arrives. `latencyMs: 8460`, `coldStart: true`.

The gap between `ready 1` at t+2s and the answer at t+9s is the honest shape of a
cold start, and it is why the pill alone is not enough — a PI watching only the
status would think it was ready five seconds before it could answer. The
playground says "Waking your deployment — this one takes longer" for exactly that
window.

---

## 2. What the console showed

### Sleeping

`Overview` while idle — status pill, microcopy, zero running copies, and a quota
panel showing the GPU genuinely released:

```
demo-cv                        [ Sleeping ]

Status
  Sleeping     [Refresh]
  Idle, holding no GPU — wakes on first request, in about 15 seconds.
  This is what "on demand" means and it is working correctly.
  Running copies                 0
  Created                        2026-08-23 08:10:15 UTC
  Template                       inference-endpoint

Time remaining
  ( 13 days )   Removed automatically on 2026-09-06
  ▲ Expiry is recorded but not yet enforced. Automatic removal arrives with
    the platform TTL service; today the date is written on your project and
    nothing acts on it.

What you can use at once
  GPUs      ▏                                        0 / 2
  CPU       ████▏                                 1500m / 8
  Memory    ██████▏                             4266Mi / 16Gi
  Storage   ████████████████▏                       7Gi / 12Gi
```

**`GPUs 0 / 2` is the whole point.** A sleeping project holds nothing. The CPU and
memory still in use belong to the vCluster control plane, which stays up — the
tenant's own Kubernetes API does not go away when their model sleeps.

### Waking, then answered

`Playground`, prefilled with the model's own known-good `sampleInput`:

```
Request                          Response   9.5s · cold start
{                                HTTP 200
  "instances": [
    [ 6.8, 2.8, 4.8, 1.4 ]       {
  ]                                "predictions": [ 1 ]
}                                }
```

The latency chip is **amber**, deliberately: a cold start is a consequence of a
local shortcut the PI was warned about when they chose "on demand", and amber is
this console's one reserved signal for "this is not how production works".

---

## 3. Status is derived, never remembered

Invariant (b) is the reason this page can be trusted, so it is worth stating what
each word is computed from. Nothing below is read from SQLite.

| Shown | Where it comes from |
|---|---|
| `ready` / `sleeping` | `.status.conditions[Ready]` on the host isvc, **plus** a live count of Running predictor pods |
| Running copies | pods in `p-demo-cv` whose name starts with the synced isvc's `-predictor` |
| Endpoint | `.status.url`, read live — never constructed (`-predictor` is a local artefact) |
| Quota bars | `kubectl get resourcequota tier-quota -o json` |
| Days remaining | the `hive.ksl/expiry` annotation on the namespace |
| `waking` | in-memory, for the life of one request |

Two consequences worth spelling out:

**An always-on project with zero pods is `failed`, not `sleeping`.** T-AO exists to
hold a replica; reporting "sleeping" would hide the one failure it is supposed to
prevent. The distinction is in `live.derive_status` and depends on the project's
own `availability`.

**`daysLeft` now comes from the namespace, not from arithmetic.** S2 computed it
from `createdAt + durationDays` because there was no cluster to ask. That fallback
still exists for mock mode, but in real mode the annotation is authoritative — two
countdowns that can disagree is worse than one that is occasionally coarse.

---

## 4. The data path

One `kubectl port-forward`, owned and supervised by the backend:

```
$ curl -s localhost:8000/api/health
{
  "ok": true,
  "executor": "real",
  "dataPath": { "ready": true, "port": 18080,
                "detail": "port-forward svc/kourier -n kourier-system 18080:80",
                "restarts": 0 }
}
```

**The health probe sends a real request**, and accepts Kourier's 404 as success:

```
$ curl -o /dev/null -w '%{http_code}' -H 'Host: nothing.invalid' http://127.0.0.1:18080/
404
```

That 404 is the proof — Kourier answers it only if the tunnel is up and the gateway
is reachable. `kubectl port-forward` frequently stays alive with a dead tunnel
underneath, so "the child process exists" is not evidence of anything.

**Port 18080, and the browser never learns it.** CLAUDE.md §6 fixes the port so it
cannot collide with a port-forward a PI is running by hand, and the playground
calls through the BFF so the browser never sees a URL shape that will not exist in
production. The curl snippet the Overview offers deliberately shows `127.0.0.1:8080`
— the port the *PI* would forward themselves — not Studio's internal one.

---

## 5. The other surfaces, verified

```
kubeconfig   HTTP 200, content-disposition: attachment; filename="demo-cv-kubeconfig.yaml"
             valid kubeconfig, 18 lines
events       110 entries merged — 80 pipeline, 30 cluster
rename       displayName "Coral survey — summer run"; id still demo-cv;
             namespace p-demo-cv Active and untouched
renewal      recorded 2026-08-23T08:19:05Z
```

**Rename is registry-only, on purpose.** The project id is the namespace, the
vCluster name and the endpoint hostname; changing it would mean destroying and
rebuilding the project. The label is what a PI actually wants to change, and the
Settings tab says so where it is edited.

**Renewal grants nothing.** Extending a project holds scarce shared hardware for
longer, which is a decision about other people's access. Studio records the ask;
the toast says "routed to the Research Committee in production" rather than
implying an approval queue exists locally.

---

## 6. Two bugs the screenshots caught

Neither would have failed a build or an API check; both were visible only on screen.

1. **The playground response rendered as pale boxes on near-black.** The global
   `code` rule paints a light chip background — correct in prose, wrong inside a
   dark pane. The `.yaml` block already had the reset; `.pg__json` did not.
2. **"Response9.5s · cold start"** — the label and its latency chip had no gap.

A third, from earlier in the step and more consequential: `kubectl wait --for
condition=established` does not merely fail to wait when a CRD's status is still
empty, it **errors out**:

```
error: .status.conditions accessor error: <nil> is of the type <nil>, expected []interface{}
```

It waits for a condition to become *true*; it does not wait for the conditions
list to *exist*. The S5 fix that introduced it was therefore incomplete, and cost
`demo-cv` a failed `vcluster` step before it was caught. It is now a poll that
treats "no status yet" as "not ready yet".

---

## 7. What is proven, and what is not

**Proven.** An on-demand project releases its GPU when idle and recovers on demand
in ~8.5 s; the console reports each state from the cluster rather than from memory;
a PI can call their own model from the browser without learning curl, a Host header
or a port-forward.

**Not proven:**

- **The chat playground.** Built, but no chat model can be deployed — `hive-llama-cpp`
  does not exist (see `docs/studio-real-proof.md` §7). The chat path has never been
  exercised against a live endpoint.
- **Expiry enforcement.** Still only recorded. The amber note on the Overview says so.
- **Streaming.** The invoke proxy sends `stream: false`. A streamed reply needs an
  SSE relay through the BFF.
- **Re-mirroring.** A tenant editing their own InferenceService is still not
  reflected on the host — the mirror happens once, at deploy.
- **Concurrency.** One project, one viewer. Several PIs invoking at once is untested,
  and they would share the single port-forward.
