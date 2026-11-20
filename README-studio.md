# HIVE Studio

**HIVE-DD-002 v1** — the PI-facing console for the HIVE platform.

Studio turns "I have a model and a deadline" into a running endpoint without the
researcher learning Kubernetes. It shows a catalogue of use-case templates, asks
five questions, provisions a real tenant, and hands back a web address and a
kubeconfig.

**Studio is a client of the platform, not a second platform.** It creates exactly
the objects a tenant would have created by hand, it obeys every invariant in
[CLAUDE.md](CLAUDE.md) §2 rather than working around any, and it holds no
authority the platform does not already enforce. It *sets* the `hive.ksl/expiry`
clock; the host reconciler is what honours it. It *writes* a ResourceQuota;
admission control is what enforces it.

---

## Quickstart

```bash
# both halves, one command
scripts/studio-up.sh                       # mock — nothing is created
STUDIO_EXECUTOR=real scripts/studio-up.sh  # provisions for real

scripts/studio-down.sh                     # stop the console + BFF
scripts/studio-down.sh --status            # what is running, and in WHICH MODE
```

### Switching between mock and real

**The executor mode is fixed when the backend starts.** It is read once, at
startup, so that it cannot change under a pipeline that is already running — and
that means **a backend that is already up cannot be switched.** `studio-up.sh`
reuses whatever is on :8000 rather than starting a second uvicorn that would fail
to bind, so re-running it with a different `STUDIO_EXECUTOR` changes nothing.

To switch, stop first. Always these two lines, in this order:

```bash
scripts/studio-down.sh                          # then EITHER:
scripts/studio-up.sh                            # mock — nothing is created
STUDIO_EXECUTOR=real scripts/studio-up.sh       # real — provisions on the cluster
```

If you forget the stop, `studio-up.sh` now says so rather than leaving you to
spot it in the banner:

```
  !! YOU ASKED FOR 'real' AND THE RUNNING BACKEND IS 'mock'.
```

**Which mode am I in?** Four places agree, and all four read it from the backend
rather than from what anyone typed:

```bash
scripts/studio-down.sh --status                 # executor  real — Deploy PROVISIONS…
curl -s localhost:8000/api/health                # {"executor": "real", …}
```

plus the banner `studio-up.sh` prints, and the console itself.

**Open the console at `http://localhost:5173`, not `http://127.0.0.1:5173`.**
Vite binds to IPv6 `[::1]` only, so the dotted-quad form is refused while
`localhost` works. This bites scripted health checks more than browsers.

> **A project deployed in mock does not exist.** The registry remembers it as
> `ready` — that is what the run did — but the console reads status live from the
> cluster (invariant (b)), finds nothing, and shows it as **failed** once you
> switch to real. That is the invariant working, not a bug: a cached "Ready" for
> something that was never created is exactly what it exists to prevent. Delete
> such a project to clear it out; nothing on the cluster is touched, because
> there was never anything there.

| | |
|---|---|
| Console | http://localhost:5173 |
| API docs | http://localhost:8000/docs |

**`mock` is the default on purpose.** The dangerous direction is provisioning by
accident, so the safe mode is the one you get by forgetting to set anything;
`real` must be deliberate. In real mode the backend **refuses to start** unless
your kubectl context is `kind-hive-host`.

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `STUDIO_EXECUTOR` | `mock` \| `real` | `mock` | whether the pipeline touches the cluster |
| `STUDIO_FAIL_STEP` | a step id | unset | fail that step deliberately; read fresh at each step, so clearing it and retrying works |
| `STUDIO_DB` | path | `studio/backend/studio.db` | registry location |

First run only:

```bash
python3 -m venv studio/backend/.venv
studio/backend/.venv/bin/pip install -r studio/backend/requirements.txt
(cd studio/frontend && npm install)
```

Verify the read surface without a cluster: `scripts/studio-smoke.sh` — 36 assertions.

---

## API

Everything under `/api`. The frontend calls relative paths only; the Vite dev
server proxies them, so the browser is same-origin and there is no CORS layer.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness, executor mode, data-path state |
| `GET` | `/api/templates` | catalogue cards |
| `GET` | `/api/templates/{id}` | one template, **enums resolved** |
| `GET` | `/api/models` | the curated model list |
| `POST` | `/api/deployments` | provision; `?dry_run=1` renders without creating |
| `GET` | `/api/deployments` | list; `?all=1` includes removed |
| `GET` | `/api/deployments/{id}` | intent + history + **live cluster status** |
| `PATCH` | `/api/deployments/{id}` | rename the display label (registry only) |
| `POST` | `/api/deployments/{id}/retry` | resume a failed run from its failed step |
| `POST` | `/api/deployments/{id}/invoke` | call the model, server-side |
| `POST` | `/api/deployments/{id}/renewal` | record a request for more time |
| `GET` | `/api/deployments/{id}/events` | pipeline log + cluster events, merged |
| `GET` | `/api/deployments/{id}/kubeconfig` | the tenant's own admin credentials |
| `DELETE` | `/api/deployments/{id}` | teardown, then soft-delete |

