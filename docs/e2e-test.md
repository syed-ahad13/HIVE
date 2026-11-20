# Testing HIVE Studio end to end

Five tiers, cheapest first. Each one is useful on its own, and each one tells you
something the tier below it cannot.

| Tier | Needs | Time | Answers |
|---|---|---|---|
| 0 | nothing | ~10s | is the pure logic right? |
| 1 | nothing | ~5s | does the console render what the catalogue says? |
| 2 | Python only | ~3min | does the whole pipeline work, without a cluster? |
| 3 | a browser | ~5min | does a person get through it? |
| 4 | the cluster | ~20min | does it actually work? |

**Run them in order.** Not ceremony: tier 4 takes twenty minutes and creates real
things, and the most common reason it fails is something tiers 0–2 would have
caught in seconds. A failing render test means every manifest is wrong, and
finding that out after `vcluster create` has run is twenty wasted minutes.

---

## Tier 0 — the pure logic

```bash
studio/backend/.venv/bin/python tests/test-render.py     # 55 assertions
studio/backend/.venv/bin/python tests/test-chunker.py    # 19 assertions
```

**What it does.** Renders both templates' manifests from fixed inputs and a fixed
clock, parses the YAML, and asserts what is in it. Chunks a document and asserts
the passages.

**Why it exists.** `app/render.py` is pure by design — hand it inputs and a clock
and it returns text — and that is what makes this possible with Docker stopped.
The assertions are the things that would be *silently* wrong: that the RAG
InferenceService pins `hive-llama-cpp-rag` rather than the model's own runtime,
that `storageUri` is `pvc://models` with no trailing slash, that the ingestion Job
and the RAG service are given the *same* embedding model.

> **Design decision — why the renderer is pure.** It could have written files and
> applied them in one step. Keeping it as inputs→text buys three things: the
> wizard's preview and the real deploy come from the *same function*, so the
> preview cannot drift into a plausible-looking lie; the executor is the only
> thing that writes, so there is one place to audit against invariant (a); and
> this test exists at all.

> **Design decision — why the chunker is stdlib-only.** It is the one piece of
> RAG logic that decides retrieval quality, and it has no dependencies so it can
> be tested in a second rather than inside a 900-second Job on a cluster. Its
> contract is *determinism*: the same folder always produces the same chunk ids,
> because a chunk id is a citation and a citation that moves is worthless.

---

## Tier 1 — the console's own components

```bash
scripts/studio-ui-check.sh                               # 62 assertions
```

**What it does.** Compiles `studio/frontend/tests/ui-check.jsx` with the project's
own Vite config, runs it in Node against a jsdom document, mounts the real
components, and **clicks things**.

**Why it exists.** `npm run build` proves the console *compiles*. It says nothing
about whether the progress list renders eleven steps, whether a successful step's
log can be opened, or whether the sources panel appears when retrieval found
nothing — and those are exactly the claims the RAG work makes.

Three of the checks are worth knowing about because each covers a bug that was
actually there:

- **the ingestion log opens.** `StepRow` used to render a log only for *failed*
  steps, so the most informative output the pipeline produces — the
  `INGEST_RESULT` line saying how many passages were indexed — was reachable only
  if it broke.
- **the sources panel renders when empty.** When retrieval finds nothing the
  service refuses to guess *without calling the model*. The empty panel is the
  evidence for that refusal; hiding it would leave a refusal with no visible
  reason.
- **the wizard lays out both templates** — from fixtures captured off the running
  BFF, so it is the real catalogue being rendered, not a hand-written imitation.

> **Design decision — why there is no test runner.** No Jest, no Vitest, no
> Testing Library. The script uses the Vite config the app already has, so there
> is one JSX transform and one resolution story rather than two that must be kept
> in step. jsdom is installed `--no-save`, so a 30MB implementation of the browser
> never enters the dependency list of a page that runs *in* a browser.

---

## Tier 2 — the whole pipeline, without a cluster

```bash
scripts/studio-smoke.sh                                  # 78 assertions, ~3 min
```

**What it does.** Starts its own BFF on a scratch database and a scratch upload
directory, exercises every read endpoint, uploads a corpus, dry-runs both
templates, deploys both for real *against the mock executor*, tears them down,
then injects a failure and retries it.

