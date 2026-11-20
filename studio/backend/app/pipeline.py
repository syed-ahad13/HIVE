# ===========================================================================
# HIVE STUDIO BFF — THE PROVISIONING PIPELINE
# ===========================================================================
# Turns a validated form submission into a sequence of steps, runs them, and
# records what happened as it happens.
#
# ---------------------------------------------------------------------------
# THE SIX STEP IDS ARE A SHARED VOCABULARY, NOT LABELS
# ---------------------------------------------------------------------------
# CLAUDE.md §6: these strings are one vocabulary shared by four places — the
# provisioning UI's progress list, the registry's history rows, STUDIO_FAIL_STEP,
# and `teardownOrder` in template.yaml. They are defined once in catalog.py and
# imported here; nothing in this file spells one out as a literal except the
# ordering table below, which IS the definition of the order.
#
# ---------------------------------------------------------------------------
# WHY AN EXECUTOR PROTOCOL RATHER THAN if mock: ... else: ...
# ---------------------------------------------------------------------------
# The pipeline is the part that must be identical in both modes: same steps, same
# order, same recording, same failure handling, same rollback. Only the six method
# bodies differ. Splitting it as "what happens" (this runner) versus "how it
# happens" (an executor) means the mock run genuinely rehearses the real one — the
# thing being demonstrated is the pipeline, and a mock that took a different path
# through it would demonstrate nothing.
#
# It also means S5's RealExecutor is a new class implementing the same six methods,
# with no edit to the runner, the registry writes, or the API.
#
# ---------------------------------------------------------------------------
# WHY STEPS ARE WRITTEN TO SQLITE AS THEY HAPPEN
# ---------------------------------------------------------------------------
# Never only at the end. A pipeline that recorded its history on completion would
# have nothing to show during the 23 seconds it is running — which is exactly when
# a PI is watching — and would lose everything if the process died mid-run. The
# progress list and the failure path are features (CLAUDE.md §6), and both are made
# of these rows.
# ===========================================================================

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db
from .catalog import PIPELINE_STEPS
from .schema import ModelEntry, UseCaseTemplate

# ---------------------------------------------------------------------------
# THE PLAN
# ---------------------------------------------------------------------------
# A template's `resources` list names RENDER FRAGMENTS; the pipeline speaks STEP
# IDS. The two overlap but are not the same vocabulary, and the difference is
# load-bearing in one place: the fragment is `model-seed-job` (it renders a
# Kubernetes Job) while the step is `model-seed` (it runs that Job and waits).
# Mapping them explicitly here — rather than string-munging the suffix — means an
# unrecognised resource is an error instead of a silently-skipped step.
RESOURCE_TO_STEP: dict[str, str] = {
    "namespace-pack": "namespace-pack",
    "model-seed-job": "model-seed",
    "model-seed": "model-seed",
    "vcluster": "vcluster",
    "inference-service": "inference-service",
    # --- RAG stack ---------------------------------------------------------
    # These four happen to be spelled identically in both vocabularies, and are
    # listed anyway rather than falling back to identity: an unrecognised
    # resource must raise, and a `.get(resource, resource)` default would turn a
    # typo in a template into a step that does nothing.
    "vector-db": "vector-db",
    "corpus-upload": "corpus-upload",
    "ingestion": "ingestion",
    "rag-api": "rag-api",
    # --- batch inference ---------------------------------------------------
    "dataset-upload": "dataset-upload",
    "batch-run": "batch-run",
    "collect-results": "collect-results",
}

# The gate before anything is created. A REAL step, so that a rejection appears
# in history as a rejection rather than as a deployment that never started
# (CLAUDE.md §6).
FIRST_STEP = "approve"

# ---------------------------------------------------------------------------
# GATES — THE STEPS THAT WAIT FOR SOMETHING TO BECOME TRUE
# ---------------------------------------------------------------------------
# A gate is a step that creates nothing and instead waits for what the step
# before it created to be usable, then reads the facts that later steps need.
# It is inserted IMMEDIATELY AFTER the step it gates, and that position is
# load-bearing rather than tidy.
#
#   inference-service -> ready-check      waits for Ready and reads .status.url
#   rag-api           -> rag-ready-check  waits for /healthz and asks one
#                                         real question
#
# THIS REPLACED A HARD-CODED "ready-check IS ALWAYS LAST". For
# inference-endpoint the two rules produce exactly the same six-step plan, so
# nothing about that template changed. For rag-stack they differ completely:
# `rag-api` needs the chat model's hostname, which only `ready-check` can
# supply, so ready-check must run in the MIDDLE — after the InferenceService and
# before the service that calls it. A rule that appended it at the end would
# have produced a RAG service configured with the sentinel hostname and a
# pipeline that reported success.
STEP_GATES: dict[str, str] = {
    "inference-service": "ready-check",
    "rag-api": "rag-ready-check",
}