Two rules worth knowing before you build on these:

**`GET /api/deployments/{id}` reads the cluster every time.** Readiness, replica
count, endpoint, quota and expiry all come from `kubectl`, never from SQLite. The
registry stores intent and history only ([CLAUDE.md](CLAUDE.md) invariant (b)) — a
cached "Ready" for a project that has since scaled to zero is worse than no status.

**`invoke` is server-side and always will be.** Reaching an endpoint locally needs
a port-forward plus a `Host` header; proxying through the BFF means the browser
never learns a URL shape production will not have.

---

## The catalogue is data

Templates live in [`templates/catalog/`](templates/catalog/) and **no template
lives in the backend**. The wizard is generated from a template's `parameters`; the
manifests are rendered from its fragments.

```
templates/catalog/
├── models.yaml                     the CLOSED model list — the governance boundary
├── inference-endpoint/             maturity: available
│   ├── template.yaml               parameters, resources, outputs, lifecycle
│   └── resources/*.yaml.tmpl       two-pass templates, rendered per deploy
├── rag-stack/                      maturity: available
│   ├── template.yaml
│   ├── resources/*.yaml.tmpl       seven fragments
│   └── demo-corpus/                indexed when a PI uploads nothing
└── fine-tune/template.yaml         maturity: coming-soon
```

Schema and field contract: [`studio/backend/app/schema.py`](studio/backend/app/schema.py).
The loader ([`catalog.py`](studio/backend/app/catalog.py)) validates everything at
startup and **refuses to start** on a malformed file, naming the file and the field.

### Adding a template — a file, not code

This was the claim the coming-soon entries existed to test, and **`rag-stack` is
now the answer**: it went from a sketch to a working template, and the honest
score is *mostly data, some code*. The parameters, the seven manifests, the
quota profile, the outputs and the teardown order are all YAML. What needed
backend work was the machinery the template's SHAPE implied and the old one had
never needed — filtered enums, five new pipeline steps and their executors, a
gate that is not last, and a second data path. `studio/` grew; no template moved
into it.

The general recipe, still accurate:

1. **Write the questions.** Fill in `values:` for the enums in
   `templates/catalog/rag-stack/template.yaml`. The wizard renders them — six
   parameter types are supported (`string`, `enum`, `integer`, `boolean`, `fixed`,
   `file`).
2. **Add the fragments.** Drop `.yaml.tmpl` files under `rag-stack/resources/`, one
   per resource, using the two-pass contract documented at the top of every
   existing fragment (`#@if key=value` blocks, then `$placeholder` substitution).
3. **List them.** Add each entry to `spec.resources`, and map it to a pipeline
   step in `pipeline.RESOURCE_TO_STEP`. If the entry is named for what the PI
   gets rather than for what is in the file — `vector-db` renders
   `qdrant.yaml.tmpl` — alias it in `catalog.RESOURCE_TO_FRAGMENT`. A step that
   waits rather than creates goes in `pipeline.STEP_GATES`, next to the step it
   gates; that is what puts `ready-check` in the MIDDLE of a RAG run, because
   `rag-api` needs the chat hostname it reads.
4. **Declare the lifecycle.** `spec.lifecycle.ttl.fromParameter` must name a
   parameter you actually defined. (`fine-tune` still points at `durationDays`
   without defining it — the loader will refuse to start the moment it is
   promoted, which is the check doing its job.)
5. **Flip `maturity: available`.** The catalogue now gets its full referential
   check: enums must resolve, every resource must have a fragment, teardown steps
   must be canonical ids.

Adding a **model** is smaller still: one entry in `models.yaml`. That closure is
the governance boundary — a PI selects an id and cannot supply a URL, so the only
network egress the pipeline performs is a `seedUrl` a platform engineer reviewed.
An `embed` entry is a different shape (no runtime, no URL — it is loaded
in-process by the RAG toolkit) and the loader enforces the contract **per
interface**, so relaxing the schema for embeddings did not quietly make `seedUrl`
optional for chat models.

