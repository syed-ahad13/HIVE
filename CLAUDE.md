# HIVE-LOCAL

## 1. Project summary

HIVE is a multi-tenant GPU inference platform being built by the **KSL AI Platforms team**.
Roughly **20 university research groups** — referred to as *PIs*, the tenants — will each
receive their **own Kubernetes control plane**, delivered as a **vCluster** running on **one
shared physical host cluster**. All tenants deploy models through **one central KServe
installation** on the host. There is never one KServe per tenant.

**HIVE-LOCAL** reproduces that architecture on a single MacBook M2 Pro (Apple Silicon, macOS,
Docker Desktop) using **exactly two tenants** drawn from the project's user stories:

| Tenant | Archetype | Behavior | Tier | Allocation |
|---|---|---|---|---|
| **Inf-101** | PI A | Long-lived, **always-on** LLM service | Medium = 1 GPU | 90 days |
| **Inf-103** | PI C | Bursty, **scale-to-zero** service | Small = 1 GPU locally | 60 days |

This local build is a rehearsal: it will later be **migrated to the official platform**.
Therefore two rules dominate everything else — **every artifact is a declarative file committed
to git**, and **every local substitution for a production component is logged** in
[DEVIATIONS.md](DEVIATIONS.md).

## 2. Architecture invariants (non-negotiable)

1. **Cluster topology.** One `kind` host cluster named **`hive-host`**. Two vClusters:
   **`inf-101`** in host namespace **`p-inf-101`**, and **`inf-103`** in host namespace
   **`p-inf-103`**.
2. **One KServe.** Exactly **ONE** KServe installation, on the **host**, running in
   **Serverless (Knative) mode** for everything. No per-tenant KServe. No RawDeployment mode.
3. **GPU request surface.** Tenants request GPUs **only** as
   `resources.limits["nvidia.com/gpu"]`. Locally this is a **fake extended resource patched onto
   node capacity**. **Never introduce a device plugin.** The request surface must be byte-identical
   to production so tenant manifests migrate unchanged.
4. **Ceilings.** Tenant ceilings are enforced by a **`ResourceQuota` in the tenant's HOST
   namespace**, at **admission time** — not by post-hoc reconciliation.
5. **Network isolation.** Tenant namespaces carry a **default-deny ingress `NetworkPolicy`**,
   allowing only: **same-namespace**, **`kourier-system`**, **`knative-serving`**, and **`kserve`**.
6. **Lifetime.** Tenant lifetime is governed by the annotation **`hive.ksl/expiry`**
   (**RFC3339, UTC**) on the **tenant namespace**, enforced by a **host `CronJob`** driving the
   states **Active → Draining → Suspended → Deleted**. **Sleep state NEVER pauses this clock.**
7. **Archetype behavior.** Archetype behavior lives in **Knative annotations on the
   `InferenceService`**:
   - **T-AO** (always-on, Inf-101): `autoscaling.knative.dev/min-scale: "1"`
   - **T-SZ** (scale-to-zero, Inf-103): `autoscaling.knative.dev/min-scale: "0"`, with
     **window** and **scale-down-delay** of **60s locally** (production: **30m**).

## 3. Local-vs-production deviations

| Local | Production |
|---|---|
| Fake GPU extended resource patched onto node capacity | NVIDIA GPU Operator + DRA (**same request surface**: `limits["nvidia.com/gpu"]`) |
| `kind` cluster + Calico CNI | Rocky Linux on bare metal |
| `local-path` StorageClass | WekaFS CSI with directory quotas |
| Kourier + `kubectl port-forward` | MetalLB + Kong, with Keycloak tokens |
| 60s idle windows (window / scale-down-delay) | 30m idle windows |
| 10-minute TTL grace period | 14-day TTL grace period |
| No identity layer | Keycloak-backed identity and token issuance |

The authoritative, append-only log of these substitutions lives in [DEVIATIONS.md](DEVIATIONS.md).

## 4. Repo layout

```
hive-local/
├── CLAUDE.md          # this file — architecture contract and working agreements
├── DEVIATIONS.md      # append-only log of every local substitution for production
├── VERSIONS.md        # append-only log of every installed component + version
├── host/
│   ├── serving/       # the single central KServe + Knative Serving + Kourier install
│   ├── tenancy/       # host namespaces, vCluster values, ResourceQuotas, NetworkPolicies, RBAC
│   └── reconciler/    # host CronJob enforcing hive.ksl/expiry: Active→Draining→Suspended→Deleted
├── templates/         # reusable archetype templates (T-AO, T-SZ) parameterized per tenant
│   └── catalog/       # HIVE Studio use-case templates + curated model list (§6)
├── tenants/
│   ├── inf-101/       # PI A — always-on, Medium (1 GPU), 90-day allocation
│   └── inf-103/       # PI C — scale-to-zero, Small (1 GPU local), 60-day allocation
├── studio/            # HIVE Studio — the PI-facing console: BFF + frontend (§6)
├── scripts/           # wrapper scripts (the only sanctioned place for imperative commands)
├── tests/             # verification of the invariants in §2
└── docs/              # design notes, runbooks, migration notes
```

