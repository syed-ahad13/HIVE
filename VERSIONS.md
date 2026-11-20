# VERSIONS

Every component installed for HIVE-LOCAL is recorded here **at install time**, per the
working agreements in [CLAUDE.md](CLAUDE.md).

| component | version | how installed | date |
|---|---|---|---|
| kind (CLI) | v0.32.0 (go1.26.3, darwin/arm64) | pre-installed via Homebrew | 2026-08-15 |
| kind node image | `kindest/node:v1.36.1` (digest `sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5`) | `kind create cluster --name hive-host --config host/kind-config.yaml` | 2026-08-15 |
| Kubernetes (server) | v1.36.1 | kind node image default for kind v0.32.0 | 2026-08-15 |
| Kubernetes (kubectl client) | v1.36.1 | pre-installed via Homebrew | 2026-08-15 |
| Calico | v3.32.1 (`quay.io/calico/node:v3.32.1`) | `kubectl apply -f host/cni/custom-resources.yaml` (Installation + APIServer CRs) | 2026-08-15 |
| Tigera operator | v1.42.3 (`quay.io/tigera/operator:v1.42.3`) | `kubectl apply --server-side -f host/cni/operator-crds.yaml` then `-f host/cni/tigera-operator.yaml`, both vendored from Calico v3.32.1 | 2026-08-15 |
| cert-manager | v1.21.1 (`quay.io/jetstack/cert-manager-{controller,cainjector,webhook}:v1.21.1`) | `kubectl apply --server-side -f host/serving/cert-manager/cert-manager.yaml`, vendored from the upstream release | 2026-08-17 |
| Knative Serving (core) | `knative-v1.23.0` | `kubectl apply --server-side -f host/serving/knative/serving-crds.yaml` then `serving-core.yaml`, both vendored | 2026-08-17 |
| Knative net-kourier | `knative-v1.23.0` | `kubectl apply --server-side -f host/serving/knative/kourier.yaml`, vendored | 2026-08-17 |
| Envoy (Kourier gateway) | `docker.io/envoyproxy/envoy:v1.37-latest` | pulled by the Kourier manifest above | 2026-08-17 |
| KServe | v0.20.0 (`kserve/kserve-controller:v0.20.0`) | `kubectl apply --server-side -f host/serving/kserve/{namespace,kserve-crds,kserve,kserve-cluster-resources}.yaml`, vendored | 2026-08-17 |
| KServe ClusterServingRuntimes | v0.20.0 — 14 runtimes | `host/serving/kserve/kserve-cluster-resources.yaml` | 2026-08-17 |
| vcluster (CLI) | v0.36.1 | `brew install vcluster` | 2026-08-18 |
| vCluster (per-tenant control plane) | chart `vcluster-0.36.1`, app 0.36.1, k8s v1.36.0 | `vcluster create <name> --namespace p-<name> -f templates/t-{ao,sz}-values.yaml --connect=false --add=false` | 2026-08-18 |
| Node.js | v26.4.0 (`/opt/homebrew/bin/node`, darwin/arm64) | pre-installed via Homebrew (`brew list node` -> 25.9.0_1, 26.4.0; 26.4.0 linked) | 2026-08-22 |
| npm | 11.17.0 (`/opt/homebrew/bin/npm`) | bundled with the Node.js above | 2026-08-22 |
| Python | 3.14.3 (`/opt/homebrew/bin/python3`, formula `python@3.14` 3.14.3_1) | pre-installed via Homebrew | 2026-08-22 |
| Studio backend venv | CPython 3.14.3, at `studio/backend/.venv` (gitignored) | `python3 -m venv studio/backend/.venv` | 2026-08-22 |
| FastAPI | 0.141.1 | `pip install -r studio/backend/requirements.txt` | 2026-08-22 |
| Uvicorn | 0.52.4 (with the `[standard]` extra: uvloop 0.22.1, httptools 0.8.0, watchfiles 1.2.0, websockets 17.0.1, python-dotenv 1.2.3) | as above | 2026-08-22 |
| Pydantic | 2.13.4 (pydantic-core 2.46.4) | as above | 2026-08-22 |
| PyYAML | 6.0.3 | as above | 2026-08-22 |
| httpx | 0.28.1 (httpcore 1.0.9, h11 0.16.0, anyio 4.14.2, certifi 2026.7.22, idna 3.19) | as above | 2026-08-22 |
| Starlette | 1.6.0 | transitive, pulled by FastAPI 0.141.1 | 2026-08-22 |
| Vite | 8.2.2 | `npm create vite@latest . -- --template react`, then `npm install` | 2026-08-23 |
| React | 19.2.8 (react + react-dom) | as above | 2026-08-23 |
| react-router-dom | 7.18.2 | `npm install react-router-dom` | 2026-08-23 |
| @vitejs/plugin-react | 6.1.0 | dev dependency, pulled by the Vite React template | 2026-08-23 |
| create-vite (scaffolder) | 9.1.2 | run once via `npm create`; not a project dependency | 2026-08-23 |
| container image set | 19 references, listed in [host/images.txt](host/images.txt) — generated from the vendored manifests, not hand-written | `scripts/pull-images.sh` (tag-pinned: `docker pull` + `kind load`; digest-pinned: `crictl pull` on each node). **Resolved digests are recorded in `host/images.lock` at pull time**, which is where this table's per-image detail now lives | 2026-08-28 |