Adding a **serving runtime** is the third menu and the one a PI never sees:
[docs/catalog-curation.md](docs/catalog-curation.md) is the procedure, and
`hive-llama-cpp-rag` is the worked example.

---

## How this maps to production

The console is the part that does not change.

| Layer | Local | Production |
|---|---|---|
| **SPA** | Vite dev server on 5173 | **unchanged** — same React app, served from a real host |
| **BFF** | FastAPI on 8000, shells out to `kubectl`/`vcluster` | **Platform API**, with Keycloak-issued tokens and per-user authorisation |
| **Data path** | one `kubectl port-forward` to Kourier, `Host` header routing | **Kong**, with MetalLB addresses and the token check at the gateway |
| **Registry** | SQLite, intent and history | **Platform inventory** — same split: intent stored, status always read live |
| **Identity** | a name in `localStorage` | **Keycloak** against KAUST AD |
| **Executor** | `RealExecutor` shelling out to CLIs | the same operations through the Platform API |

The three things that survive the move unchanged are the ones worth noticing: the
**SPA**, the **catalogue** (templates and models are data, not code), and the
**invariants** — render before apply, status read live, reserved names, the
protected reference tenants.

Everything that does not survive is listed in [DEVIATIONS.md](DEVIATIONS.md) and
mirrored, in plain language, in the console's own **Local build notes** panel
(`studio/frontend/src/deviations.js`). Those two must be kept in step.

---

## Layout

```
studio/backend/app/
├── main.py           FastAPI app and routes
├── catalog.py        loads and validates templates/catalog/ at startup
├── schema.py         Pydantic models for the catalogue and the API
├── render.py         PURE — fragments + answers -> manifest text
├── pipeline.py       the six canonical steps, the runner, the executor protocol
├── executor_real.py  the real one: kubectl, vcluster, and the guards
├── live.py           every live cluster read (invariant (b) lives here)
├── dataplane.py      the supervised port-forward the playground calls through
├── db.py             registry: deployments, steps, renewal requests
└── validation.py     server-side parameter checks, incl. reserved names

studio/frontend/src/
├── App.jsx           shell, routes, mock sign-in, deviations panel
├── steps.js          the six step ids -> human words, in ONE place
├── tiers.js          tier ceilings (the only duplicated governance data)
├── deviations.js     the honesty panel's single source
└── pages/            Catalog · TemplateDetail · Wizard · DeploymentsList ·
                      DeploymentDetail (4 tabs) · Playground
```

## Proof

| Document | Shows |
|---|---|
| [docs/studio-real-proof.md](docs/studio-real-proof.md) | the UI provisioning a real tenant end to end, and removing it |
| [docs/studio-sleepwake-proof.md](docs/studio-sleepwake-proof.md) | an on-demand project releasing its GPU and waking in ~8.5 s |
| [docs/studio-failure-proof.md](docs/studio-failure-proof.md) | what a PI sees when a step fails, at three different steps |
| [scripts/studio-demo.md](scripts/studio-demo.md) | the seven-minute walkthrough |
| [docs/e2e-test.md](docs/e2e-test.md) | **how to test the whole thing**, in five tiers, cheapest first |
| [docs/rag-real-proof.md](docs/rag-real-proof.md) | the first real RAG deployment, step by step, with the answers it gave |
| [docs/rag-manifests.md](docs/rag-manifests.md) | the seven RAG manifests — why each is a separate file |
| [scripts/studio-down.sh](scripts/studio-down.sh) | stopping the console — and why that is not a teardown |

## The second template: RAG

`rag-stack` provisions a project that answers questions about a PI's own
documents and cites the passages it used. Seven manifests, eleven pipeline steps.

```
approve → namespace-pack → model-seed → vcluster → vector-db → corpus-upload
        → ingestion → inference-service → ready-check → rag-api → rag-ready-check
```

Four things about it are worth knowing before reading the files:

- **Only the chat model lives in the vCluster.** Qdrant, the ingestion Job and
  the RAG service are host-native, because a pod authored inside a vCluster
  cannot mount the existing host `models`/`scratch` PVCs — the syncer maps a
  virtual claim to a *new* host claim, so an in-vCluster ingester would index an
  empty volume and report success. DEVIATIONS.md carries the row.
- **The embedding model is not a served model.** `bge-small-en-v1.5` runs
  in-process, on CPU, inside the ingestion Job and the RAG service. A second
  InferenceService would have taken a GPU it does not need against a ceiling of
  one, and one of the two `count/inferenceservices` a tenant is allowed.