## 5. Working agreements

- **Record every installed component version in [VERSIONS.md](VERSIONS.md) at install time** —
  not later, not from memory.
- **Never delete the `kind` cluster or any `p-inf-*` namespace without asking me first.**
- **All cluster changes go through files committed in this repo**, applied with `kubectl` or
  `helm`. **No imperative one-offs**, except where a script in `scripts/` wraps them.
- **This is macOS arm64.** Before choosing any container image, **confirm it publishes
  `linux/arm64`.** Docker Desktop can emulate `amd64` via Rosetta, but whenever that emulation is
  relied upon, **note it in [DEVIATIONS.md](DEVIATIONS.md)**.
- **When unsure of a tool's current flag or field names, consult that tool's built-in help
  (`--help`) or bundled docs rather than guessing.**

## 6. HIVE Studio

### Purpose

**HIVE Studio is the PI-facing console** — the layer that turns "I have a model and a
research deadline" into a running endpoint without the PI learning Kubernetes. It presents a
**catalogue of use-case templates**, drives a **schema-driven deploy wizard** from the chosen
template's `parameters`, performs **real provisioning** against the host cluster, and hands
back a **playground** for hitting the endpoint. Studio is a *client of the platform, not a
second platform*: it creates exactly the objects a HIVE tenant would have created by hand
(host namespace pack, vCluster, InferenceService), it obeys every invariant in §2 rather
than working around any of them, and it holds no authority the platform does not already
enforce. It sets the `hive.ksl/expiry` clock; the host reconciler is what honours it. It
writes a `ResourceQuota`; admission control is what enforces it. Everything Studio produces
is a file in this repo before it is a thing in the cluster, so the rehearsal-to-production
rule from §1 — every artifact is a declarative file committed to git — still holds with a
web UI in front of it.

### Directory map

```
studio/
├── backend/           # BFF: catalogue, wizard schema, provisioning pipeline, registry
│   └── studio.db      # SQLite — intent and history ONLY, never status (see invariant b)
└── frontend/          # Vite dev server; catalogue, wizard, project page, playground

templates/catalog/     # the catalogue is DATA, not code — no template lives in the backend
├── models.yaml                    # the curated, CLOSED model list (governance boundary)
├── inference-endpoint/            # maturity: available — the one template that provisions
│   ├── template.yaml              # UseCaseTemplate: parameters, resources, outputs, lifecycle
│   └── resources/                 # Python string.Template fragments, rendered per deploy
│       ├── namespace-pack.yaml.tmpl    # Namespace + ResourceQuota + LimitRange + PVCs
│       ├── model-seed-job.yaml.tmpl    # Job that pulls the model into the models PVC
│       └── inference-service.yaml.tmpl # the workload itself
├── rag-stack/                     # maturity: coming-soon — sketch only, provisions nothing
│   └── template.yaml
└── fine-tune/                     # maturity: coming-soon — sketch only, provisions nothing
    └── template.yaml

tenants/{project}/rendered/        # what Studio actually applied, per project (invariant a)
```

**`vcluster` is a pipeline step but NOT a fragment**, and deliberately so: a vCluster is not
created by applying a manifest. The pipeline shells out to
`vcluster create $project -n p-$project -f templates/t-ao-values.yaml` (always-on) or
`-f templates/t-sz-values.yaml` (on-demand), reusing the two archetype values files step 6
already proved. `--connect=false --add=false` are both required; `--add=false` in particular
is mandatory offline (see VERSIONS.md, step 6 notes). This is the one imperative call in the
pipeline, and the backend executor wrapping it is the Studio equivalent of the `scripts/`
carve-out in the §5 working agreements.

### Ports

| Port | What | Notes |
|---|---|---|
| **8000** | Studio BFF (backend) | The only thing that talks to the cluster. |
| **5173** | Frontend (Vite dev server) | Proxies `/api` -> `http://localhost:8000`, so the browser is same-origin and there is no CORS layer to get wrong. |
| **18080** | Internal data-path `kubectl port-forward` | How the BFF reaches Kourier to invoke a model. Deliberately **not** 8080: it must never collide with a port a PI is forwarding by hand, and it is an internal detail the UI never mentions. |

The playground calls the model **through the BFF over 18080**, never from the browser
directly. Local endpoints are only reachable via a port-forward with a `Host` header, which
is a local shortcut (Kourier + `kubectl port-forward` stands in for MetalLB + Kong — §3), so
keeping it server-side means the browser never learns a URL shape that will not exist in
production.