**Why it takes three minutes.** The mock executor sleeps roughly as long as the
real steps take — a RAG run is ~54 seconds across eleven steps. That is not
padding: the progress UI is being exercised against a realistic rhythm, and a
demo where the progress list flashes past teaches nothing about what waiting
feels like.

**What to look for in the output.** The eleven-step table, and specifically that
`ready-check` sits *between* `inference-service` and `rag-api` rather than at the
end — `rag-api` needs the chat model's live hostname, which only `ready-check`
can read.

> **Design decision — mock is the default.** `STUDIO_EXECUTOR` defaults to `mock`
> because the dangerous direction is provisioning by accident. The safe mode is
> the one you get by forgetting to set anything; `real` has to be a deliberate act.

> **Design decision — the route cross-check.** The script reads every `/api/...`
> path out of `studio/frontend/src` and compares it against the BFF's own OpenAPI
> schema. That guard exists because a refactor once deleted two routes and nothing
> noticed: the console still built, every test still passed, and the only symptom
> was a 404 on a tab nobody had clicked. Assertions catch the routes somebody
> thought to assert; this catches the ones nobody did.

---

## Tier 3 — through the console, by hand

```bash
scripts/studio-up.sh                    # BFF on :8000, console on :5173
```

Open <http://localhost:5173>. Still mock — nothing is created.

Walk **Catalogue → Ask questions about your own documents → Deploy**:

1. **Basics.** Three questions: name, documents, duration. Drop two `.md` files on
   the drop zone; watch the list appear with sizes and a remove button. Then
   remove them all and read the fallback line — *"No files? We'll index the
   bundled HIVE documents so you can try it immediately."*
2. **Resources.** Six questions. Check that **Search index** is a read-only
   statement rather than a dropdown of one, that **How many passages** is bounded
   1–8, and that **Availability** renders as two comparable cards.
3. **Review.** The corpus files and the collection name, the eleven steps in the
   PI's language, and the exact manifests behind a disclosure.
4. **Deploy.** Watch the progress list. **Expand the ingestion step's log** — the
   `INGEST_RESULT` line is there, and the mock says plainly that nothing was
   really indexed.
5. **The project page.** The Knowledge base card, in mock mode, shows the figures
   the run reported and an amber banner saying none of it describes anything that
   exists.

**What you are testing that no automated check covers:** whether a person can get
through it. Whether the questions are in a sensible order, whether the copy makes
sense, whether the wait feels explained.

> **Design decision — the wizard reads the catalogue, not a list of names.** It
> used to name the five parameters `inference-endpoint` declares. RAG asks nine,
> two of kinds that did not exist. Now three fields carry it — `group` decides the
> screen, `type` decides the input, `spec.interface` decides the playground — and
> a template can add a question without the console being edited. The honest cost:
> a new *kind* of question still needs a renderer in `src/fields.jsx`.

> **Design decision — the upload endpoint is all-or-nothing.** One unusable file
> rejects the whole set. The friendlier alternative — accept what we can, report
> the rest — means a PI who dropped ten PDFs and one README gets a project quietly
> built from the README, and finds out a month later.

---

## Tier 4 — the real cluster

**This has now been done once** — [docs/rag-real-proof.md](rag-real-proof.md)
is the transcript: 11/11 steps, ~11 minutes, with the real answers and the real
timings. Read it before you run this yourself; it tells you what each step
actually costs and what the model actually says.

The four container images it needs are already on both workers, so a repeat run
skips the slowest surprise. `model-seed` still downloads 491MB of GGUF per
project — that is per-deployment and cannot be pre-staged, because the PVC it
writes into does not exist until the deployment does.

### 4a. Pre-flight

```bash
kubectl config current-context                    # must be kind-hive-host
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}={.status.capacity.nvidia\.com/gpu}{"\n"}{end}'
```

**Why.** The fake GPUs are a node-status patch, and **any kubelet restart wipes
them** — a Docker Desktop restart included. Empty output here means every pod
will sit `Pending` with `Insufficient nvidia.com/gpu`. Fix with
`scripts/fake-gpus.sh`.

```bash
scripts/build-rag-toolkit.sh --check
```