All of step 3 is installed and re-converged by [scripts/install-serving.sh](scripts/install-serving.sh).

## Notes

- **Host cluster `hive-host`**: 1 control-plane + 2 worker nodes, all `linux/arm64`,
  Debian GNU/Linux 13 (trixie). Everything above runs natively on Apple Silicon —
  **no Rosetta/`amd64` emulation is relied upon**, so no emulation row was added to
  [DEVIATIONS.md](DEVIATIONS.md) for this step.
- **kind default CNI is disabled** (`networking.disableDefaultCNI: true`) because kindnet
  does not enforce `NetworkPolicy`, which invariant §2.5 in [CLAUDE.md](CLAUDE.md) depends on.
  Pod CIDR is `192.168.0.0/16` in both `host/kind-config.yaml` and the Calico `Installation` CR.
- **Calico ships three manifests, not two, as of v3.30**: the operator's CRDs were split out
  into `operator-crds.yaml` and are no longer bundled in `tigera-operator.yaml`. That CRD
  manifest is ~3 MB and must be applied with `--server-side`, since a client-side apply
  overflows the 262144-byte `last-applied-configuration` annotation limit.
- Calico manifests are **vendored** into [host/cni/](host/cni/) rather than applied from a
  URL, so the exact bytes installed are committed to git and re-runs are reproducible.

## Step 3 — serving stack notes (2026-08-17)

- **Serverless mode is asserted, not assumed.** `inferenceservice-config` ships with
  `deploy.defaultDeploymentMode: "Serverless"`, but KServe v0.20's compiled-in fallback is
  `Standard` (`DefaultDeployment = Standard` in `pkg/constants/constants.go`). If the key were
  ever dropped the invariant would break silently, so `scripts/install-serving.sh` re-asserts
  it and fails loudly if it reads back as anything else.
- **`"Serverless"` is a deprecated alias in v0.20.** `ParseDeploymentMode()` normalises
  `"Serverless" -> Knative` and `"RawDeployment" -> Standard`. The two spellings are
  behaviourally identical today; we keep `"Serverless"` because it is what the v0.20 release
  manifest ships and what HIVE's contract language uses. When upstream removes the alias,
  `host/serving/kserve/inferenceservice-config-patch.yaml` is the only file to change.
- **Knative ships `config-network` and `config-domain` with only `_example` blocks**, so
  `ingress-class` really does default to Istio. Both ConfigMaps are patched from committed
  files; without the `ingress-class` patch every Route would wait forever on an Istio ingress
  that does not exist.