def plan_steps(template: UseCaseTemplate) -> list[str]:
    """The ordered step ids for one provisioning run.

    Derived from the template rather than hard-coded, so a template that declares
    fewer resources gets a shorter pipeline without any change here — and a
    template that declares more gets a longer one, which is the whole reason the
    RAG stack needed no new plan logic beyond the gate table above.
    """
    plan = [FIRST_STEP]
    for resource in template.spec.resources or []:
        step = RESOURCE_TO_STEP.get(resource)
        if step is None:
            raise ValueError(
                f"template '{template.metadata.id}' declares resource {resource!r}, "
                f"which maps to no pipeline step (known: "
                f"{', '.join(sorted(RESOURCE_TO_STEP))})"
            )
        if step not in plan:
            plan.append(step)
        gate = STEP_GATES.get(step)
        if gate and gate not in plan:
            plan.append(gate)

    unknown = [s for s in plan if s not in PIPELINE_STEPS]
    if unknown:  # pragma: no cover — guarded by the catalogue loader already
        raise ValueError(f"plan contains non-canonical step ids: {unknown}")
    return plan


def terminal_success_state(template: UseCaseTemplate) -> str:
    """What a finished run of THIS template is called when it worked.

    Read from `lifecycle.kind` rather than decided by the executor, for the same
    reason the step plan is read from `resources`: the template knows what shape
    of thing it produces, and the pipeline should not have to be told twice.

    A service becomes `ready` — it is up and will stay up. A run becomes
    `completed` — it finished and nothing is running. See DEPLOYMENT_STATES in
    app/db.py for why conflating the two would make the console lie.
    """
    lifecycle = template.spec.lifecycle
    return "completed" if (lifecycle and lifecycle.is_run) else "ready"


def plan_teardown(template: UseCaseTemplate) -> list[str]:
    """The ordered step ids for tearing one down.

    Read from the template's `teardownOrder`, which exists because the order
    matters and is not simply the reverse of creation: the workload goes first so
    it releases its GPU while the control plane that owns it is still up, and the
    namespace goes last because it takes the quota and the PVCs with it.
    """
    if template.spec.lifecycle and template.spec.lifecycle.teardownOrder:
        return list(template.spec.lifecycle.teardownOrder)
    # No declared order: undo the plan backwards, minus the bookends — `approve`
    # and every gate create nothing and therefore have nothing to remove.
    skip = {FIRST_STEP, *STEP_GATES.values()}
    return [s for s in reversed(plan_steps(template)) if s not in skip]


# ---------------------------------------------------------------------------
# WHAT A STEP IS GIVEN, AND WHAT IT RETURNS
# ---------------------------------------------------------------------------


@dataclass
class StepContext:
    """Everything a step needs, assembled once per run.

    Passed to every step rather than each step reaching for globals, so a step is
    a pure function of its inputs and can be exercised on its own.
    """

    project: str
    template: UseCaseTemplate
    params: dict[str, Any]
    # The SERVED model — the chat or predict entry the PI chose.
    model: ModelEntry | None
    # The `embed` entry, for templates that index documents. A separate field
    # rather than a second lookup wherever it is needed: the executor must not
    # reach into the catalogue, both for the same reason the renderer must not —
    # a step is a function of the context it was handed, and can be exercised on
    # its own.
    embed_model: ModelEntry | None = None
    # Accumulates as the run proceeds, so a later step can read what an earlier
    # one produced. In a RAG run this is how `rag-api` learns the chat model's
    # hostname that `ready-check` read live from the cluster — the one value
    # that cannot be known before the deploy.
    outputs: dict[str, Any] = field(default_factory=dict)

    # Append one line to the CURRENT step's log, immediately.
    #
    # A mock step finishes in seconds and can return its whole log at the end. A
    # real one shells out to `vcluster create` and waits ten minutes for an image
    # pull, and a PI watching a step sit at "running" with an empty log cannot
    # tell progress from a hang. The runner wires this to db.append_step_log so
    # output reaches the page as it happens.
    #
    # Defaults to a no-op so a step can be exercised outside the runner.
    emit: Callable[[str], None] = lambda _line: None

    # Replace the CURRENT step's progress snapshot, immediately.
    #
    # A sibling of `emit`, and the difference between them is the difference
    # between a log and a gauge. `emit` appends a line to a history; this
    # replaces one answer to "how far along is it?". A step that runs for an
    # hour needs both: the log says what happened, the snapshot says where it is.
    #
    # Defaults to a no-op so a step can be exercised outside the runner, exactly
    # as `emit` does.
    progress: Callable[[dict | None], None] = lambda _snapshot: None