**Why.** The toolkit image lives in no registry, so every pod using it sets
`imagePullPolicy: Never`. If the nodes do not have it, the failure is
`ImagePullBackOff` on an image `docker images` clearly shows is present — because
the host Docker daemon and the nodes' containerd are separate stores. Without
`--check` the script builds and loads as needed.

### 4b. Preload the serving-runtime image — strongly recommended

```bash
for n in hive-host-worker hive-host-worker2; do
  docker exec "$n" crictl pull \
    ghcr.io/ggml-org/llama.cpp@sha256:c3728b9ba0c4c427770eec092fdb1bab1e86e811d578b27440771b8b3207f2c7
done
```

**What it does.** Fetches the 1.18GB llama.cpp server image into each worker's
containerd *before* the run needs it.

**Why.** Without this, `ready-check` is where the deployment sits while the
kubelet pulls a gigabyte, and its 900-second ceiling is not generous for that on
a domestic line. Doing it up front turns a stall of unknown length into a step
you watched finish.

**Why `crictl pull` and not `kind load`.** `kind load` works by
`docker save | ctr images import`, and save/import **rebuilds the image manifest,
so the manifest digest changes**. The runtime pins its image by digest, so a
tag-loaded copy would not match and the kubelet would pull from the registry
anyway. A digest reference can only be satisfied by a containerd that fetched
that exact manifest. This is the same finding `scripts/pull-images.sh` documents
for the five digest-pinned Knative images.

> **Known gap, worth fixing before a cold rebuild.** `host/images.txt` does not
> list *any* of the four RAG images, and `pull-images.sh --refresh` does not scan
> `host/serving/runtimes/`. So a `kind delete` + rebuild will not pre-fetch them
> and the first RAG deploy on a fresh cluster pulls ~1.5GB mid-run. Nothing is
> broken; the offline-rebuild promise just does not cover RAG yet.

### 4c. Deploy for real

```bash
STUDIO_EXECUTOR=real scripts/studio-up.sh
```

**Why the environment variable and not a toggle in the UI.** Provisioning by
accident is the failure to design against, so the mode is a property of the
process, fixed at startup, and the console *reads it back from `/api/health`*
rather than echoing what you asked for. The backend also refuses to start if the
kubectl context is not `kind-hive-host`.

Deploy through the console as in tier 3 — or from a terminal:

```bash
UP=$(curl -s -X POST http://localhost:8000/api/uploads \
       -F "files=@templates/catalog/rag-stack/demo-corpus/01-architecture.md" \
     | python3 -c 'import json,sys;print(json.load(sys.stdin)["uploadId"])')

curl -s -X POST http://localhost:8000/api/deployments \
  -H 'Content-Type: application/json' \
  -d "{\"templateId\":\"rag-stack\",\"params\":{
        \"projectName\":\"ragdemo\",
        \"baseModel\":\"qwen2.5-0.5b-instruct-q4\",
        \"embeddingModel\":\"bge-small-en-v1.5\",
        \"vectorDb\":\"qdrant\",
        \"corpusUploadId\":\"$UP\",
        \"topK\":3,
        \"availability\":\"on-demand\",
        \"tier\":\"small\",
        \"durationDays\":10}}"
```

**What each step is doing, and roughly how long:**

| Step | What happens | Watch for |
|---|---|---|
| `approve` | preconditions, then the toolkit image | the runtime it says it needs |
| `namespace-pack` | namespace, quota, LimitRange, **three** PVCs | quota `requests.storage: 10Gi` |
| `model-seed` | downloads 469MiB of GGUF + the embedding model | the slowest step, ~3–6 min |
| `vcluster` | `vcluster create`, then seeds the KServe API into it | ~2–3 min |
| `vector-db` | Qdrant Deployment + Service | rollout completes = `/readyz` passed |
| `corpus-upload` | staging pod, `kubectl cp`, verify, delete pod | the file list read back *inside* the pod |
| `ingestion` | chunk, embed on CPU, upsert | the `INGEST_RESULT` line |
| `inference-service` | authored **inside the vCluster**, mirrored to the host | this is the architecture claim |
| `ready-check` | waits for Ready, reads `.status.url` **live** | the URL has a `-predictor` segment |
| `rag-api` | resolves the gateway, applies the RAG service | `Host:` header it was given |
| `rag-ready-check` | `/healthz`, then **one real question** | the answer, in the step log |

