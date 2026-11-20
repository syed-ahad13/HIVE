# Catalogue curation — how a thing gets onto the menu

**ADR-11: a menu, not a kitchen.** A PI picks from a reviewed list; they do not
supply a URL, an image, or a runtime. This document records what that costs the
platform team, because the price of a closed list is that *somebody has to open
it deliberately*, and that act needs a written procedure or it becomes a Slack
message and a `kubectl apply` nobody can find later.

There are three menus, and they are curated the same way: **a commit, reviewed,
recorded in [VERSIONS.md](../VERSIONS.md), never a runtime input.**

| Menu | File | What a PI sees |
|---|---|---|
| Use cases | `templates/catalog/*/template.yaml` | the catalogue cards |
| Models | [`templates/catalog/models.yaml`](../templates/catalog/models.yaml) | the wizard's model dropdowns |
| Serving runtimes | `host/serving/runtimes/*.yaml` | nothing — it is the platform's half of a model entry |

The third is the one this document exists for. The first two are visibly
tenant-facing and their governance is written into the files themselves. A
runtime is invisible to a PI, which makes it the easiest of the three to add
carelessly.

---

## Why a runtime is a curation decision and not an implementation detail

A `ClusterServingRuntime` is **cluster-scoped, on the host**. A tenant cannot
author one: they are cluster-admin inside their own vCluster and that vCluster's
copy of the runtime catalogue is a read-only mirror of the host's (DEVIATIONS.md,
Fallback A). So the set of runtimes is chosen by the platform team on every
tenant's behalf, and adding one means asserting, for everybody:

* **that its image is real and pinned** — a runtime naming a floating tag changes
  what tenants run with no commit anywhere in this repository;
* **that it publishes `linux/arm64`**, or that the Rosetta reliance is logged
  (CLAUDE.md §5, and DEVIATIONS.md already carries a row for the two upstream
  runtimes that do not);
* **that its resource requests fit inside a tenant tier**, because the runtime's
  own `resources` stanza is what every tenant pod using it will request against
  their quota;
* **that nothing about it lets one tenant affect another** — it is shared.

None of that is discoverable from the manifest after the fact. It is discoverable
from the commit that added it, if the commit was made under this procedure.

---

## The procedure

1. **Author the file** under `host/serving/runtimes/<name>.yaml`, one runtime per
   file. Cluster-scoped platform objects live in `host/`, never in `tenants/`.
2. **Verify, do not recall.** Every value that could be wrong gets checked
   against the tool or the cluster, and the check is written into the file as a
   comment next to the value:
   * architectures — `docker manifest inspect <image>`
   * flags — the binary's own `--help`, run from the image
   * the digest — `docker inspect --format '{{index .RepoDigests 0}}'`
   * how KServe will treat the spec — read an installed runtime, do not assume
3. **Pin by digest.** A tag is a moving target; the digest is the verification.
4. **`autoSelect: false` unless the runtime is genuinely the only sane answer for
   its format.** Two runtimes claiming one `modelFormat` and both auto-selectable
   means a priority tie-break decides what a tenant runs.
5. **Apply it, and record the version in [VERSIONS.md](../VERSIONS.md) at install
   time** — the §5 working agreement, not later and not from memory.
6. **Wire it into the menu above it.** A runtime nothing references is dead
   weight: a `models.yaml` entry must name it in `runtime:`, or a template must
   pin it. Otherwise the addition is unreviewable — there is no tenant-visible
   change to review.

---

## The record

### `hive-llama-cpp-rag` — 2026-08-31

Added for the RAG stack template (`templates/catalog/rag-stack/`), which needs a
gguf chat runtime with a **≥ 4096-token context**: retrieval works by putting
passages in the prompt, and a context that is too small truncates them silently
rather than failing.

* File: [`host/serving/runtimes/llama-cpp-rag.yaml`](../host/serving/runtimes/llama-cpp-rag.yaml)
* Image: `ghcr.io/ggml-org/llama.cpp@sha256:c3728b9b…` (the `:server` build),
  pinned by digest. **`linux/arm64` confirmed** by `docker manifest inspect`
  (amd64, arm64, s390x) — native on Apple Silicon, no Rosetta row required.
* Format: `gguf`, `autoSelect: false` — pinned per model entry in `models.yaml`.
* Context: `--ctx-size 4096`, verified as the correct flag from
  `llama-server --help`.
* Referenced by: `templates/catalog/models.yaml` → `qwen2.5-0.5b-instruct-q4-rag`.

**It was authored from scratch, not widened from a base runtime.** The task that
requested it assumed a `hive-llama-cpp` existed to copy; it does not. There is no
`host/serving/runtimes/llama-cpp.yaml` in this repository and
`kubectl get clusterservingruntime` returns the 14 upstream KServe runtimes and
nothing else — none of which declares `gguf`. Step 9b was never carried out here,
which [`docs/studio-real-proof.md`](studio-real-proof.md) §7 already records and
which is why `qwen2.5-0.5b-instruct-q4` is flagged UNVERIFIED in `models.yaml`.

**Outstanding, and deliberately not done here:** the sibling `hive-llama-cpp`
(2048-context, for the plain `inference-endpoint` template) is still missing, so
the non-RAG chat model in the catalogue still cannot be deployed. Authoring it is
Step 9b's job, and when it happens the two files should differ only in
`--ctx-size` and the memory allowance.