- **`kserve.yaml` contains no Namespace object.** It assumes `kserve` already exists, so
  `host/serving/kserve/namespace.yaml` is applied first.
- **Re-runs need `--server-side --force-conflicts`.** Knative's webhook reconciler owns
  `.webhooks[].rules`, cert-manager's cainjector owns `.webhooks[].clientConfig.caBundle`, and
  the KServe controller normalises CPU quantities on the `LLMInferenceServiceConfig` objects.
  Without `--force-conflicts` the second run of the install script fails on field-ownership
  conflicts rather than converging.
- **Kourier's controller runs in `knative-serving`, not `kourier-system`.** Only the Envoy
  gateway (`3scale-kourier-gateway`) lives in `kourier-system`.

## Step 6 — vCluster notes (2026-08-18)

- **`--add=false` is required offline.** `vcluster create` defaults to `--add=true`, which
  tries to register the vCluster with vCluster Platform and aborts with
  `you have vCluster pro features enabled, but seems like you are not logged in`.
- **Custom-resource sync in BOTH directions is a vCluster Pro feature** (`Generic Sync`,
  `vcp-distro-generic-sync`). Open-source v0.36.1 accepts the config, reports a successful
  `vcluster create`, and then the control-plane pod crash-loops. See DEVIATIONS.md — HIVE-LOCAL
  uses `scripts/sync-catalog.sh` and `scripts/sync-isvc.sh` as stand-ins.
- **The vCluster StatefulSet provisions a 5Gi etcd PVC in the tenant's host namespace**, which
  counts against the tenant ResourceQuota. Step 5's original `requests.storage: 5Gi` therefore
  blocked the control plane from ever starting; raised to `12Gi` and `persistentvolumeclaims` to
  `6`. Failure shape worth remembering: **no pod is created at all**, so `kubectl get pods` is
  empty and the explanation lives only in the StatefulSet's events.
- **A quota-blocked StatefulSet backs off exponentially.** After fixing the quota, the fastest
  way to make it retry is `kubectl scale sts <name> --replicas=0` then `--replicas=1`.
- **The `vcluster connect` CLI prints a "vCluster is up and running" banner to stdout**, so
  piping its output into `wc -l` or a JSON parser miscounts or fails. Filter it (`grep '^kserve-'`,
  `sed -n '/^{/,$p'`) in any script that parses.
- Virtual objects are name-translated on the host as `<name>-x-<namespace>-x-<vcluster>`;
  `scripts/sync-isvc.sh` deliberately mirrors that convention.

## Studio S0 — toolchain notes (2026-08-22)

- **All three HIVE Studio prerequisites were already present; nothing was installed.** The
  floors are node >= 20 and python3 >= 3.11; the machine has node **26.4.0** and python
  **3.14.3**, so `brew install` was not run and no new component entered the system. The rows
  above record what the build will actually run on, per the working agreement to record
  versions at install time.
- **Homebrew keeps two `node` kegs (25.9.0_1 and 26.4.0) and 26.4.0 is the linked one.** Worth
  knowing because `brew unlink/link node` would silently change the version the Studio frontend
  builds against. If the Vite build ever behaves differently between machines, check
  `node --version` before anything else.
- **Python is 3.14, well ahead of the 3.11 floor, and that is a risk to watch rather than a
  benefit.** The Studio backend has no dependencies pinned yet; some wheels lag a new CPython
  minor by months, so a dependency that fails to build on 3.14 is a plausible early failure. If
  that happens the fix is a `python@3.13` (or 3.11) virtualenv for `studio/backend/`, not a
  downgrade of the system interpreter.
- **This is unrelated to the `python:3.11-slim` image used by the model-seed Job.** That version
  is fixed by the container image, not by the host interpreter, and the two never need to agree.

## Studio S1 — BFF dependency notes (2026-08-22)

