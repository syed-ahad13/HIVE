# The seven RAG manifests — why each one exists

A `rag-stack` deployment declares **eight** resources and writes **seven** files.
`vcluster` is the missing one: a vCluster is not created by applying a manifest,
so it is a pipeline step with no fragment behind it — the one imperative call in
the pipeline.

Each fragment carries its own full justification in its header. **Those headers
are the source of truth**; this page is the index, for answering "why is that a
separate file?" without opening seven of them.

| # | File | Creates | Applied to |
|---|---|---|---|
| 1 | `namespace-pack.yaml` | Namespace, ResourceQuota, LimitRange, **3** PVCs | host |
| 2 | `model-seed.yaml` | Job | host |
| 3 | `qdrant.yaml` | Deployment, Service | host |
| 4 | `corpus-staging-pod.yaml` | Pod | host |
| 5 | `ingestion-job.yaml` | Job | host |
| 6 | `inference-service.yaml` | InferenceService | **the tenant's vCluster** |
| 7 | `rag-api.yaml` | Deployment, Service | host |

**Six of seven land on the host.** Only the chat model is authored inside the
PI's own vCluster, and the reason is not preference: a pod authored inside a
vCluster **cannot mount the existing host PVCs**, because the syncer maps a
virtual claim to a *new* host claim. An in-vCluster ingestion Job would index an
empty volume and report success — silent, which is what drove the decision.

---

## Why each is separate

The split is not tidiness. **A file is separate when it is applied at a different
moment, or removed at a different moment, or both** — because the pipeline
applies them one step at a time and `teardownOrder` removes them in an order that
is not the reverse of creation.

### 1. `namespace-pack.yaml` — the ground everything else needs

Must be first, and nothing else can be admitted without it. Its **LimitRange is
what makes every later pod admissible at all**: once the quota sets
`limits.cpu`/`limits.memory`, Kubernetes rejects any container that does not
state both, and that includes things the PI never wrote — the vCluster control
plane, Knative's queue-proxy, KServe's storage-initializer.

Three PVCs, three different jobs, and they are separate claims rather than one:

* `models` — written once by the seed Job, mounted **read-only** by everything else
* `scratch` — the PI's corpus: written by the staging pod, read by ingestion, never touched again
* `qdrant-data` — the live index, the only claim a **long-lived** pod writes to

`scratch` and `qdrant-data` are not merged because they are ReadWriteOnce on
local-path: sharing one would put a pod that must *stay up* and a pod that must
*finish* on the same single-writer volume.