### Environment variables

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `STUDIO_EXECUTOR` | `mock` \| `real` | **`mock`** | `mock` runs the pipeline without touching the cluster. `real` provisions for real. |
| `STUDIO_FAIL_STEP` | a step id, or `teardown:<step id>` | unset | Failure injection: the named step fails deliberately. A bare step id affects **provisioning only** — teardown needs the `teardown:` prefix, so that injecting a build failure can never disable *Delete and start over*, the documented way out of one (see [docs/studio-failure-proof.md](docs/studio-failure-proof.md) §5). |

**`mock` is the default on purpose.** The dangerous direction is provisioning by accident, so
the safe mode is the one you get by forgetting to set anything; `real` must be an explicit,
deliberate act. `STUDIO_FAIL_STEP` exists because the failure path is a *feature* — a PI must
see which step failed and why, and a rollback must actually roll back — and that path is
otherwise almost impossible to exercise on demand.

### Canonical pipeline step ids

```
approve -> namespace-pack -> model-seed -> vcluster -> inference-service -> ready-check
```

These six strings are **one vocabulary shared by four places**: the provisioning UI's
progress list, the registry's history rows, `STUDIO_FAIL_STEP`, and `teardownOrder` in
`template.yaml`. They must not be renamed casually or spelled differently in any of them.

- **`approve`** — the gate before anything is created: validate parameters, reject reserved
  project names, confirm the project does not already exist. It is a real step so that a
  rejection appears in history as a rejection, rather than as a deployment that never started.
- **`namespace-pack`** — render and apply the host-side ground. Must precede everything: the
  LimitRange it installs is what makes every later pod admissible at all.
- **`model-seed`** — run the seed Job and wait for completion. The only network egress in the
  pipeline, and its URL can only be a `seedUrl` from `models.yaml`.
- **`vcluster`** — the imperative step described above.
- **`inference-service`** — apply the workload into the PI's vCluster, where they are the author.
- **`ready-check`** — wait for readiness and read `.status.url` **live**. Never construct the
  URL: local URLs carry a `-predictor` segment that production will not have (DEVIATIONS.md),
  so anything that assembles a hostname by hand breaks at migration.

### Invariants

These sit alongside §2 and are equally non-negotiable.

- **(a) Render before apply.** Studio writes every manifest into
  `tenants/{project}/rendered/` **before** applying it, and applies the file it wrote. Never
  imperative-only, never a manifest that existed solely in memory. A Studio-provisioned
  project must be as reviewable, diffable and reproducible as a hand-written one — otherwise
  the console quietly breaks the §1 rule the entire rehearsal rests on.
- **(b) Status truth is always read live from the cluster.** The registry stores **intent and
  history only** — who asked for what, when, and how the pipeline went. It never caches
  readiness, replica counts, URLs or expiry state. The cluster is the single source of truth
  for what *is*; SQLite records what was *asked for*. A cached "Ready" that has since scaled
  to zero, been evicted, or been deleted is worse than no status at all.
- **(c) Reserved project names are rejected.** Exactly: **`inf-101`**, **`inf-103`**,
  **`default`**, and any name starting **`kube-`** or **`hive-`**. Enforced server-side at the
  `approve` step, not by the wizard's regex — a client-side check is a hint, not a control.
- **(d) Never touch `p-inf-101` or `p-inf-103`.** The two hand-built tenants are the
  reference implementation the whole rehearsal is measured against. Studio reads them at most;
  it never writes, patches or deletes anything in them. Studio only acts on namespaces
  carrying `hive.ksl/template: studio-inference`. This extends the §5 agreement about not
  deleting `p-inf-*` namespaces to a program that could do it far faster than a human.
- **(e) UI copy is user-side language, per DD-002.** Write what the PI gets, not what
  Kubernetes does: **"your private cluster (vCluster)"**, **"sleeps when idle"**, **"wakes in
  about 15 seconds"**, **"your project is removed automatically when this runs out"**. The
  parenthetical keeps the real term discoverable for a PI who wants it, without making it the
  price of entry. **Every local shortcut shows an amber banner** — fake GPUs, 60s idle windows
  standing in for 30m, port-forward instead of a real gateway, no identity layer. A demo that
  hides its scaffolding teaches the audience something false, and these are exactly the rows
  in DEVIATIONS.md that must be swapped at migration.

### Design tokens

From the architecture poster. Same palette, so the console and the diagrams read as one system.

| Token | Hex | Use |
|---|---|---|
| navy | `#1f3864` | primary — headings, structure, the host/platform layer |
| green | `#0f6e56` | success, Ready, healthy |
| amber | `#8a5a0d` | **local shortcuts and deviations**, warnings, coming-soon |
| red | `#a03030` | failure, expired, destructive actions |

Each has a **soft fill** — the same hue at low saturation for backgrounds, badges and step
rows — so state is carried by a filled region rather than by coloured text alone. Amber is
reserved: it means "this is not how production works", and using it for ordinary warnings
would dilute the one signal invariant (e) depends on.

**System font stack**, no web fonts:

```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
             "Helvetica Neue", Arial, sans-serif;
```

Nothing is fetched from a CDN: the console must work on a laptop with no internet, which is
exactly the demo condition it exists for.