@dataclass
class StepResult:
    """What a step reports.

    A step signals failure by RAISING, never by returning ok=False. Two reasons:
    an exception cannot be forgotten by a caller that does not check a flag, and
    the traceback is available for the log. `ok` exists because the task specifies
    the shape and because an explicit True documents the contract at each return.
    """

    ok: bool
    log: str
    outputs: dict[str, Any] | None = None


# The executor method that implements each step. Step ids carry hyphens, which are
# not legal in Python identifiers, so the mapping is written out rather than
# computed — a computed `.replace("-", "_")` would silently produce a missing
# attribute for a mistyped id instead of failing here.
METHOD_FOR_STEP: dict[str, str] = {
    "approve": "approve",
    "namespace-pack": "namespace_pack",
    "model-seed": "model_seed",
    "vcluster": "vcluster",
    "inference-service": "inference_service",
    "ready-check": "ready_check",
    # --- RAG stack ---------------------------------------------------------
    "vector-db": "vector_db",
    "corpus-upload": "corpus_upload",
    "ingestion": "ingestion",
    "rag-api": "rag_api",
    "rag-ready-check": "rag_ready_check",
    # --- batch inference ---------------------------------------------------
    "dataset-upload": "dataset_upload",
    "batch-run": "batch_run",
    "collect-results": "collect_results",
}