The quota arithmetic, confirmed against a real run: **9Gi of 10Gi** used
(models 1 + scratch 1 + qdrant-data 2 + the vCluster's own etcd claim 5), and
**4 PVCs of 6**.

### 2. `model-seed.yaml` — the only network egress in the entire pipeline

One Job that fetches **both** models: the chat GGUF by HTTPS, and the embedding
model's ONNX cache through fastembed.

**One Job and not two, deliberately.** The pipeline has one `model-seed` step and
that step must be able to say "the models are there" or "they are not". Two Jobs
would make a half-finished step indistinguishable from a failed one, and a retry
would have to reason about which half to redo.

It is separate from everything else because it is **the governance boundary**.
Every byte this project will ever fetch from the internet is fetched here, from a
URL that can only come from `models.yaml`. Everything downstream mounts `models`
**read-only** and loads with `local_files_only=True`, so a query path that
somehow reached the network could not use it.

### 3. `qdrant.yaml` — the first thing HIVE runs for a tenant that is not an InferenceService

A **Deployment, not a StatefulSet**, and that is a real decision. A StatefulSet
buys stable per-replica identity, ordered rollout, and a `volumeClaimTemplate`.
This workload wants none of them:

* **one replica, always** — Qdrant clusters by Raft; a second replica without cluster config is two databases with one name, not high availability
* the **PVC is declared in the namespace pack on purpose**, so the tenant's quota counts it. A `volumeClaimTemplate` creates claims *outside* the pack — invisible in the manifest a reviewer reads, and **not deleted when the StatefulSet is**

`strategy: Recreate` is load-bearing: the PVC is ReadWriteOnce, so a rolling
update would start the new pod before releasing the volume and stall on
`Multi-Attach error` with no pod log to explain it.

### 4. `corpus-staging-pod.yaml` — because there is no other way in

**A PersistentVolumeClaim is not writable from outside the cluster.** `kubectl cp`
copies into a *container*, not a volume, so getting a PI's documents onto
`scratch` needs a running container with `scratch` mounted for as long as the
copy takes. That is this pod, and its lifetime is exactly the copy — the same
step creates it, copies, verifies the file list *inside* it, and deletes it.

**Why not copy into the ingestion Job instead?** Because the ingestion Job starts
running its program immediately; there is no window in which it is up, idle, and
waiting to be copied into. Ordering demands a container that does nothing.

The trap in this file: `kubectl cp` is `tar cf - | kubectl exec -i -- tar xf -`,
so **the target container must have `tar`**. Without it you get `tar: not found`
and an exit status that reads like a permissions problem. busybox has a `tar`
applet — verified by running it, not assumed.

This is also why `corpus-upload` is **absent from `teardownOrder`**: the pod is
already gone by the time teardown runs, and a teardown step that always finds
nothing reads like a bug.

### 5. `ingestion-job.yaml` — chunk, embed, upsert

Separate from the seed Job because it runs at a different time and can fail for
entirely different reasons: the seed is a download, this is CPU work against a
Qdrant that must already be up and a corpus that must already be staged. It is
the step whose **output is the interesting part** — the `INGEST_RESULT` line
recording files, chunks and dimensions.

A Job rather than a Deployment because it **must finish**. Its completion is what
the pipeline waits on before starting the RAG service, so the service comes up
against a populated index and its first health check is honest.

### 6. `inference-service.yaml` — the architecture claim, in one file

The only fragment applied **inside the PI's own vCluster**, and the only one that
matters architecturally: **the tenant authors it, the ONE central KServe on the
host reconciles it.** That is CLAUDE.md §2.1 and §2.2 made concrete, and it is
identical in shape to the hand-written `tenants/inf-101/legal-translation.yaml`.

It is separate because it is the only file with a *different destination*. The
executor also derives a host mirror from it (`inference-service.host.yaml`) —
that is generated, not authored, which is why there is no eighth fragment.

### 7. `rag-api.yaml` — the endpoint the PI actually calls

Last, because it depends on all four things above: Qdrant to search, the index to
search *in*, the models volume to embed with, and the chat model's **live
hostname**, which only `ready-check` can read. It cannot be rendered until the
step before it has run.

It is why the template declares `interface: rag`: the endpoint a PI calls is not
the model, it is this service in front of it.

---

## Teardown removes them in a different order

`rag-api → ingestion → vector-db → inference-service → vcluster → namespace-pack`

Not the reverse of creation, and each position is load-bearing:

* **rag-api first** — the only thing holding open connections to the other two
* **ingestion second** — it *writes* to Qdrant and may still be running on a failed deploy
* **vector-db third** — before its PVC goes with the namespace
* **inference-service** — releases the GPU while the control plane that owns it is still up
* **vcluster** — deleting its host namespace first would strand the StatefulSet and its etcd PVC
* **namespace-pack last** — takes the quota, the LimitRange and all three PVCs, and therefore the PI's documents, with it

---

## Two extra files in `tenants/<project>/rendered/`

Nine files land on disk, not seven. The other two are not fragments:

| File | What it is |
|---|---|
| `inference-service.host.yaml` | the host mirror, **derived** from #6 by the executor — the local stand-in for a vCluster Pro syncer (DEVIATIONS.md) |
| `params.json` | a snapshot of exactly what the PI asked for, written at `approve`, so "what did this person request?" is answerable from the filesystem alone |

And a `kubeconfig`, written at the vCluster step and **deleted at teardown** —
a credential for a control plane that no longer exists is only a liability.
