# RAG stack — the real thing

**Status: PASSED.** · 2026-08-31 · host cluster `hive-host` · `STUDIO_EXECUTOR=real`

The first end-to-end `real` deployment of `rag-stack`. Everything below is pasted
from the run; nothing is reconstructed.

Until this run every RAG component had been verified in isolation — the toolkit
against a real Qdrant and a real llama-server in containers, the pipeline in mock
— and the whole had never been assembled on the cluster. This is that.

| | |
|---|---|
| Project | **`ksl-docs`** — always-on, Small, 30 days, topK 3 |
| Corpus | 4 files, 8,246 bytes, uploaded through `POST /api/uploads` |
| Chat model | `qwen2.5-0.5b-instruct-q4` on `hive-llama-cpp-rag` (4096-token context) |
| Embeddings | `BAAI/bge-small-en-v1.5`, 384 dims, in-process on CPU |
| Result | **11/11 steps succeeded**, total 694s |
| Left running | **yes** — it is the demo project |

## The eleven steps

| Step | State | Took |
|---|---|---|
| `approve` | succeeded | 1s |
| `namespace-pack` | succeeded | 1s |
| `model-seed` | succeeded | 445s |
| `vcluster` | succeeded | 63s |
| `vector-db` | succeeded | 41s |
| `corpus-upload` | succeeded | 19s |
| `ingestion` | succeeded | 22s |
| `inference-service` | succeeded | 8s |
| `ready-check` | succeeded | 37s |
| `rag-api` | succeeded | 34s |
| `rag-ready-check` | succeeded | 23s |

`model-seed` is most of the run and all of it is network: 491,400,032 bytes of
GGUF plus the embedding model. Everything after it is under a minute apiece,
because the four container images were pre-pulled onto both workers first.

## What the pipeline produced

```json
{
  "chatEndpoint": "http://kourier-internal.kourier-system.svc.cluster.local",
  "chatEndpointUrl": "http://ksl-docs-x-default-x-ksl-docs-predictor.p-ksl-docs.hive.local",
  "chatHost": "ksl-docs-x-default-x-ksl-docs-predictor.p-ksl-docs.hive.local",
  "collectionName": "ksl-docs-corpus",
  "corpusFiles": 4,
  "corpusSource": "tenants/_uploads/69ebec8e0cc34558/",
  "embeddingDims": 384,
  "indexedChunks": 23,
  "indexedFiles": 4,
  "kubeconfigAvailable": true,
  "ragEndpointUrl": "http://ksl-docs-rag.p-ksl-docs.svc.cluster.local:8080",
  "smokeOk": true,
  "smokeSources": 3,
  "syncedName": "ksl-docs-x-default-x-ksl-docs"
}
```

## What the cluster holds

```
pod/coredns-df8c87f55-7f746-x-kube-system-x-ksl-docs                  1/1   Running     0               10m
pod/ksl-docs-0                                                        1/1   Running     1 (4m51s ago)   11m
pod/ksl-docs-ingest-qhlfr                                             0/1   Completed   0               9m25s
pod/ksl-docs-qdrant-754777cd8b-2v4sc                                  1/1   Running     0               10m
pod/ksl-docs-rag-795d56dfcd-tlzf9                                     1/1   Running     0               8m19s
pod/ksl-docs-x-default-x-ksl-docs-predictor-00001-deployment-7r77jv   2/2   Running     0               8m55s
pod/seed-ksl-docs-2qj8g                                               0/1   Completed   0               18m
service/ksl-docs                                                ClusterIP      10.96.140.139   <none>                                              443/TCP,10250/TCP                           11m
service/ksl-docs-headless                                       ClusterIP      None            <none>                                              443/TCP                                     11m
service/ksl-docs-node-hive-host-worker                          ClusterIP      10.96.91.227    <none>                                              10250/TCP                                   10m
service/ksl-docs-qdrant                                         ClusterIP      10.96.197.194   <none>                                              6333/TCP,6334/TCP                           10m
service/ksl-docs-rag                                            ClusterIP      10.96.47.37     <none>                                              8080/TCP                                    8m19s
service/ksl-docs-x-default-x-ksl-docs-predictor                 ExternalName   <none>          kourier-internal.kourier-system.svc.cluster.local   80/TCP                                      8m43s
service/ksl-docs-x-default-x-ksl-docs-predictor-00001           ClusterIP      10.96.12.8      <none>                                              80/TCP,443/TCP                              8m55s
service/ksl-docs-x-default-x-ksl-docs-predictor-00001-private   ClusterIP      10.96.231.193   <none>                                              80/TCP,443/TCP,9090/TCP,9091/TCP,8012/TCP   8m55s
service/kube-dns-x-kube-system-x-ksl-docs                       ClusterIP      10.96.9.245     <none>                                              53/UDP,53/TCP,9153/TCP                      10m
deployment.apps/ksl-docs-qdrant                                            1/1   1     1     10m
deployment.apps/ksl-docs-rag                                               1/1   1     1     8m19s
deployment.apps/ksl-docs-x-default-x-ksl-docs-predictor-00001-deployment   1/1   1     1     8m55s
replicaset.apps/ksl-docs-qdrant-754777cd8b                                            1     1     1     10m
replicaset.apps/ksl-docs-rag-795d56dfcd                                               1     1     1     8m19s
replicaset.apps/ksl-docs-x-default-x-ksl-docs-predictor-00001-deployment-74fcfc7c96   1     1     1     8m55s
statefulset.apps/ksl-docs   1/1   11m
job.batch/ksl-docs-ingest   Complete   1/1   20s     9m25s
job.batch/seed-ksl-docs     Complete   1/1   7m25s   18m
configuration.serving.knative.dev/ksl-docs-x-default-x-ksl-docs-predictor   ksl-docs-x-default-x-ksl-docs-predictor-00001   ksl-docs-x-default-x-ksl-docs-predictor-00001   True   
revision.serving.knative.dev/ksl-docs-x-default-x-ksl-docs-predictor-00001   ksl-docs-x-default-x-ksl-docs-predictor   1     True         1     1
route.serving.knative.dev/ksl-docs-x-default-x-ksl-docs-predictor   http://ksl-docs-x-default-x-ksl-docs-predictor.p-ksl-docs.hive.local   True   
service.serving.knative.dev/ksl-docs-x-default-x-ksl-docs-predictor   http://ksl-docs-x-default-x-ksl-docs-predictor.p-ksl-docs.hive.local   ksl-docs-x-default-x-ksl-docs-predictor-00001   ksl-docs-x-default-x-ksl-docs-predictor-00001   True
```