- **The template pins the runtime, not the catalogue.** Retrieval needs
  ≥ 4096 tokens of context or the prompt truncates *silently, from the front,
  where the passages are* — so `rag-stack` pins `hive-llama-cpp-rag` regardless
  of what the model entry names, and `topK` is capped at 8 because of that
  number.
- **`rag-ready-check` asks a real question.** Health, then one smoke question
  through embed → search → prompt → gateway → model → answer, recorded in the
  step log. So a PI's first click is never the first real request, and a project
  that provisioned but cannot answer fails while a progress bar is still on
  screen.

```bash
scripts/build-rag-toolkit.sh        # build + kind load the toolkit image
studio/backend/.venv/bin/python tests/test-render.py    # 55 assertions, no cluster
studio/backend/.venv/bin/python tests/test-chunker.py   # 19 assertions, no cluster
scripts/studio-smoke.sh             # 78 assertions incl. a full mock RAG run
scripts/studio-ui-check.sh          # 62 assertions, the console's own components
```

`studio-ui-check.sh` mounts the real components in a DOM and clicks things —
it is what confirms the eleven RAG steps render, that a *succeeded* step's log
opens (which is where `INGEST_RESULT` lives), and that the sources panel appears
even when retrieval found nothing. The wizard in it renders **fixtures captured
from the running BFF**, so it is the real catalogue being laid out.

`studio-smoke.sh` also cross-checks every `/api/...` path the console calls
against the BFF's own OpenAPI schema. That guard exists because a refactor once
deleted two routes and nothing noticed: the console still built, every test still
passed, and the only symptom was a 404 on a tab nobody had clicked.

### In the console

The wizard asks about documents, the project page shows the knowledge base, and
the playground answers questions with the passages it used.

**None of that arrived as a branch on the template id.** The console reads three
things from the catalogue and renders the rest:

| The template says | The console does |
|---|---|
| `parameters[].group` | puts each question on Basics or Resources |
| `parameters[].type` | picks the input — including `upload`, the drop zone |
| `spec.interface` | picks the playground — `rag` here, the model'''s own otherwise |

and `GET /api/templates/{id}` now returns the **step plan** the backend computed
from that template'''s `resources`, so "what happens when you deploy" is the real
eleven steps rather than a list in the frontend. `STEP_ORDER` is gone from
`steps.js` for that reason; the file translates ids into words and no longer
claims to know their order.

Two consequences worth knowing:

- **A template can add a question without the console being edited.** That was
  always the claim; it is now true. Adding a new KIND of question needs one
  renderer in `src/fields.jsx`, which is the honest cost.
- **The archetype trade-off is catalogue copy.** "Frees the GPU when nobody is
  asking…" used to be a ternary in the wizard keyed on the option'''s value. It is
  `values[].description` in the template, and an enum whose options all describe
  themselves renders as comparable cards rather than a dropdown.

## Known gaps

- **`inference-endpoint` still cannot deploy a chat model.** Its Qwen entry needs
  `hive-llama-cpp`, which does not exist (Step 9b was never done here). The RAG
  template is unaffected — it pins `hive-llama-cpp-rag`, which is authored and
  installed ([docs/catalog-curation.md](docs/catalog-curation.md)). Authoring the
  2048-context sibling is what closes this.
- ~~The RAG stack has not been run end to end on the cluster.~~ **Done** —
  see [docs/rag-real-proof.md](docs/rag-real-proof.md). 11/11 steps, ~11 minutes,
  three-quarters of it `model-seed` pulling 491MB of GGUF. It found one defect
  (the playground's port-forward probed once after a fixed 1.5s and 503'd under
  load) which is fixed.
- **A corpus is indexed once.** Changing your documents means redeploying: there
  is no re-index trigger and no incremental update.
- **A corpus cannot be changed without redeploying.** The upload endpoint mints
  an id per set of documents and never mutates one, because a deployment may
  already point at it. Removing a file in the wizard re-uploads the rest under a
  new id; changing the documents of a project that already exists means deploying
  again.
- **Uploads are not garbage-collected.** `tenants/_uploads/` accumulates a
  directory per upload, including ones that were never deployed. They are
  gitignored — they are a PI'''s research data, not repository content — and
  removing them is a manual `rm` for now.
- **Expiry is recorded, not enforced.** Waiting on the platform TTL service.
- **Tier ceilings are duplicated** in `tiers.js` and `namespace-pack.yaml.tmpl`.
  The fix is a backend endpoint.
- **One provisioning run at a time**, deliberately — a laptop-scale cap.
- **The isvc is mirrored once, at deploy.** A tenant editing their own
  InferenceService afterwards is not reflected on the host.