- **The 3.14 risk flagged in S0 did not materialise for this dependency set.** All
  five direct dependencies installed cleanly on CPython 3.14.3, and **every compiled
  extension executes as arm64** — so the BFF relies on no Rosetta emulation and no
  DEVIATIONS.md row is needed for it. Checked with `file(1)` rather than assumed:
  pydantic-core, PyYAML's `_yaml`, watchfiles, websockets and httptools are
  arm64-only `cp314` wheels; **uvloop is the one exception, a `universal2` wheel**
  carrying both an x86_64 and an arm64 slice, of which Apple Silicon runs the arm64
  one. A fat binary is not emulation, so the conclusion is unchanged — but the
  distinction is worth recording, because "the wheel contains x86_64" is exactly the
  kind of thing that looks alarming later if nobody wrote down why it is fine. The S0
  contingency (rebuild the venv against `python@3.13`) stays available and is now
  cheap, since `requirements.txt` is the only thing that would have to be replayed.
- **Versions are pinned with `==` in `studio/backend/requirements.txt`.** The venv
  itself is gitignored — it is built against whichever interpreter the machine has,
  and the reproducible artefact is the pinned list, not the resolved environment.
- **`uvicorn[standard]`, not plain `uvicorn`.** The extra is what supplies
  `watchfiles`, and therefore `--reload`. Plain uvicorn has no file watching, so the
  documented dev command in `scripts/studio-backend.sh` would fail.
- **Uvicorn's reloader watches `*.py` ONLY, and only its start directory.** Watching
  the catalogue needs BOTH `--reload-dir templates/catalog` and
  `--reload-include '*.yaml'`; supplying the directory alone changes nothing, which
  is easy to mistake for a working setup. Verified by touching `models.yaml` with
  each combination.
- **Uvicorn does not respawn a child that exited during startup.** A malformed
  template aborts the process, and the reloader then sits watching with nothing
  running — fixing the YAML at that point does NOT recover it, and Ctrl+C plus a
  restart is required. Worth knowing because the error message says "fix the file
  above and restart", and only the second half of that is automatic.
- **No ORM and no Kubernetes client, deliberately.** The registry is two tables on
  stdlib `sqlite3`, and the provisioning pipeline shells out to `kubectl` and
  `vcluster` — the same commands a human would run, and the same ones the gate
  reports in `docs/` document. Both choices are recorded in the header of
  `studio/backend/requirements.txt`.

## Studio S3 — frontend notes (2026-08-23)

- **Five packages, and only five.** `npm ls --depth=0` reports react, react-dom,
  react-router-dom, vite and @vitejs/plugin-react. The Vite template also
  scaffolds `@types/react`, `@types/react-dom` and `oxlint`; all three were
  removed. The types packages are for TypeScript, which this project is not
  using, and oxlint is a linter nobody asked for — an unused dev dependency is
  still a thing that has to be kept current and audited.
- **No web fonts and no CDN, per CLAUDE.md §6.** The system font stack is the
  only typeface, and the favicon is an inline `data:` SVG rather than a file
  fetched at load. The console must work on a laptop with no internet, which is
  exactly the demo condition it exists for. Verified: zero network requests leave
  localhost.
- **Vite binds to `[::1]` (IPv6) by default, not `127.0.0.1`.** `curl
  http://127.0.0.1:5173` fails with a connection refused while
  `curl http://localhost:5173` succeeds. Worth knowing before debugging a proxy
  that is working perfectly well.
- **Uvicorn's `--reload` and Vite's HMR are independent.** Editing a template
  YAML restarts the backend (see the S1 notes); editing a `.jsx` file swaps the
  module in the browser without a reload. Neither notices the other, so a change
  spanning both needs both to settle before the console reflects it.
- **The frontend was verified by screenshot, not by "it builds".** `npm run build`
  succeeding proves the modules parse; it says nothing about whether React
  renders. Headless Chrome was driven over the DevTools Protocol — using Node's
  built-in `WebSocket`, so no test dependency was installed — to seed the mock
  identity into localStorage and capture all four pages, checking
  `Runtime.exceptionThrown` and console errors along the way.