class Executor(ABC):
    """How each step is actually performed.

    Two implementations: MockExecutor (below) and RealExecutor, which renders
    fragments into tenants/<project>/rendered/ and applies them. The runner
    cannot tell them apart.

    EVERY STEP IS ABSTRACT, INCLUDING THE FIVE THE RAG STACK ADDED. A base
    implementation that returned "not applicable" for the RAG steps would be
    convenient and wrong: a template declaring `vector-db` against an executor
    that had not implemented it would report a successful step that did nothing,
    and the deployment would fail two steps later with the RAG service unable to
    reach a Qdrant nobody created. Abstract means that mistake is a TypeError at
    construction, before any project exists.
    """

    name: str = "abstract"

    @abstractmethod
    async def approve(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def namespace_pack(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def model_seed(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def vcluster(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def inference_service(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def ready_check(self, ctx: StepContext) -> StepResult: ...

    # --- RAG stack ---------------------------------------------------------

    @abstractmethod
    async def vector_db(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def corpus_upload(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def ingestion(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def rag_api(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def rag_ready_check(self, ctx: StepContext) -> StepResult: ...

    @abstractmethod
    async def teardown(self, ctx: StepContext, step_id: str) -> StepResult: ...


# ---------------------------------------------------------------------------
# FAILURE INJECTION
# ---------------------------------------------------------------------------


def injected_failure_step() -> str | None:
    """The step STUDIO_FAIL_STEP currently names, or None.

    READ FRESH ON EVERY CALL, and that is the whole design of this function.

    If the value were captured at startup, a failed run could never be retried
    without restarting the server: the variable would still say `vcluster` and the
    retry would fail at exactly the same place. Reading it at the moment each step
    executes means `unset STUDIO_FAIL_STEP` followed by a retry succeeds inside the
    same process — which is what makes the failure path demonstrable in a single
    smoke run rather than requiring a restart between the two halves.

    The cost is that the value can change mid-run. For a deliberate testing hook
    that is a feature, not a hazard.
    """
    value = os.environ.get("STUDIO_FAIL_STEP", "").strip()
    return value or None


def _raise_if_injected(step_id: str) -> None:
    """Fail this PROVISIONING step on purpose if it is the named one.

    Called BEFORE the step does any work, so an injected failure leaves nothing
    half-created — the same shape as a step that fails on its first action.
    """
    if injected_failure_step() == step_id:
        raise RuntimeError("injected failure")


def _raise_if_injected_teardown(step_id: str) -> None:
    """The same hook for teardown, but it must be asked for EXPLICITLY.

    `STUDIO_FAIL_STEP=namespace-pack` means "fail while building". It must NOT
    also mean "fail while removing", and the audit that found this is the reason:
    injecting a failure at namespace-pack broke the provisioning run AND then
    broke "Delete and start over" — the documented way out of a failed run — at
    the identically-named teardown step. The recovery path was sabotaged by the
    same flag that caused the problem, which is the one thing a failure-injection
    hook must never do.

    Teardown failures are still worth testing, so they keep a spelling of their
    own: STUDIO_FAIL_STEP=teardown:vcluster.
    """
    value = injected_failure_step()
    if value and value.startswith("teardown:") and value.split(":", 1)[1] == step_id:
        raise RuntimeError("injected failure (teardown)")


# ---------------------------------------------------------------------------
# THE MOCK EXECUTOR
# ---------------------------------------------------------------------------


class MockExecutor(Executor):
    """Runs the pipeline without touching the cluster.

    THE DEFAULT, deliberately (CLAUDE.md §6): the dangerous direction is
    provisioning by accident, so the safe mode is the one you get by forgetting to
    set anything.

    The sleeps are not padding. They are roughly the shape of the real thing — the
    vCluster control plane really is the slowest step, the model seed really does
    depend on a download — so the progress UI is exercised against a realistic
    rhythm rather than six steps that all complete instantly. A demo where the
    progress list flashes past teaches nothing about what waiting feels like.
    """

    name = "mock"

    # Seconds per step. An inference-endpoint run is ≈ 23s; a RAG run, which
    # has five more steps, is ≈ 54s. Both are roughly the shape of the real
    # thing, and the RAG numbers are not padding either: starting a database is
    # slower than applying a Deployment, `kubectl cp` is quick, and indexing is
    # the longest step because it embeds every chunk on CPU.
    DELAYS: dict[str, float] = {
        "approve": 1.0,
        "namespace-pack": 2.0,
        "model-seed": 5.0,
        "vcluster": 8.0,
        "inference-service": 4.0,
        "ready-check": 3.0,
        # --- RAG stack -----------------------------------------------------
        "vector-db": 6.0,
        "corpus-upload": 3.0,
        "ingestion": 12.0,
        "rag-api": 6.0,
        "rag-ready-check": 4.0,
        # --- batch inference -----------------------------------------------
        # `batch-run` is not in this table: it does not sleep once, it walks a
        # progress bar. See MockExecutor.batch_run.
        "dataset-upload": 3.0,
        "collect-results": 4.0,
    }
    TEARDOWN_DELAY = 1.5

    # The canned figures the mock reports for an index it never built. Chosen to
    # look like the demo corpus rather than to be round: a PI who sees "100
    # chunks" learns nothing, and a number that matches what the real thing
    # would produce makes a mock run a usable rehearsal of the project page.
    MOCK_FILES = 4
    MOCK_CHUNKS = 37

    async def _step(self, step_id: str, log: str) -> StepResult:
        _raise_if_injected(step_id)
        await asyncio.sleep(self.DELAYS[step_id])
        return StepResult(ok=True, log=log)

    async def approve(self, ctx: StepContext) -> StepResult:
        # The approval gate is STUBBED. In production this is where a request waits
        # for a human with authority to grant hardware. Locally it auto-approves,
        # and says so in the log rather than pretending a decision was made.
        return await self._step(
            "approve",
            f"Parameters validated for project '{ctx.project}'.\n"
            f"Reserved-name check passed.\n"
            f"AUTO-APPROVED (stub) — no approval queue exists locally; in production "
            f"this step waits for a platform decision.",
        )

    async def namespace_pack(self, ctx: StepContext) -> StepResult:
        tier = ctx.params.get("tier", "small")
        return await self._step(
            "namespace-pack",
            f"[mock] would render tenants/{ctx.project}/rendered/namespace-pack.yaml\n"
            f"[mock] would apply Namespace p-{ctx.project} (tier={tier})\n"
            f"[mock] would apply ResourceQuota, LimitRange, PVCs models + scratch",
        )

    async def model_seed(self, ctx: StepContext) -> StepResult:
        url = ctx.model.seedUrl if ctx.model else "<no model>"
        return await self._step(
            "model-seed",
            f"[mock] would render and run Job seed-{ctx.project}\n"
            f"[mock] would download {url}\n"
            f"[mock] no network request was made",
        )

    async def vcluster(self, ctx: StepContext) -> StepResult:
        values = (
            "t-ao-values.yaml"
            if ctx.params.get("availability") == "always-on"
            else "t-sz-values.yaml"
        )
        return await self._step(
            "vcluster",
            f"[mock] would run: vcluster create {ctx.project} -n p-{ctx.project} \\\n"
            f"           -f templates/{values} --connect=false --add=false\n"
            f"[mock] the one imperative call in the pipeline",
        )

    async def inference_service(self, ctx: StepContext) -> StepResult:
        min_scale = 1 if ctx.params.get("availability") == "always-on" else 0
        return await self._step(
            "inference-service",
            f"[mock] would render tenants/{ctx.project}/rendered/inference-service.yaml\n"
            f"[mock] would apply InferenceService '{ctx.project}' inside the vCluster\n"
            f"[mock] minReplicas={min_scale}",
        )

    async def ready_check(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("ready-check")
        await asyncio.sleep(self.DELAYS["ready-check"])
        # The synced name follows vCluster's host translation scheme,
        # <name>-x-<namespace>-x-<vcluster>, the same convention step 6 proved and
        # scripts/sync-isvc.sh deliberately mirrors.
        url = f"http://{ctx.project}.mock.hive.local"
        outputs = {
            "endpointUrl": url,
            "syncedName": f"{ctx.project}-x-default-x-{ctx.project}",
            # False because a mock run creates no vCluster, so there is no
            # kubeconfig to hand over. Stated as a field rather than omitted, so the
            # UI can say "not available in mock mode" instead of rendering a gap.
            "kubeconfigAvailable": False,
        }
        # A RAG project has TWO endpoints and the template says so in its
        # `outputs`. Keyed on that declaration rather than on the template id,
        # so a future template that also wants the raw model surfaced gets it by
        # naming the output rather than by being added to a list in here.
        if "chatEndpointUrl" in (ctx.template.spec.outputs or []):
            outputs["chatEndpointUrl"] = url
        return StepResult(
            ok=True,
            log=(
                "[mock] would wait for InferenceService readiness\n"
                "[mock] would read .status.url LIVE — never construct it, because "
                "local URLs carry a -predictor segment production will not have\n"
                f"[mock] endpoint: {outputs['endpointUrl']}  (NOT REAL)"
            ),
            outputs=outputs,
        )

    # ------------------------------------------------------------------
    # RAG STACK
    # ------------------------------------------------------------------

    async def vector_db(self, ctx: StepContext) -> StepResult:
        return await self._step(
            "vector-db",
            f"[mock] would render tenants/{ctx.project}/rendered/qdrant.yaml\n"
            f"[mock] would apply Deployment + Service {ctx.project}-qdrant "
            f"into p-{ctx.project}\n"
            f"[mock] would wait for the rollout and for /readyz to answer",
        )

    async def corpus_upload(self, ctx: StepContext) -> StepResult:
        upload = str(ctx.params.get("corpusUploadId", "") or "").strip()
        source = (
            f"tenants/_uploads/{upload}/"
            if upload
            else "templates/catalog/rag-stack/demo-corpus/ (no upload — the "
                 "demo corpus is used, and a real run says so here too)"
        )
        return await self._step(
            "corpus-upload",
            f"[mock] would start pod {ctx.project}-stage with the scratch volume\n"
            f"[mock] would kubectl cp {source} -> /mnt/scratch/corpus\n"
            f"[mock] would verify the file list and delete the pod\n"
            f"[mock] no files were copied",
        )

    async def ingestion(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("ingestion")
        await asyncio.sleep(self.DELAYS["ingestion"])
        collection = f"{ctx.project}-corpus"
        # The canned line is spelled EXACTLY as ingest.py prints it, so the
        # executor's parser is exercised by a mock run rather than only by a
        # real one. If the marker or the shape ever drifts, a mock smoke run
        # catches it before a cluster does.
        result = (
            'INGEST_RESULT {"files":%d,"chunks":%d,"collection":"%s","dims":384}'
            % (self.MOCK_FILES, self.MOCK_CHUNKS, collection)
        )
        return StepResult(
            ok=True,
            log=(
                f"[mock] would apply Job {ctx.project}-ingest and wait for it\n"
                f"[mock] would chunk, embed on CPU and upsert into '{collection}'\n"
                f"[mock] {result}\n"
                f"[mock] NOTHING WAS INDEXED — these numbers are canned"
            ),
            outputs={
                "collectionName": collection,
                "indexedFiles": self.MOCK_FILES,
                "indexedChunks": self.MOCK_CHUNKS,
            },
        )

    async def rag_api(self, ctx: StepContext) -> StepResult:
        return await self._step(
            "rag-api",
            f"[mock] would resolve the chat model's live URL and Knative's "
            f"cluster-local gateway\n"
            f"[mock] would render tenants/{ctx.project}/rendered/rag-api.yaml\n"
            f"[mock] would apply Deployment + Service {ctx.project}-rag and wait "
            f"for the rollout",
        )

    async def rag_ready_check(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("rag-ready-check")
        await asyncio.sleep(self.DELAYS["rag-ready-check"])
        return StepResult(
            ok=True,
            log=(
                "[mock] would poll /healthz through a temporary port-forward\n"
                "[mock] would ask one smoke question, so the PI's first click is "
                "not the first real request\n"
                "[mock] smoke question: 'What is this collection about?' — NOT ASKED"
            ),
            outputs={
                "ragEndpointUrl": (
                    f"http://{ctx.project}-rag.p-{ctx.project}.svc.cluster.local:8080"
                ),
                # False, not absent. A mock run genuinely did not verify
                # anything, and the project page should be able to say so rather
                # than showing a gap that reads like a missing feature.
                "smokeOk": False,
            },
        )

    # ------------------------------------------------------------------
    # BATCH INFERENCE
    # ------------------------------------------------------------------

    async def dataset_upload(self, ctx: StepContext) -> StepResult:
        source = (ctx.params.get("datasetUploadId") or "").strip()
        where = f"tenants/_uploads/{source}/" if source else "the bundled sample dataset"
        return await self._step(
            "dataset-upload",
            f"[mock] would render tenants/{ctx.project}/rendered/staging-pod.yaml\n"
            f"[mock] would copy {where} into {ctx.project}-stage:/mnt/scratch/input/\n"
            f"[mock] would verify the byte count, then delete the pod\n"
            f"[mock] nothing was copied",
        )

    async def batch_run(self, ctx: StepContext) -> StepResult:
        """A believable ~20 seconds, so the UI can be built without a cluster.

        NOT A SLEEP AND A NUMBER. The progress bar, the "waiting for GPU" state
        and the throughput readout are the three things this template adds to
        the console, and all three are hard to get right against a step that
        jumps from 0 to 100. So the mock walks the same shape a real run walks:
        a couple of seconds queued behind the GPU, then rows accumulating with a
        rate and an ETA that actually move.

        Two seconds of "waiting for GPU" is deliberate and is the detail most
        worth rehearsing: it is amber, it is not an error, and it is the state a
        reviewer is most likely to see for the first time during a demo.
        """
        _raise_if_injected("batch-run")

        total = int(ctx.params.get("maxRows") or 0) or 200
        interface = ctx.model.interface if ctx.model else "chat"

        # --- queued behind the quota ---------------------------------------
        ctx.emit("[mock] would apply the Job: runtime sidecar + driver")
        ctx.progress({
            "percent": 0, "done": 0, "total": total, "failed": 0,
            "rowsPerSec": 0, "etaSeconds": None,
            "detail": "waiting for GPU — your project's GPU is currently in use",
        })
        await asyncio.sleep(2.0)

        # --- the work --------------------------------------------------------
        ctx.emit(f"[mock] would stream {total} rows through the runtime over localhost")
        started = asyncio.get_running_loop().time()
        steps = 12
        failed = 0
        for index in range(1, steps + 1):
            await asyncio.sleep(18.0 / steps)
            done = int(total * index / steps)
            # One plausible failure late on, so the UI's failure column is
            # exercised by the mock rather than only by a real bad run.
            if index == steps - 2:
                failed = max(1, total // 100)
            elapsed = max(1e-6, asyncio.get_running_loop().time() - started)
            rate = done / elapsed
            ctx.progress({
                "percent": int(100 * index / steps),
                "done": done, "total": total, "failed": failed,
                "rowsPerSec": round(rate, 2),
                "etaSeconds": int((total - done) / rate) if rate > 0 else None,
                "detail": None,
            })

        duration = round(asyncio.get_running_loop().time() - started, 2)
        rows = total - failed
        rate = round(rows / duration, 2) if duration else 0.0
        ctx.emit(
            f'[mock] BATCH_RESULT {{"rows":{rows},"failed":{failed},'
            f'"durationSeconds":{duration},"rowsPerSecond":{rate}}}'
        )
        return StepResult(
            ok=True,
            log=(
                f"[mock] {interface} interface, {total} rows requested\n"
                f"[mock] {rows} processed, {failed} failed in {duration}s "
                f"({rate} rows/s)\n"
                f"[mock] NOT REAL — no Job was created and no GPU was held"
            ),
            outputs={
                "rowsProcessed": rows,
                "rowsFailed": failed,
                "durationSeconds": duration,
                "rowsPerSecond": rate,
            },
        )

    async def collect_results(self, ctx: StepContext) -> StepResult:
        retention = int(ctx.params.get("artifactRetentionDays") or 14)
        until = (
            datetime.now(timezone.utc) + timedelta(days=retention)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        # Sizes are invented but PLAUSIBLE, so the results tab can be laid out
        # against something that looks like a real listing.
        rows = int(ctx.params.get("maxRows") or 0) or 200
        artifacts = [
            {"name": "results.jsonl", "bytes": rows * 320},
            {"name": "errors.jsonl", "bytes": max(1, rows // 100) * 180},
            {"name": "run-summary.json", "bytes": 740},
        ]
        result = await self._step(
            "collect-results",
            f"[mock] would copy /mnt/scratch/output/. out to "
            f"tenants/{ctx.project}/artifacts/\n"
            f"[mock] would set hive.ksl/expiry to {until} "
            f"({retention} days from completion)\n"
            f"[mock] no files were fetched",
        )
        return StepResult(
            ok=True,
            log=result.log,
            outputs={"artifacts": artifacts, "resultsAvailableUntil": until},
        )

    async def teardown(self, ctx: StepContext, step_id: str) -> StepResult:
        _raise_if_injected_teardown(step_id)
        await asyncio.sleep(self.TEARDOWN_DELAY)
        return StepResult(ok=True, log=f"[mock] would remove {step_id} for {ctx.project}")


def build_executor(mode: str) -> Executor:
    """The executor for a mode. `real` arrives in S5."""
    if mode == "mock":
        return MockExecutor()
    if mode == "real":
        # Imported lazily so a mock-mode process never loads the module that
        # shells out to kubectl, and so an import error in it cannot stop the
        # console from starting in its safe default mode.
        from .executor_real import RealExecutor

        return RealExecutor()
    raise NotImplementedError(f"unknown executor mode {mode!r}")


# ---------------------------------------------------------------------------
# THE RUNNER
# ---------------------------------------------------------------------------

# Running tasks, by deployment id. Held module-level for two reasons: asyncio only
# keeps a weak reference to a task, so one that nothing holds can be garbage
# collected mid-run; and a retry needs to know whether a run is already in flight.
_TASKS: dict[str, asyncio.Task] = {}

# ---------------------------------------------------------------------------
# ONE PROVISIONING RUN AT A TIME
# ---------------------------------------------------------------------------
# A real run pulls a container image, starts a vCluster control plane and waits
# for a GPU. Two at once on one laptop means both crawl, both look broken, and
# the demo stalls at the worst moment — and the tenant quota is per project, so
# nothing stops them competing for the same 8 CPUs.
#
# So the second submission is QUEUED, not rejected. Rejecting would push the
# retry onto the PI for a limitation that is ours, and queueing is honest: the
# project appears immediately with a `queued` state and starts by itself.
#
# The cap is deliberately a laptop-scale decision, not a platform one. On real
# hardware this ceiling goes up or disappears; it is here to keep the rehearsal
# truthful about what one machine can do.
MAX_CONCURRENT_RUNS = 1

# (deployment_id, factory) — a factory, not a coroutine. A coroutine created and
# not awaited raises "coroutine was never awaited" if the queue is ever dropped,
# and holding a live coroutine object across an arbitrary wait is a trap.
_QUEUE: list[tuple[str, Any]] = []


def active_runs() -> int:
    return sum(1 for t in _TASKS.values() if not t.done())


def queued_ids() -> list[str]:
    return [d for d, _ in _QUEUE]


def launch_or_queue(factory, deployment_id: str) -> str:
    """Start a run now, or queue it behind the one in flight.

    Returns "running" or "queued" so the API can tell the PI which happened.
    """
    if active_runs() >= MAX_CONCURRENT_RUNS:
        _QUEUE.append((deployment_id, factory))
        db.set_deployment_state(deployment_id, "queued")
        return "queued"
    launch(factory(), deployment_id)
    return "running"


def _start_next() -> None:
    """Pull the next queued run forward. Called when one finishes."""
    while _QUEUE and active_runs() < MAX_CONCURRENT_RUNS:
        deployment_id, factory = _QUEUE.pop(0)
        # It may have been deleted while waiting; a queued project that no longer
        # exists must not be resurrected.
        row = db.get_deployment_full(deployment_id)
        if row is None or row["deletedAt"]:
            continue
        launch(factory(), deployment_id)


def is_running(deployment_id: str) -> bool:
    task = _TASKS.get(deployment_id)
    return task is not None and not task.done()


async def _record(fn, *args, **kwargs) -> None:
    """Run a synchronous registry write without blocking the event loop.

    sqlite3 is synchronous. These writes are sub-millisecond against a local file,
    but they happen inside an async task that is also serving HTTP requests, so
    they go to a thread rather than stalling the loop at every transition.
    """
    await asyncio.to_thread(fn, *args, **kwargs)


async def run_pipeline(
    deployment_id: str,
    template: UseCaseTemplate,
    params: dict[str, Any],
    model: ModelEntry | None,
    executor: Executor,
    plan: list[str],
    resume: bool = False,
    embed_model: ModelEntry | None = None,
) -> None:
    """Execute a provisioning run, recording every transition as it happens.

    Args:
        resume: on a retry, steps already recorded as `succeeded` are skipped
            rather than re-run. That is what makes a retry resume from the failed
            step instead of rebuilding from scratch — and it is safe because each
            step is recorded idempotently.
    """
    ctx = StepContext(
        project=deployment_id,
        template=template,
        params=params,
        model=model,
        embed_model=embed_model,
    )

    # Carry forward anything an earlier attempt produced.
    if resume:
        existing = await asyncio.to_thread(db.get_deployment_full, deployment_id)
        if existing and existing.get("outputs"):
            ctx.outputs.update(existing["outputs"])
        done = {
            s["stepId"] for s in (existing or {}).get("steps", []) if s["state"] == "succeeded"
        }
    else:
        done = set()

    await _record(db.set_deployment_state, deployment_id, "provisioning")

    for index, step_id in enumerate(plan):
        if step_id in done:
            await _record(
                db.append_step_log,
                deployment_id,
                step_id,
                "(already completed in an earlier attempt — skipped)",
            )
            continue

        await _record(db.set_step, deployment_id, step_id, "running")
        await _record(
            db.append_step_log, deployment_id, step_id, f"--- {step_id} started ---"
        )

        # Point `emit` at THIS step's log for the duration of the step. Writes go
        # through the DAO synchronously: they are sub-millisecond against a local
        # file, and a step that is already shelling out to kubectl is not going to
        # notice. Using to_thread here would need an event loop reference the
        # executor does not have.
        ctx.emit = lambda line, _s=step_id: db.append_step_log(deployment_id, _s, line)
        ctx.progress = lambda snapshot, _s=step_id: db.set_step_progress(
            deployment_id, _s, snapshot
        )

        try:
            method = getattr(executor, METHOD_FOR_STEP[step_id])
            result: StepResult = await method(ctx)
        except Exception as exc:  # noqa: BLE001 — any failure is a step failure
            # The message a PI sees. `repr` rather than `str` so an exception with
            # an empty message still identifies its type.
            reason = str(exc) or repr(exc)
            await _record(
                db.append_step_log,
                deployment_id,
                step_id,
                f"FAILED: {reason}",
            )
            await _record(db.set_step, deployment_id, step_id, "failed")

            # Everything after the failure is marked `skipped`, not left `pending`.
            # A failed run whose remaining steps say "pending" reads as still in
            # progress; `skipped` says plainly that they never got their turn.
            for later in plan[index + 1 :]:
                if later not in done:
                    await _record(db.set_step, deployment_id, later, "skipped")

            await _record(db.set_deployment_state, deployment_id, "failed")
            return

        if result.outputs:
            ctx.outputs.update(result.outputs)
        await _record(db.append_step_log, deployment_id, step_id, result.log)
        await _record(db.set_step, deployment_id, step_id, "succeeded")

    await _record(
        db.set_deployment_state,
        deployment_id,
        terminal_success_state(template),
        outputs=ctx.outputs or None,
    )


async def run_teardown(
    deployment_id: str,
    template: UseCaseTemplate,
    params: dict[str, Any],
    executor: Executor,
) -> None:
    """Tear a project down, in the template's declared order.

    Recorded in the SAME table as the provisioning run, under phase='teardown', so
    the two are one continuous history rather than the second erasing the first.
    The step ids are the canonical six — teardown does not get its own vocabulary.
    """
    # No models: teardown removes objects by name and by label and never
    # renders a fragment, so it needs neither the chat entry nor the embed one.
    ctx = StepContext(project=deployment_id, template=template, params=params, model=None)
    order = plan_teardown(template)

    await asyncio.to_thread(db.ensure_steps, deployment_id, order, "teardown")

    for index, step_id in enumerate(order):
        await _record(db.set_step, deployment_id, step_id, "running", phase="teardown")
        await _record(
            db.append_step_log,
            deployment_id,
            step_id,
            f"--- teardown {step_id} started ---",
            phase="teardown",
        )
        ctx.emit = lambda line, _s=step_id: db.append_step_log(
            deployment_id, _s, line, phase="teardown"
        )
        try:
            result = await executor.teardown(ctx, step_id)
        except Exception as exc:  # noqa: BLE001
            await _record(
                db.append_step_log,
                deployment_id,
                step_id,
                f"FAILED: {str(exc) or repr(exc)}",
                phase="teardown",
            )
            await _record(db.set_step, deployment_id, step_id, "failed", phase="teardown")
            for later in order[index + 1 :]:
                await _record(db.set_step, deployment_id, later, "skipped", phase="teardown")
            # The deployment is NOT marked deleted: something is still out there,
            # and recording it as gone would lose the only record that it is not.
            await _record(db.set_deployment_state, deployment_id, "failed")
            return

        await _record(
            db.append_step_log, deployment_id, step_id, result.log, phase="teardown"
        )
        await _record(db.set_step, deployment_id, step_id, "succeeded", phase="teardown")

    await _record(db.set_deployment_state, deployment_id, "deleted")


def launch(coro, deployment_id: str) -> None:
    """Start a pipeline as a background task and keep a reference to it."""
    task = asyncio.create_task(coro)
    _TASKS[deployment_id] = task
    # Without a done-callback the task's exception would be swallowed until
    # interpreter shutdown. The runner catches step failures itself, so anything
    # reaching here is a bug in the runner and should be visible immediately.
    task.add_done_callback(_report_task_result)


def _report_task_result(task: asyncio.Task) -> None:
    # Whatever happened, the slot is free — let the queue move before anything
    # else, so a crash in one run does not strand every run behind it.
    try:
        _start_next()
    except Exception:  # pragma: no cover
        pass
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover — only on a runner bug
        import traceback

        print("!! pipeline task crashed outside step handling:", file=__import__("sys").stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