> **Design decision — `rag-ready-check` asks a real question.** Everything before
> it can succeed with a project that still cannot answer: Qdrant up but empty, the
> service ready but unable to reach the model, the model asleep. One question
> exercises embed → search → prompt → gateway → model → answer. It also means the
> PI's first click is never the first real request — on an on-demand project that
> first question pays a cold start, and paying it here, while a progress bar is
> already on screen, is far better than paying it in the playground where it looks
> like the endpoint is broken.

> **Design decision — the RAG components run in the HOST namespace.** Only the
> chat model is authored inside the vCluster. A pod authored inside a vCluster
> **cannot mount the existing host PVCs** — the syncer maps a virtual claim to a
> *new* host claim, so an in-vCluster ingestion Job would index an empty volume
> and report success. Silent, which is why it drove the decision.

### 4d. Verify it actually works

```bash
kubectl get all -n p-ragdemo
kubectl get isvc -n p-ragdemo
kubectl logs -n p-ragdemo job/ragdemo-ingest --tail=5
```

Then in the console's **Playground**, ask two questions:

- *"How many KServe installations does HIVE have?"* — expect a correct answer with
  **cited passages**, each showing its file, chunk id and similarity score.
- *"Who is the current Queen of England?"* — expect
  *"I don't find that in the indexed documents."* **and the sources panel still
  visible**, so you can see the grounding that produced the refusal.

**The second question is the more important test.** A RAG system that answers
everything confidently is the failure mode; one that declines when its documents
do not cover the question is the feature.

> **Design decision — judge the citations, not the prose.** The model is a 0.5B
> chosen to fit on a laptop. If the passages under an answer are the right ones,
> retrieval worked — that is the part this rehearsal proves. The writing is not.
> Observed on the real run: a question the documents answer retrieved passages at
> **0.74–0.84**; one they do not retrieved at **0.38–0.41** and was refused. The
> scores are the signal, and they are on screen.

Check the **Knowledge base** card: `livePoints` is read from Qdrant on every page
load, beside the count the ingestion run reported. When those two disagree, that
is information, not a bug — the index has changed since it was built.

### 4e. Sleep and wake (on-demand only)

```bash
kubectl get pods -n p-ragdemo -w
```

Leave it 60–90 seconds. The predictor pod goes away; the status pill says
**Sleeping** in grey, not red. Qdrant and the RAG service stay up — only the model
sleeps, which is why a sleeping RAG project still answers `/healthz` instantly and
still takes ~15 seconds for the first *question*.

### 4f. Tear it down

```bash
# From the console: Settings → Danger zone → type the project name
kubectl get ns p-ragdemo        # expect: NotFound
```

**Why the order matters.** `teardownOrder` is `rag-api → ingestion → vector-db →
inference-service → vcluster → namespace-pack`, and it is not the reverse of
creation. The RAG service goes first because it is the only thing holding
connections to the other two; the ingestion Job second because it *writes* to
Qdrant and may still be running on a failed deployment; the namespace last,
because it takes the quota, the LimitRange and all three PVCs — and therefore the
PI's documents — with it.

---

## What is still not covered

- **A cold rebuild.** `kind delete cluster` → `scripts/hive-up.sh` → tier 4 has
  never been run for RAG, and the image gap in 4b is the known reason it would be
  slow rather than broken.
- **Concurrency.** The pipeline runs one provisioning job at a time, deliberately.
  Two RAG projects at once is untested.
- **A large corpus.** The demo corpus is 4 files and 23 passages. The ceilings are
  20 files and 5MB, and nothing has been indexed at that size.
- **`inference-endpoint` with a chat model**, which still needs a
  `hive-llama-cpp` this repository does not contain (Step 9b).

## Leftovers on the current cluster

`p-coral` is a Studio-provisioned inference project from an earlier session,
still running and holding a GPU. It does not block a RAG deployment — quota is
per namespace — but it is consuming cluster CPU and memory. Remove it from the
console, or:

```bash
kubectl get ns p-coral -o jsonpath='{.metadata.labels.hive\.ksl/template}'   # studio-inference
kubectl delete ns p-coral
```