## Studio S5 — real executor notes (2026-08-23)

- **No new component was installed.** The real executor shells out to the `kubectl`
  and `vcluster` CLIs already recorded above — deliberately the same commands a
  human would run, and the same ones the gate reports document, rather than a
  Python Kubernetes client that could drift from them.
- **`vcluster connect <name> -n <ns> -- <cmd>` is supported and non-interactive**
  in v0.36.1; verified against `vcluster connect --help` rather than assumed. The
  global `-s, --silent` flag is what makes `--print` usable for capturing a
  kubeconfig: without it the CLI writes a "vCluster is up and running" banner to
  STDOUT, which lands inside the file and makes it unparseable.
- **`vcluster delete` needs no confirmation flag** — it is non-interactive by
  default. It does have `--auto-delete-namespace`, which DEFAULTS TO TRUE; Studio
  passes `--auto-delete-namespace=false` because the namespace was created by
  `namespace-pack`, not by vclusterctl, and its removal is a separate ordered step.
- **A CRD cannot be used the instant `kubectl apply` returns.** The object is
  stored, but the API server has still to register the type and refresh discovery;
  creating a custom resource inside that window fails with `no matches for kind`,
  which reads like a missing CRD rather than a race. Every CRD apply is now
  followed by `kubectl wait --for condition=established`.
- **A fresh vCluster contains no KServe CRDs.** `inf-101` and `inf-103` have
  `inferenceservices.serving.kserve.io` only because it was applied by hand during
  step 6 — `scripts/sync-catalog.sh` copies the ClusterServingRuntime CRD and
  nothing else, so that second act is recorded in no script. The real executor
  seeds both CRDs and all 14 runtimes into every tenant cluster it creates.


## Studio R1 — RAG stack notes (2026-08-31)

Everything installed or built for the second catalogue template
(`templates/catalog/rag-stack/`). Recorded at install time, per the §5 working
agreement — every version below was read from the tool or the registry, not
from memory.

### Cluster objects

| Component | Version / reference | Where |
|---|---|---|
| **ClusterServingRuntime `hive-llama-cpp-rag`** | `ghcr.io/ggml-org/llama.cpp@sha256:c3728b9ba0c4c427770eec092fdb1bab1e86e811d578b27440771b8b3207f2c7` (the `:server` build) | [host/serving/runtimes/llama-cpp-rag.yaml](host/serving/runtimes/llama-cpp-rag.yaml) — applied, `kubectl get clusterservingruntime hive-llama-cpp-rag` |
| **Qdrant** | `qdrant/qdrant:v1.19.0` | rendered per project by `templates/catalog/rag-stack/resources/qdrant.yaml.tmpl`; pinned in `app/render.py` (`QDRANT_IMAGE`) |
| **busybox** (corpus staging) | `busybox:1.37` | `corpus-staging-pod.yaml.tmpl`; pinned in `app/render.py` (`STAGE_IMAGE`) |
| **RAG toolkit** (built here) | `hive-local/rag-toolkit:0.1.0` | [studio/images/rag-toolkit/](studio/images/rag-toolkit/), built and `kind load`ed by [scripts/build-rag-toolkit.sh](scripts/build-rag-toolkit.sh) |

### RAG toolkit image contents

`python:3.11-slim` base. Pinned exactly, same rule as
`studio/backend/requirements.txt`. Resolved transitive versions are recorded
because the ones that matter here are compiled and architecture-specific:

| Direct | Version | | Resolved transitive | Version |
|---|---|---|---|---|
| fastapi | 0.141.1 | | onnxruntime | 1.29.0 |
| uvicorn[standard] | 0.52.4 | | numpy | 2.4.6 |
| httpx | 0.28.1 | | tokenizers | 0.23.1 |
| qdrant-client | 1.19.0 | | grpcio | 1.83.1 |
| fastembed | 0.8.0 | | protobuf | 7.36.0 |
| | | | huggingface-hub | 1.29.0 |