Read live from `GET /api/deployments/ksl-docs`:

| | |
|---|---|
| `livePoints` (read from Qdrant now) | **23** |
| `indexedChunks` (what the run reported) | **23** |
| vector database | 1/1 ready |
| RAG service | 1/1 ready |

**The quota arithmetic in `namespace-pack.yaml.tmpl` is confirmed by reality:**
`requests.storage` 9Gi of 10Gi (models 1 + scratch 1 + qdrant-data 2 + the
vCluster's own etcd 5), and 4 persistentvolumeclaims of 6.

## Three questions, asked through the console's own invoke path

**1. Answerable from the corpus.** *"How many KServe installations does HIVE
have, and where does it run?"*

> HIVE has one KServe installation, and it runs on the host. It is not installed
> per tenant. Every tenant deploys models through that single central KServe,
> which runs in Serverless mode using Knative for everything. There is no
> RawDeployment mode anywhere in HIVE.

Cited `01-architecture.md#2` (**0.835**), `README.md#3` (0.794),
`01-architecture.md#0` (0.740). 38.1s, 1,217 characters of context.

**2. NOT in the corpus.** *"Who is the current Queen of England?"*

> I don't find that in the indexed documents.

Cited `README.md` (**0.410**), `02-tenancy-and-quotas.md` (0.381, 0.377). 13.0s.

**This is the result that matters, and the scores are why.** The passages came
back at 0.38–0.41 against 0.74–0.84 for a question the documents actually answer.
The sources panel shows them anyway, so a person can see *why* it declined rather
than having to trust that it did. A RAG system that answers everything
confidently is the failure mode; this one refused and showed its working.

**3. A question the model answers badly.** *"What is the difference between
always-on and on-demand?"* returned *"always-on is a long-lived LLM service,
while on-demand is a bursty archetype"* — thin, from four correctly-retrieved
passages of `03-archetypes-and-storage.md`.

**Retrieval worked; the writing did not.** That is the 0.5B model, and it is the
standing reminder to judge the citations rather than the prose (DEVIATIONS.md).

## One defect found and fixed

`ServiceForward.ensure()` in `app/dataplane.py` spawned `kubectl port-forward`,
slept a fixed **1.5 seconds**, and probed **once** — three attempts of that being
4.5 seconds spread over three processes. A port-forward routinely takes longer to
establish: it has to reach the API server, resolve the Service to a pod and open a
SPDY stream.

The symptom was a **503 on the playground's first question**, with the pipeline's
own `rag-ready-check` having succeeded minutes earlier through the same code —
load-dependent, so it passed on an idle cluster and failed on a busy one. Which
is exactly when a demo would meet it.

It now spawns once and polls for readiness for up to 15s per attempt, and detects
a child that exited rather than polling at a dead process for the full window.

**Nothing else needed changing.** Every other step worked first time.

## Timing worth knowing before a demo

| | |
|---|---|
| First question after a deploy | ~38s |
| Later questions | ~13–25s |
| Cause | a 0.5B model on CPU, and 1–2k characters of context per question |

Fast enough to demonstrate, slow enough to talk over. On an always-on project
there is no cold start on top of this; on-demand adds ~15s after an idle spell.