### BFF

| Component | Version | Why |
|---|---|---|
| python-multipart | 0.0.32 | `POST /api/uploads` declares an `UploadFile`, and Starlette raises at IMPORT time without it — the console would refuse to start rather than fail the first upload. Pure Python. |

### Notes worth keeping

- **`linux/arm64` throughout — nothing runs under Rosetta, and no
  DEVIATIONS.md row was needed for this work.** Checked per wheel against the
  PyPI JSON API before the image was written, and confirmed by the build:
  every dependency resolved to a `manylinux…aarch64` wheel and pip compiled
  nothing from source. `onnxruntime` was the one worth checking first and it
  publishes `cp311-cp311-manylinux_2_28_aarch64`. The `sentence-transformers`
  fallback the plan allowed for was not needed. `docker image inspect` reports
  the built image as `linux/arm64`, and the build script asserts that on every
  run.
- **Python 3.11, not 3.12+, and it is load-bearing.** fastembed's numpy pin for
  3.11 resolves to numpy 2.4.x, which is where the aarch64 wheels are; numpy
  2.5 dropped 3.11 and requires `>=3.12`. A 3.12 base would work too but would
  move the whole dependency set.
- **`qdrant-client` 1.19.0 has NO `search()` method.** It was removed in favour
  of `query_points()`, which returns a `QueryResponse` whose `.points` are
  `ScoredPoint`s (`.id`, `.score`, `.payload`). Verified by reading the
  installed package, not recalled. `recreate_collection()` still exists but is
  deprecated ("will be removed in the future"), so the ingester does
  delete-if-exists then create.
- **fastembed's cache directory is a CONSTRUCTOR ARGUMENT.** `cache_dir=` is
  unconditional; the `FASTEMBED_CACHE_PATH` environment variable is consulted
  only when `cache_dir` is `None` (0.8.0, `common/utils.py:define_cache_dir`).
  The toolkit passes it explicitly. `local_files_only=True` is what keeps the
  query path offline, and `HF_HUB_OFFLINE=1` is set in the manifest as a second
  lock.
- **A read-only `models` mount works with fastembed.** Verified by running the
  ingester in a container with `-v …:/mnt/models:ro`: `define_cache_dir` calls
  `mkdir(parents=True, exist_ok=True)`, which the kernel answers with `EEXIST`
  for an existing directory regardless of the mount being read-only, so
  `exist_ok=True` swallows it.
- **`BAAI/bge-small-en-v1.5` is the exact fastembed registry id, and its
  dimension is 384.** Read out of fastembed 0.8.0's own
  `text/onnx_embedding.py` (`DenseModelDescription(model="BAAI/bge-small-en-v1.5",
  dim=384, sources=ModelSource(hf="qdrant/bge-small-en-v1.5-onnx-q"))`). It is
  also fastembed's default model.
- **Qdrant's readiness path is `/readyz`, not `/healthz`.** Probed against a
  running `v1.19.0` container: `/readyz` → 200 "all shards are ready",
  `/healthz` → 200, `/livez` → 200, and **`/health` → 404**. The point count in
  `GET /collections/{name}` is at **`.result.points_count`**; the sibling
  `indexed_vectors_count` is a different number that stays 0 until the HNSW
  indexing threshold is passed, so reading it would report a healthy small
  index as empty.
- **Knative's cluster-local gateway here is `kourier-internal` in
  `kourier-system`.** `kubectl get svc -n kourier-system` returns two: `kourier`
  (LoadBalancer, external) and `kourier-internal` (ClusterIP). The executor
  discovers it by TYPE and logs what it found, rather than hard-coding a name
  that is a Kourier packaging detail.
- **KServe uses a runtime's `args` verbatim and renders `{{.Name}}` in them.**
  Confirmed by reading the installed `kserve-sklearnserver`, whose three args
  are exactly what its pod runs. Upstream's own
  `kserve-huggingfaceserver-multinode` runs `bash -c '<script> $0 $@'` with
  templated `args`, which is the pattern `hive-llama-cpp-rag` follows to get the
  InferenceService's name into `--alias`.
- **`llama-server`'s flags were read from the binary**
  (`docker run --rm ghcr.io/ggml-org/llama.cpp:server --help`):
  `-c, --ctx-size N`, `-m, --model FNAME`, `-a, --alias STRING`, `--host`,
  `--port` (default 8080). The image is Ubuntu-based and has `/bin/sh`, which
  is what makes the one-gguf glob possible.
- **`kubectl cp` needs `tar` inside the target container.** It is implemented as
  `tar cf - … | kubectl exec -i -- tar xf - -C …`. busybox has a `tar` applet
  (verified by running it), which is why the staging pod is busybox rather than
  something smaller.
- **The catalogue's `qwen2.5-0.5b-instruct-q4` `seedUrl` RESOLVES ANONYMOUSLY.**
  Fetched on 2026-08-31: **HTTP 200, 491,400,032 bytes** (469 MiB). That closes
  the first of the two questions `models.yaml` flags as unverified; the second
  — that a `gguf` runtime exists — is now answered for the RAG template by
  `hive-llama-cpp-rag`, and remains open for `inference-endpoint`, which still
  points at a `hive-llama-cpp` this repository does not contain.

## Studio R2 — RAG in the console (2026-08-31)

No new runtime dependency. The console still ships **react, react-dom and
react-router-dom** and nothing else, which is the point: a drop zone, a sources
panel and a knowledge-base card are all ordinary React, and adding a component
library to draw them would have been a dependency for the life of the project in
exchange for a week of styling.

| Component | Version | Where | Note |
|---|---|---|---|
| jsdom | 30.0.1 | `studio/frontend` **dev only, `--no-save`** | Needed by `scripts/studio-ui-check.sh` to mount the console's components and click things. Deliberately NOT in `package.json` — a 30MB implementation of the browser has no business in the dependency list of a page that runs in a browser. The script installs it if it is missing. |

### Notes worth keeping

- **The console has no test runner and does not need one.**
  `scripts/studio-ui-check.sh` compiles `studio/frontend/tests/ui-check.jsx` with
  **the project's own Vite config** (`vite build --ssr`) and runs the bundle in
  Node against a jsdom document. Same JSX transform, same resolution, no second
  toolchain to keep in step with the first. The whole runner is 30 lines.
- **`navigator` cannot be assigned on modern Node's `globalThis`** — it is a
  getter-only property, so `globalThis.navigator = dom.window.navigator` throws
  `TypeError: Cannot set property navigator of #<Object> which has only a
  getter`. It has to be `Object.defineProperty`. Node 26.7.0.
- **jsdom implements no layout, so there is no `scrollIntoView`.** Both
  playgrounds call it to keep the newest turn in view, and the mount fails
  without a stub. The runner installs a no-op on `Element.prototype`.
- **React 19 requires `globalThis.IS_REACT_ACT_ENVIRONMENT = true`** before
  `act()` will drive updates, and `act` is imported from `react` rather than from
  `react-dom/test-utils`.
- **The UI fixtures are captured from the running BFF, not written by hand.**
  `studio/frontend/tests/fixtures/*.json` are the exact bodies of
  `GET /api/templates/{id}` and `GET /api/models`, so the wizard under test
  renders the real catalogue. Recapture after a template change:
  `curl -s localhost:8000/api/templates/rag-stack | python3 -m json.tool > studio/frontend/tests/fixtures/template-rag-stack.json`
- **`python-multipart` 0.0.32** was already recorded at R1 and is what makes
  `POST /api/uploads` parse a form. Its ceilings changed at R2 — **20 files, 5MB
  in total, `.md`/`.txt` only, all-or-nothing** — down from R1's 200 files and
  25MB, because a corpus is embedded on CPU in the ingestion Job and an
  unbounded upload is a way to make a project fail slowly rather than fast.
