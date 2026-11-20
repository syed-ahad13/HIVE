# ===========================================================================
# HIVE STUDIO BFF — THE APP
# ===========================================================================
# The only thing in Studio that talks to the cluster (CLAUDE.md §6). Port 8000.
#
# NO CORS LAYER, DELIBERATELY. The Vite dev server on 5173 proxies /api to
# http://localhost:8000, so the browser is same-origin and there is no CORS to get
# wrong. Adding a permissive CORS middleware "just in case" would be adding a real
# hole to solve a problem that does not exist.
#
# ---------------------------------------------------------------------------
# STARTUP IS FAIL-LOUD
# ---------------------------------------------------------------------------
# The catalogue is loaded and fully validated before the app serves anything. A
# malformed template aborts the process with a message naming the file and field,
# rather than starting a console that is quietly missing a use case. See
# catalog.py for why a half-loaded catalogue is the failure mode to design against.
# ===========================================================================

from __future__ import annotations

import os
import re
import csv
import io
import json
import shutil
import tempfile
import subprocess
import sys
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from . import dataplane, db, live, pipeline, render
# uploads_root is shared with executor_real, which copies OUT of the directory
# main.py writes INTO. One definition, so an override cannot be honoured by one
# end and not the other — see app/paths.py.
from .paths import repo_root as repo_root_path, uploads_root
from .catalog import Catalog, CatalogError, SAMPLE_DATASETS, load_catalog, sample_dataset
from .schema import Health, TemplateSummary
from .validation import validate_params

# ---------------------------------------------------------------------------
# EXECUTOR MODE
# ---------------------------------------------------------------------------
# `mock` is the default ON PURPOSE (CLAUDE.md §6): the dangerous direction is
# provisioning by accident, so the safe mode is the one you get by forgetting to
# set anything, and `real` must be an explicit, deliberate act. Read once at
# startup so the mode cannot change under a running pipeline.
EXECUTOR_MODES = ("mock", "real")

# The only cluster Studio will provision into. Shared with executor_real, which
# checks it again per deployment.
REQUIRED_CONTEXT = "kind-hive-host"


def executor_mode() -> str:
    mode = os.environ.get("STUDIO_EXECUTOR", "mock").strip().lower() or "mock"
    if mode not in EXECUTOR_MODES:
        # Refuse rather than fall back. Falling back to `mock` would be safe but
        # silent, and someone who typed STUDIO_EXECUTOR=Real and got a mock run
        # would reasonably believe they had provisioned something.
        raise SystemExit(
            f"STUDIO_EXECUTOR is {mode!r}; expected one of {', '.join(EXECUTOR_MODES)}"
        )
    return mode


# Populated by the lifespan handler. Module-level rather than app.state so the
# helpers below read plainly; there is exactly one catalogue per process.
_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    if _catalog is None:  # pragma: no cover — only reachable if startup was skipped
        raise RuntimeError("catalogue accessed before startup completed")
    return _catalog


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _catalog

    mode = executor_mode()

    try:
        _catalog = load_catalog()
    except CatalogError as exc:
        # Printed rather than raised bare so the message is readable: it is aimed
        # at whoever just edited a YAML file, and a Python traceback would bury
        # the one line that tells them which file and which field.
        print("\n=== HIVE Studio: CATALOGUE FAILED TO LOAD ===", file=sys.stderr)
        print(f"{exc}\n", file=sys.stderr)
        print(
            "The catalogue is committed data (templates/catalog/). Fix the file "
            "above and restart.\n",
            file=sys.stderr,
        )
        sys.stderr.flush()
        # os._exit, not `raise SystemExit`. Uvicorn catches exceptions raised in
        # lifespan and logs them with a full traceback, which lands BELOW the
        # message above — so the last thing on screen, and the first thing a human
        # reads, becomes a stack trace through contextlib instead of the name of
        # the file they need to fix. Verified: with SystemExit the traceback is
        # eight lines of uvicorn internals.
        #
        # The usual objection to os._exit is that it skips cleanup, and here there
        # is nothing to skip: this runs before init_db(), before any connection is
        # opened, before the app serves a request. stderr is flushed explicitly
        # just above, since os._exit does not.
        os._exit(1)

    if mode == "real":
        # REFUSE TO START AGAINST THE WRONG CLUSTER.
        #
        # The executor checks this too, per deployment. This second check exists
        # because the window between "the operator started Studio" and "the
        # operator clicks Deploy" is exactly when a kubecontext gets switched for
        # some unrelated reason — and by then the console is up, looks healthy,
        # and is one click from provisioning into someone else's cluster.
        #
        # Refusing at startup makes the failure loud, immediate, and attributable
        # to the person who can fix it.
        try:
            ctx = subprocess.run(
                ["kubectl", "config", "current-context"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            ctx = f"<could not be read: {exc}>"
        if ctx != REQUIRED_CONTEXT:
            print("\n=== HIVE Studio: WRONG CLUSTER, REFUSING TO START ===", file=sys.stderr)
            print(
                f"STUDIO_EXECUTOR=real provisions real infrastructure, and the "
                f"current kubectl context is {ctx!r}.\n"
                f"Studio only provisions into {REQUIRED_CONTEXT!r}.\n\n"
                f"  kubectl config use-context {REQUIRED_CONTEXT}\n\n"
                f"Or run without STUDIO_EXECUTOR=real to use the console in mock mode.\n",
                file=sys.stderr,
            )
            sys.stderr.flush()
            os._exit(1)

    db.init_db()

    available = sum(1 for t in _catalog.templates if t.metadata.maturity == "available")
    print(
        f"HIVE Studio BFF ready — executor={mode}, "
        f"{len(_catalog.templates)} templates ({available} available), "
        f"{len(_catalog.models)} models, registry at {db.db_path()}",
        file=sys.stderr,
    )
    if mode == "real":
        # The one mode that touches the cluster. Worth a line in the log that is
        # hard to miss when scrolling back to work out what a run actually did.
        print(
            "  !! STUDIO_EXECUTOR=real — provisioning will act on the host cluster",
            file=sys.stderr,
        )

    if dataplane.enabled():
        await dataplane.data_path.start()
        print("data path: supervising the Kourier port-forward on 18080", file=sys.stderr)

    yield

    await dataplane.data_path.stop()
    # Per-project forwards opened by the playground. Closed here so a restart
    # does not leave orphaned `kubectl port-forward` children holding ports.
    await dataplane.forwards.stop_all()


app = FastAPI(
    title="HIVE Studio BFF",
    version="0.1.0",
    summary="The PI-facing console's backend: catalogue, wizard schema, registry.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------


@app.get("/api/health", response_model=Health, tags=["meta"])
def health() -> Health:
    """Liveness, and which executor the pipeline will use.

    The UI reads `executor` to decide whether to show the amber "nothing is really
    being created" banner. Amber means "this is not how production works"
    (invariant (e)), and a mock run is exactly that.
    """
    return Health(
        ok=True,
        executor=executor_mode(),
        # Whether the playground can reach a model at all. The UI uses this to
        # explain a dead Invoke button instead of letting it fail mysteriously.
        dataPath=dataplane.data_path.status if dataplane.enabled() else None,
    )


@app.get("/api/templates", response_model=list[TemplateSummary], tags=["catalogue"])
def list_templates() -> list[TemplateSummary]:
    """The catalogue, as cards. Available templates first.

    `maturity` is part of the response because the UI must not let a PI start a
    `coming-soon` template (CLAUDE.md §6) — that decision needs the flag, so the
    flag is on the card rather than only in the detail view.
    """
    return get_catalog().summaries()


@app.get("/api/templates/{template_id}", tags=["catalogue"])
def get_template(template_id: str) -> dict[str, Any]:
    """One template in full, with every enum RESOLVED to real choices.

    The `model` parameter declares `source: models.yaml`; this endpoint replaces
    that with the actual `values`, each carrying the model's `displayName`. The
    frontend therefore renders one uniform kind of enum and never needs to know
    that models.yaml exists — the wizard is driven by `parameters` alone, which is
    the point of a schema-driven wizard.
    """
    catalog = get_catalog()
    resolved = catalog.resolved_template(template_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"no such template: {template_id}")

    # THE STEP PLAN, COMPUTED FROM THIS TEMPLATE'S OWN `resources`.
    #
    # Served here so the console never has to derive it. The template page
    # pre-discloses the steps before a PI commits, and the wizard's review screen
    # lists them again — both used to read a hard-coded six-entry array, which
    # was right for the one template that existed and silently wrong for a
    # template with eleven steps in a different order.
    #
    # Deriving it in the frontend instead would mean a second copy of
    # RESOURCE_TO_STEP and STEP_GATES in JavaScript, kept in step by hand. The
    # backend already computes the plan it is going to run; this is that exact
    # list, from that exact function.
    #
    # `coming-soon` templates declare no resources, so they get no plan rather
    # than an empty one that would render as "this creates nothing".
    if resolved["metadata"]["maturity"] == "available":
        typed = catalog.resolved_template_model(template_id)
        resolved["steps"] = pipeline.plan_steps(typed)

    # THE BUNDLED SAMPLES, PREVIEWED THE SAME WAY AN UPLOAD IS.
    #
    # A template with an OPTIONAL upload promises a fallback — "If you leave
    # this empty we use a small sample so you can see how it works". Anything
    # the wizard must ask ABOUT that file (which columns the model reads) is
    # unanswerable unless the console can see the file, and the console cannot:
    # it lives in the repository, beside the template.
    #
    # So the preview is served with the template, keyed by MODEL INTERFACE
    # because that is what selects the sample (catalog.sample_dataset — the same
    # function the executor calls when it copies the file in). Produced by
    # `_dataset_preview`, the same function an upload goes through, so the
    # columns a PI picks from are read the same way whichever file they end up
    # running over.
    #
    # Absent for every template that ships no samples, which is all of them but
    # one. An empty object rather than a missing key would read as "this
    # template has samples and they are broken".
    samples: dict[str, Any] = {}
    for interface in SAMPLE_DATASETS:
        path = sample_dataset(template_id, interface)
        if path is None:
            continue
        preview = _dataset_preview(path)
        if preview is None:  # pragma: no cover — a sample that stopped parsing
            continue
        samples[interface] = {
            "file": path.name,
            "bytes": path.stat().st_size,
            "preview": preview,
        }
    if samples:
        resolved["sampleDatasets"] = samples

    return resolved


@app.get("/api/models", tags=["catalogue"])
def list_models() -> list[dict[str, Any]]:
    """The curated model list — the governance boundary, served whole.

    Returned as-is, including `seedUrl` and `runtime`. This is a local rehearsal
    console for developer-facing PIs and the same file is in git; there is nothing
    secret here, and the playground genuinely needs `interface`, `invokePath` and
    `sampleInput` in order to render itself.
    """
    return [m.model_dump() for m in get_catalog().models]


# ---------------------------------------------------------------------------
# DEPLOYMENTS
# ---------------------------------------------------------------------------


def namespace_exists(project: str) -> bool:
    """Does p-<project> already exist on the cluster?

    THE REAL CHECK, replacing the S1 stub. It runs in BOTH executor modes and
    that is deliberate: a mock deployment that reserves a project name whose
    namespace already exists would hand a PI a name that fails the moment they
    provision for real.

    A cluster that cannot be reached returns False — "no collision known" — and
    the deployment proceeds. Refusing every submission because kubectl is
    unavailable would make the console unusable in mock mode with Docker stopped,
    which is a perfectly normal way to work on the frontend. The two real
    backstops still hold: the registry's primary key refuses a duplicate project,
    and namespace-pack refuses a namespace it does not own.
    """
    try:
        result = subprocess.run(
            ["kubectl", "get", "namespace", f"p-{project}", "-o", "name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def selected_models(catalog: Catalog, params: dict[str, Any]) -> tuple[Any, Any]:
    """The (served, embedding) catalogue entries a submission selected.

    ONE PLACE THAT KNOWS WHICH PARAMETER NAMES CARRY MODELS, so create, retry
    and dry-run cannot drift apart. inference-endpoint calls its served model
    `model`; rag-stack calls it `baseModel` and adds `embeddingModel`. Both are
    looked up unconditionally — a template that declares neither simply gets
    (None, None), which is what every step that needs one already handles.
    """
    served = catalog.model(params.get("model") or params.get("baseModel") or "")
    embed = catalog.model(params.get("embeddingModel") or "")
    return served, embed


def _preview_extras(template: Any, params: dict[str, Any], model: Any) -> dict[str, Any] | None:
    """Values a dry run cannot compute, read from the cluster for the preview.

    A batch Job's serving container is NOT DESCRIBED ANYWHERE IN THIS
    REPOSITORY — it is whatever the curated ClusterServingRuntime says it is,
    which is the whole argument of that template (a batch run uses the same
    image a served endpoint would). So `batch-job` cannot render from parameters
    alone, and the review screen answered "could not render a preview" for every
    batch deployment.

    Resolved with the SAME PURE FUNCTIONS the executor uses
    (render.select_runtime + render.runtime_container) over the same one
    `kubectl get clusterservingruntimes`, so the manifest a PI reviews is built
    from the same objects the deploy will use rather than from a plausible
    stand-in.

    RETURNS None WHEN THE CLUSTER CANNOT BE ASKED, and the render then fails
    with a message naming what was missing. That is the honest outcome: a
    preview of the runtime block assembled from a guess would be a lie about the
    single most load-bearing line in the file, and mock mode with Docker stopped
    is exactly when a guess would go unnoticed.
    """
    if "batch-run" not in set(template.spec.resources or []) or model is None:
        return None
    try:
        probe = subprocess.run(
            ["kubectl", "get", "clusterservingruntimes", "-o", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    try:
        runtimes = json.loads(probe.stdout).get("items", [])
    except (ValueError, AttributeError):
        return None
    try:
        chosen = render.select_runtime(model, runtimes)
        # The PROJECT NAME stands in for the InferenceService name KServe would
        # otherwise have supplied for `{{.Name}}` in the runtime's own args.
        # The executor passes exactly this, so the preview renders the same
        # `--alias` / model name the run will use.
        return {
            "runtime": render.runtime_container(
                chosen, str(params.get("projectName", "")).strip()
            )
        }
    except render.RenderError:
        # RuntimeUnavailable included. Let render_all raise it with the message
        # already written for a human, rather than swallowing it here.
        return None


class DeployRequest(BaseModel):
    """A wizard submission.

    `params` is deliberately untyped here — a dict, not a model. The whole point
    of a schema-driven wizard is that the catalogue defines the questions, so the
    backend cannot know them at import time. Validation happens against the
    template, in validate_params, which is where the rules actually live.
    """

    model_config = ConfigDict(extra="forbid")

    templateId: str
    params: dict[str, Any]


def _days_left(created_at: str, params: dict[str, Any]) -> int | None:
    """Days remaining, derived from the PI's own answer.

    THIS IS NOT CLUSTER STATE, and the distinction matters for invariant (b).
    Studio is not reading an expiry from a namespace annotation and caching it —
    it is doing arithmetic on two things it legitimately owns: when the request
    was made, and how many days were asked for. The authoritative clock is
    `hive.ksl/expiry` on the namespace, and the host reconciler is what honours it.

    Once the real executor writes that annotation (S5), this should be replaced by
    a live read, not supplemented by one — two countdowns that can disagree is
    worse than one that is occasionally coarse.
    """
    duration = params.get("durationDays")
    if not isinstance(duration, int) or isinstance(duration, bool):
        return None
    try:
        started = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover
        return None
    elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 86400
    return max(0, round(duration - elapsed))


@app.post("/api/deployments", status_code=202, tags=["deployments"])
async def create_deployment(
    req: DeployRequest,
    response: Response,
    dry_run: bool = Query(False, alias="dry_run"),
) -> dict[str, Any]:
    """Accept a submission and start provisioning in the background.

    Answers **202 Accepted**, not 201 Created: nothing exists yet. The pipeline
    takes roughly twenty seconds and the caller is expected to watch
    GET /api/deployments/{id}. Answering 201 would claim a project that does not
    exist for another twenty seconds.
    """
    catalog = get_catalog()

    # The RESOLVED template — validate_params must see real enum options, or the
    # `model` parameter (which is `source: models.yaml` in the file) would reject
    # every value and enforce the closed list as empty.
    template = catalog.resolved_template_model(req.templateId)
    if template is None:
        raise HTTPException(status_code=404, detail=f"no such template: {req.templateId}")

    if template.metadata.maturity != "available":
        raise HTTPException(
            status_code=409,
            detail=(
                f"template '{req.templateId}' is {template.metadata.maturity} and "
                f"cannot be deployed"
            ),
        )

    errors = validate_params(template, req.params, namespace_exists=namespace_exists)

    # The duplicate check lives here rather than in validate_params because it is a
    # question about the REGISTRY, not about the submission — the same parameters
    # would be perfectly valid tomorrow after the first project is torn down.
    project = str(req.params.get("projectName", "")).strip()
    # ---------------------------------------------------------------------
    # A NAME IS TAKEN ONLY WHILE THE PROJECT IS ALIVE
    # ---------------------------------------------------------------------
    # This used to refuse any name the registry had ever seen, because a delete
    # is SOFT — the row stays so history survives — and this check did not look
    # at `deletedAt`. Redeploying `my-rag` after removing `my-rag` was refused
    # with "already exists", which reads as "your deletion did not work" and
    # sends somebody to inspect a cluster that is completely clean.
    #
    # Deploy / tear down / redeploy under the same name is the normal loop in a
    # rehearsal console, so a removed project now steps aside: its row is
    # archived under `<id>@<deleted_at>`, keeping every step and log, and the
    # name is free again.
    existing = db.get_deployment_full(project) if project else None
    if existing is not None and not existing["deletedAt"]:
        errors.append(
            {
                "field": "projectName",
                "message": f"A project called '{project}' already exists. "
                f"Choose a different name.",
            }
        )

    # --- does the staged upload match what this question takes? -----------
    # THE HALF THAT IS A CONTROL. The drop zone filters the file picker and
    # checks what was dropped, but both are client-side and neither survives a
    # curl. The endpoint that received the file could not check this — a PI
    # uploads before the deploy request that names the template exists, so it
    # accepts the union of every template's types.
    #
    # Here is the first moment both facts are known: which template, and which
    # upload. So here is where a .md handed to a batch run, or a .csv handed to
    # a RAG corpus, is refused — beside the field, in the wizard, rather than
    # four steps later as a driver that cannot parse its input.
    for param in template.spec.parameters:
        if param.type != "upload":
            continue
        upload_id = str(req.params.get(param.name) or "").strip()
        if not upload_id:
            continue  # empty is allowed; the pipeline falls back to a sample
        try:
            manifest = _upload_manifest(upload_id)
        except (OSError, FileNotFoundError):
            errors.append({
                "field": param.name,
                "message": f"Those files are no longer staged. Add them again.",
            })
            continue

        accept = [e.lower() for e in (param.accept or [])]
        names = [f["name"] for f in manifest["files"]]
        wrong = [n for n in names if accept and not n.lower().endswith(tuple(accept))]
        if wrong:
            errors.append({
                "field": param.name,
                "message": (
                    f"Only {' or '.join(accept)} files can be used here — "
                    f"{', '.join(wrong[:3])} "
                    f"{'is' if len(wrong) == 1 else 'are'} not."
                ),
            })
        if param.maxFiles is not None and len(names) > param.maxFiles:
            errors.append({
                "field": param.name,
                "message": (
                    f"This takes "
                    f"{'one file' if param.maxFiles == 1 else f'at most {param.maxFiles} files'}, "
                    f"and {len(names)} were uploaded."
                ),
            })
        if param.maxBytes is not None and manifest["totalBytes"] > param.maxBytes:
            errors.append({
                "field": param.name,
                "message": (
                    f"At most {_human_bytes(param.maxBytes)} can be used here, "
                    f"and those come to {_human_bytes(manifest['totalBytes'])}."
                ),
            })

    if errors:
        # 400 with the whole list, so the wizard can put each message beside its
        # field rather than showing one error at a time.
        raise HTTPException(status_code=400, detail={"errors": errors})

    plan = pipeline.plan_steps(template)
    model, embed_model = selected_models(catalog, req.params)

    # --- DRY RUN: render and return, create nothing ------------------------
    # The review step of the wizard shows a PI the exact manifests their answers
    # produce, BEFORE they commit to anything. It is the same render_all() the
    # real executor will call in S5, so the preview cannot drift from what
    # actually gets applied — a preview produced by a second code path would be a
    # plausible-looking lie.
    #
    # Nothing is written. Not the registry row, not tenants/<project>/rendered/.
    # A preview that left files behind for a project nobody deployed would put
    # junk in the repository under the name of invariant (a).
    if dry_run:
        try:
            files = render.render_all(
                template,
                req.params,
                model,
                embed_model=embed_model,
                extras=_preview_extras(template, req.params, model),
            )
        except render.RenderError as exc:
            # A render failure here is a fault in the catalogue's fragments, not
            # in the PI's answers — 422, and the message names the fragment.
            raise HTTPException(
                status_code=422, detail=f"could not render this template: {exc}"
            ) from exc
        response.status_code = 200
        return {"dryRun": True, "id": project, "steps": plan, "files": files}

    # The name belonged to a project that has been removed. Move its history
    # aside now — after validation has passed and immediately before the row is
    # written, so a submission that gets rejected never disturbs the registry.
    if existing is not None and existing["deletedAt"]:
        archived = db.archive_deleted(project)
        print(
            f"registry: '{project}' was removed on {existing['deletedAt']}; "
            f"its history is kept as '{archived}' and the name is free again",
            file=sys.stderr,
        )

    db.create_deployment(
        deployment_id=project,
        template_id=req.templateId,
        params=req.params,
        steps=plan,
        state="requested",
    )

    disposition = pipeline.launch_or_queue(
        lambda: pipeline.run_pipeline(
            deployment_id=project,
            template=template,
            params=req.params,
            model=model,
            embed_model=embed_model,
            executor=pipeline.build_executor(executor_mode()),
            plan=plan,
        ),
        project,
    )

    response.headers["Location"] = f"/api/deployments/{project}"
    return {
        "id": project,
        "state": "queued" if disposition == "queued" else "requested",
        "queued": disposition == "queued",
        "aheadInQueue": max(0, len(pipeline.queued_ids()) - 1) if disposition == "queued" else 0,
        "steps": plan,
    }


@app.get("/api/deployments", tags=["deployments"])
def list_deployments(
    all: bool = Query(False, alias="all", description="include deleted projects"),
) -> list[dict[str, Any]]:
    """Projects, newest first. Deleted ones hidden unless `?all=1`."""
    rows = db.list_deployments(include_deleted=all)
    for row in rows:
        row["daysLeft"] = _days_left(row["createdAt"], row.get("params", {}))
    return rows


@app.get("/api/deployments/{deployment_id}", tags=["deployments"])
async def get_deployment(deployment_id: str) -> dict[str, Any]:
    """One project: what was asked for, and how the pipeline went.

    HISTORY ONLY. There is no readiness, replica count or live URL in this
    response, because the registry does not hold any (invariant (b)). When the
    project page needs those it reads them from the cluster; `state: "ready"`
    here means the RUN completed, not that anything is serving right now.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    row["running"] = pipeline.is_running(deployment_id)
    availability = row.get("params", {}).get("availability", "always-on")

    if executor_mode() != "real":
        # Mock mode has no cluster to ask. The countdown falls back to arithmetic
        # on the PI's own answer, and the derived status comes from the run alone.
        row["daysLeft"] = _days_left(row["createdAt"], row.get("params", {}))
        row["live"] = None
        row["derivedStatus"] = (
            "deleted" if row["deletedAt"]
            else "provisioning" if row["state"] in ("requested", "provisioning", "queued")
            else "failed" if row["state"] == "failed"
            else "ready" if row["state"] == "ready"
            else row["state"]
        )
        return row

    # --- REAL MODE: everything below is read from the cluster, every request ---
    # Invariant (b). Nothing here comes from the registry, and nothing here is
    # cached: a project that scaled to zero, expired, or was deleted behind
    # Studio's back must say so the moment someone looks at this page.
    facts = await live.isvc_facts(deployment_id)
    pods = await live.predictor_pods(deployment_id, facts["syncedName"])
    quota = await live.quota(deployment_id)
    lifetime = await live.expiry(deployment_id)

    row["live"] = {
        "ready": facts["ready"],
        "endpointUrl": facts["url"],
        "syncedName": facts["syncedName"],
        "predictorPods": pods,
        "quota": quota,
        "expiry": lifetime,
    }

    # --- RAG projects have three more things, all read live ---------------
    # The chat model's status above is UNCHANGED and still applies: a RAG
    # project's InferenceService sleeps and wakes exactly like any other, and
    # `predictorPods` is still what makes `sleeping` visible. What is added is
    # the machinery that does not scale to zero.
    if row["templateId"] == "rag-stack":
        # Derived from the project name, never read back from the registry —
        # the same pure function the ingestion Job and the RAG service were
        # rendered from, so all three agree by construction.
        collection = render.collection_name(deployment_id)
        rag = await live.rag_facts(deployment_id, collection)
        row["live"]["rag"] = {
            **rag,
            # WHAT THIS RUN PRODUCED, beside WHAT IS THERE NOW. From the
            # pipeline's outputs, which are history — `livePoints` above is the
            # truth. Shown together on purpose: a disagreement between them is
            # information (the index was rebuilt, or dropped), not something to
            # hide behind one number.
            "indexedChunks": (row.get("outputs") or {}).get("indexedChunks"),
            "indexedFiles": (row.get("outputs") or {}).get("indexedFiles"),
            "ragEndpointUrl": (row.get("outputs") or {}).get("ragEndpointUrl"),
            "chatEndpointUrl": facts["url"],
        }

    # --- RUN-SHAPED DEPLOYMENTS ------------------------------------------
    # A batch run has no endpoint, no replicas and nothing that sleeps, so most
    # of the block above is meaningless for it — `isvc_facts` finds nothing and
    # correctly reports nothing. What it DOES have is a Job, a progress
    # snapshot, artifacts, and a quota line that should read 0 GPUs once the
    # work is done.
    #
    # THE QUOTA IS THE INTERESTING ONE and it is already read above for every
    # project: showing it on a finished run is what makes "the GPU went back to
    # the shared pool" visible rather than asserted.
    template = get_catalog().template(row["templateId"])
    lifecycle = template.spec.lifecycle if template else None
    if lifecycle and lifecycle.is_run:
        job = await live.job_facts(deployment_id, f"{deployment_id}-batch")
        outputs = row.get("outputs") or {}
        # The progress snapshot lives on the step, which is where the poller
        # wrote it; lifted here so the project page does not have to hunt
        # through the step list for the one step that has it.
        batch_step = next(
            (s for s in row["steps"]
             if s["stepId"] == "batch-run" and s["phase"] == "provision"),
            None,
        )
        row["live"]["run"] = {
            "job": job,
            "progress": (batch_step or {}).get("progress"),
            "rowsProcessed": outputs.get("rowsProcessed"),
            "rowsFailed": outputs.get("rowsFailed"),
            "durationSeconds": outputs.get("durationSeconds"),
            "rowsPerSecond": outputs.get("rowsPerSecond"),
            "runtimeUsed": outputs.get("runtimeUsed"),
            # Read from DISK, not from the outputs, so a file removed by hand is
            # not still offered for download. Same invariant-(b) reasoning as
            # everything else on this page: the artifacts a PI can actually get
            # are whichever ones are there now.
            "artifacts": _artifact_listing(deployment_id),
            "resultsAvailableUntil": outputs.get("resultsAvailableUntil"),
        }

    row["derivedStatus"] = live.derive_status(
        run_state=row["state"],
        deleted=bool(row["deletedAt"]),
        ready=facts["ready"],
        pods=pods,
        availability=availability,
        project=deployment_id,
    )
    # THE NAMESPACE ANNOTATION IS AUTHORITATIVE, not the arithmetic S2 used. Two
    # countdowns that can disagree is worse than one, so the derived value is only
    # a fallback for a namespace that has gone.
    row["daysLeft"] = (
        lifetime["daysLeft"] if lifetime
        else _days_left(row["createdAt"], row.get("params", {}))
    )
    return row


@app.post("/api/deployments/{deployment_id}/retry", status_code=202, tags=["deployments"])
async def retry_deployment(deployment_id: str) -> dict[str, Any]:
    """Resume a failed run from the step that failed.

    Only a `failed` deployment can be retried. Anything else is a 409: retrying a
    run that is still going would put two pipelines on the same project, and
    retrying one that succeeded would re-run steps that already did their work.

    The failed step is reset to `pending` and everything before it is left alone,
    so a retry picks up where it stopped rather than rebuilding from scratch.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    if row["state"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"only a failed deployment can be retried; this one is '{row['state']}'",
        )
    if pipeline.is_running(deployment_id):  # pragma: no cover — belt and braces
        raise HTTPException(status_code=409, detail="a run is already in progress")

    catalog = get_catalog()
    template = catalog.resolved_template_model(row["templateId"])
    if template is None:  # pragma: no cover — template removed since the run
        raise HTTPException(
            status_code=409,
            detail=f"template '{row['templateId']}' no longer exists in the catalogue",
        )

    provision_steps = [s for s in row["steps"] if s["phase"] == "provision"]
    failed = [s["stepId"] for s in provision_steps if s["state"] == "failed"]
    skipped = [s["stepId"] for s in provision_steps if s["state"] in ("skipped", "pending")]

    # Clear the failure and everything that never got its turn, so the resumed run
    # starts from a clean slate at exactly the point it stopped.
    for step_id in failed + skipped:
        db.reset_step(deployment_id, step_id)

    plan = pipeline.plan_steps(template)
    model, embed_model = selected_models(catalog, row["params"])
    pipeline.launch(
        pipeline.run_pipeline(
            deployment_id=deployment_id,
            template=template,
            params=row["params"],
            model=model,
            embed_model=embed_model,
            executor=pipeline.build_executor(executor_mode()),
            plan=plan,
            resume=True,
        ),
        deployment_id,
    )
    return {"id": deployment_id, "resumedFrom": failed[0] if failed else plan[0]}


@app.delete("/api/deployments/{deployment_id}", status_code=202, tags=["deployments"])
async def delete_deployment(deployment_id: str) -> dict[str, Any]:
    """Tear a project down, in the template's declared teardown order.

    A SOFT DELETE. The row stays and `deleted_at` is stamped once teardown
    finishes; the list hides it unless `?all=1`. History does not disappear
    because the thing it describes did — "what did we run last term, and what
    happened to it?" is a question the registry should still answer.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    if row["deletedAt"]:
        raise HTTPException(status_code=409, detail="this project is already deleted")
    if pipeline.is_running(deployment_id):
        raise HTTPException(
            status_code=409,
            detail="a run is in progress; wait for it to finish before deleting",
        )

    template = get_catalog().resolved_template_model(row["templateId"])
    if template is None:  # pragma: no cover
        raise HTTPException(
            status_code=409,
            detail=f"template '{row['templateId']}' no longer exists in the catalogue",
        )

    # OWNERSHIP CHECK BEFORE ANY TEARDOWN STARTS. Invariant (d).
    #
    # The executor checks this again at every destructive command, and
    # validation.py refuses the reserved names at creation. This is the third
    # guard and the cheapest one: it answers "is p-<id> actually a namespace
    # Studio created?" before a pipeline is launched at all, so a mismatch is a
    # clean 409 rather than a teardown that gets three steps in and stops.
    #
    # It also covers the case none of the others do: a registry row whose
    # namespace has since been replaced by something else with the same name.
    # ---------------------------------------------------------------------
    # A MOCK STUDIO MUST NOT "DELETE" A PROJECT THAT EXISTS FOR REAL
    # ---------------------------------------------------------------------
    # The mock teardown only logs "[mock] would remove …" — and then the project
    # is recorded as deleted. For a project that was provisioned by the REAL
    # executor that is a lie with a cost: on 2026-09-15 `abc` (a full RAG stack,
    # vCluster included) was removed from the console by a mock-mode backend and
    # kept running on the cluster for three weeks, holding a GPU and ~1.1GiB of
    # the VM, invisible to everyone and undeletable from the UI.
    #
    # The CLUSTER decides, not the registry: if p-<id> exists, something real is
    # there, and only a real executor may remove it.
    if executor_mode() == "mock" and namespace_exists(deployment_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"p-{deployment_id} exists on the cluster, so this project was "
                f"created for real. Studio is running in mock mode, which cannot "
                f"remove anything — deleting it here would hide it from the console "
                f"and leave it all running. Restart Studio in real mode "
                f"(scripts/studio-down.sh, then STUDIO_EXECUTOR=real "
                f"scripts/studio-up.sh) and delete it there."
            ),
        )

    if executor_mode() == "real":
        # Imported here, not at module scope: pipeline.build_executor loads this
        # module lazily so a mock-mode process never pulls in the code that shells
        # out to kubectl, and an eager import here would undo that.
        from .executor_real import PROTECTED_NAMESPACES, STUDIO_TEMPLATE_LABELS

        ns = f"p-{deployment_id}"
        if ns in PROTECTED_NAMESPACES:
            raise HTTPException(
                status_code=403,
                detail=f"{ns} is a protected namespace and Studio will not touch it",
            )
        # --ignore-not-found SEPARATES THE TWO FAILURES this probe used to merge:
        # a namespace that is gone (exit 0, empty) and a cluster that did not
        # answer (non-zero). The second used to fall through to a teardown that
        # then recorded "deleted" without having reached anything.
        try:
            probe = subprocess.run(
                ["kubectl", "get", "namespace", ns, "--ignore-not-found", "-o",
                 "jsonpath={.metadata.name}|{.metadata.labels.hive\\.ksl/template}"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            probe = None
            probe_error = str(exc)
        else:
            probe_error = (probe.stderr or probe.stdout).strip() if probe.returncode else ""
        if probe is None or probe.returncode != 0:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"could not reach the cluster to check {ns}, so nothing was "
                    f"deleted. Try again once it answers. ({probe_error[:200]})"
                ),
            )
        if probe.stdout.strip():
            owner = probe.stdout.split("|", 1)[1].strip()
            # MEMBERSHIP, NOT EQUALITY. There are now two templates that
            # provision — studio-inference and studio-rag — and this is the
            # guard that decides whether Studio may delete a namespace at all
            # (invariant (d)). Too narrow and a PI cannot remove their own RAG
            # project; too loose and it stops being a whitelist. The set is
            # defined once, in executor_real, and both namespace-pack fragments
            # write one of its literals.
            if owner not in STUDIO_TEMPLATE_LABELS:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"{ns} exists but is not labelled hive.ksl/template with one "
                        f"of {', '.join(sorted(STUDIO_TEMPLATE_LABELS))} "
                        f"(found {owner or 'no label'}). Studio only removes what it created."
                    ),
                )

    order = pipeline.plan_teardown(template)
    pipeline.launch(
        pipeline.run_teardown(
            deployment_id=deployment_id,
            template=template,
            params=row["params"],
            executor=pipeline.build_executor(executor_mode()),
        ),
        deployment_id,
    )
    return {"id": deployment_id, "teardownOrder": order}

# ---------------------------------------------------------------------------
# LIVE STATUS, PLAYGROUND AND LIFECYCLE SURFACES
# ---------------------------------------------------------------------------


class InvokeRequest(BaseModel):
    """A playground call, in one of three shapes.

    Which fields are meaningful depends on the deployment's TEMPLATE, not on the
    request: `messages` for a chat model, `input` for a KServe predict model,
    and `question`/`topK` for a RAG project. All are optional and the handler
    branches on the template, so a client that sends the wrong shape gets a
    clear 400 from the branch rather than a schema error that does not say why.
    """

    model_config = ConfigDict(extra="forbid")

    # inference-endpoint
    messages: list[dict[str, Any]] | None = None
    input: Any = None
    # rag-stack
    question: str | None = None
    topK: int | None = None


@app.get("/api/deployments/{deployment_id}/kubeconfig", tags=["deployments"])
def download_kubeconfig(deployment_id: str):
    """The PI's admin credentials for their OWN vCluster.

    "You get a real cluster" made concrete — this is the file that proves it.
    Served from tenants/<project>/rendered/, written by the executor and removed
    at teardown, because a credential for a control plane that no longer exists is
    only a liability.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")

    path = (
        Path(__file__).resolve().parents[3]
        / "tenants" / deployment_id / "rendered" / "kubeconfig"
    )
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                "no kubeconfig for this project. It is written when the project is "
                "provisioned for real, and removed when the project is torn down."
            ),
        )
    return FileResponse(
        path,
        media_type="application/yaml",
        filename=f"{deployment_id}-kubeconfig.yaml",
    )


# ---------------------------------------------------------------------------
# ARTIFACTS — the thing a batch run actually produces
# ---------------------------------------------------------------------------


def artifacts_dir(deployment_id: str) -> Path:
    """Where collect-results cached this project's output files."""
    return repo_root_path() / "tenants" / deployment_id / "artifacts"


def _artifact_listing(deployment_id: str) -> list[dict[str, Any]]:
    """What is on disk right now. Never what the pipeline said it wrote."""
    directory = artifacts_dir(deployment_id)
    if not directory.is_dir():
        return []
    return sorted(
        (
            {"name": f.name, "bytes": f.stat().st_size}
            for f in directory.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ),
        key=lambda f: f["name"],
    )


@app.get("/api/deployments/{deployment_id}/artifacts", tags=["deployments"])
def list_artifacts(deployment_id: str) -> dict[str, Any]:
    """The files this run produced, with sizes."""
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    outputs = row.get("outputs") or {}
    return {
        "id": deployment_id,
        "artifacts": _artifact_listing(deployment_id),
        "resultsAvailableUntil": outputs.get("resultsAvailableUntil"),
    }


def _first_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    """The first `limit` records of a JSONL file, reading no further than that.

    BOUNDED ON PURPOSE. A million-row run writes a results file of hundreds of
    megabytes; the project page wants fifty rows of it. Reading line by line and
    stopping means the cost is the preview, never the file. A line that does not
    parse is skipped rather than fatal — a half-written last line from a run that
    was killed must not blank the whole preview.
    """
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if len(rows) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                rows.append(record)
    return rows


@app.get("/api/deployments/{deployment_id}/results", tags=["deployments"])
def results_preview(
    deployment_id: str,
    limit: int = Query(50, ge=1, le=500, description="rows of results to return"),
) -> dict[str, Any]:
    """What a batch run PRODUCED: the first rows of its predictions, its failures,
    and its own summary — so the project page can show the answers, not only
    offer a file.

    READ FROM DISK, like the artifact listing: these are the files collect-results
    actually fetched, and a file removed by hand is not reported as still there.
    The totals come from run-summary.json — the driver's own count — rather than
    from counting lines here, which would mean reading the whole file.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")

    directory = artifacts_dir(deployment_id)
    summary = None
    summary_path = directory / "run-summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except ValueError:
            summary = None

    return {
        "id": deployment_id,
        "rows": _first_jsonl(directory / "results.jsonl", limit),
        "errors": _first_jsonl(directory / "errors.jsonl", 10),
        "summary": summary,
        "artifacts": _artifact_listing(deployment_id),
        "resultsAvailableUntil": (row.get("outputs") or {}).get("resultsAvailableUntil"),
    }


@app.get("/api/deployments/{deployment_id}/artifacts/{name}", tags=["deployments"])
def download_artifact(deployment_id: str, name: str):
    """Stream one artifact to the browser.

    THE NAME IS RESOLVED AND CHECKED, not joined and trusted. It arrives in a URL
    path, and `../../studio.db` is a perfectly valid thing for a client to send.
    The resolved path must sit inside this project's own artifacts directory or
    it is a 404 — not a 403, because saying "that exists but you may not have it"
    is more than the caller needs to know.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")

    directory = artifacts_dir(deployment_id)
    candidate = (directory / name).resolve()
    if not str(candidate).startswith(str(directory.resolve())) or not candidate.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"no artifact called {name!r} for this run. Artifacts are cached "
                f"when the run finishes, and they go when the project does."
            ),
        )

    # `application/octet-stream` + a filename, so every browser downloads rather
    # than trying to render a 60MB JSONL file in a tab.
    return FileResponse(
        candidate,
        media_type="application/octet-stream",
        filename=f"{deployment_id}-{candidate.name}",
    )


@app.get("/api/deployments/{deployment_id}/events", tags=["deployments"])
async def deployment_events(deployment_id: str) -> dict[str, Any]:
    """Pipeline history and cluster events, merged, newest first.

    Two sources because they answer different halves of "what happened?": the step
    logs say what Studio did, the cluster events say what Kubernetes did about it.
    A PI debugging a stuck project needs both, and correlating them by hand across
    two screens is the thing this page removes.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")

    entries: list[dict[str, Any]] = []
    for step in row["steps"]:
        if not step["log"]:
            continue
        for line in step["log"].splitlines():
            if line.strip():
                entries.append(
                    {
                        "source": "pipeline",
                        "at": step["finishedAt"] or step["startedAt"],
                        "step": step["stepId"],
                        "phase": step["phase"],
                        "text": line,
                    }
                )

    if executor_mode() == "real":
        for line in await live.events(deployment_id):
            entries.append({"source": "cluster", "at": None, "step": None, "text": line})

    # Pipeline entries newest-first; cluster events keep kubectl's own ordering
    # (oldest first from --sort-by), reversed here to match.
    return {"entries": list(reversed(entries))}


# ---------------------------------------------------------------------------
# CORPUS UPLOAD
# ---------------------------------------------------------------------------
# The wizard uploads documents BEFORE submitting the deploy request, and submits
# the id it gets back as `corpusUploadId`. That indirection is not ceremony: a
# deploy request is JSON that is validated, logged, stored in the registry as
# the record of intent, and shown back on the review screen. A corpus embedded
# in it would be megabytes of base64 in all four places.
#
# Files land in tenants/_uploads/<id>/ — under tenants/ because that is where
# per-project material lives, and under a leading-underscore directory because
# it is NOT a project: `_uploads` cannot collide with a project name, since
# projectName is constrained to ^[a-z0-9-]{3,20}$ and cannot begin with an
# underscore.

# What may be indexed. Matches chunker.TEXT_EXTENSIONS in the RAG toolkit and
# the wizard's own help text, and all three say the same two things.
# WHAT MAY BE UPLOADED, and it is now two sets rather than one, because the two
# templates that take a file want completely different things:
#
#   .md / .txt      a RAG corpus — prose, chunked and embedded
#   .csv / .jsonl   a batch dataset — records, streamed through a model
#
# Kept as one union at the endpoint because the endpoint does not know which
# template the file is destined for: a PI uploads before they have finished the
# wizard, and the deploy request that names the template comes later. The
# TEMPLATE's own `inputFormat` parameter is what rejects a corpus handed to a
# batch run, at validation time, where the mismatch is actually knowable.
CORPUS_EXTENSIONS = (".md", ".txt")
DATASET_EXTENSIONS = (".csv", ".jsonl")
UPLOAD_EXTENSIONS = CORPUS_EXTENSIONS + DATASET_EXTENSIONS

# ---------------------------------------------------------------------------
# THE CEILINGS, AND WHY THEY ARE SMALL
# ---------------------------------------------------------------------------
# A corpus goes onto a 1Gi `scratch` PVC and every chunk of it is embedded on
# CPU inside the ingestion Job. An unbounded upload is not a security problem
# here so much as a way to make a project fail SLOWLY: a PI would watch a
# progress bar for twenty minutes and then get an OOM-killed Job.
#
# 20 files and 5MB total is a demonstration corpus, which is what this template
# is for. It is roughly 800,000 words of prose — far more than anyone reads in a
# walkthrough, and small enough that ingestion finishes while somebody is still
# watching.
MAX_UPLOAD_FILES = 20
# 200MB, RAISED FROM 5MB FOR DATASETS.
#
# A RAG corpus is prose somebody wrote and 5MB of it is a lot. A batch dataset is
# machine-generated records and 5MB is nothing — the bundled iris sample alone is
# 5,000 rows, and a real run is hundreds of thousands.
#
# The ceiling is still a ceiling, and it is sized against what the rest of the
# system can survive rather than against what a disk can hold: `scratch` is 5Gi,
# a `kubectl cp` of this size takes a minute or two, and the driver streams it a
# line at a time so the container never holds it.
MAX_UPLOAD_TOTAL_BYTES = 200 * 1024 * 1024

# Names are client-supplied. Only the basename is ever used and it must match
# this, so a name can never be a path, a hidden file, or anything a shell would
# treat specially.
SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def _human_bytes(n: int) -> str:
    """Sizes a person can compare. Used in messages, not in logic."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _sanitize(filename: str | None) -> str | None:
    """The name this file will be stored under, or None if it cannot be one.

    THE CLIENT'S NAME IS NOT TRUSTED. A multipart filename may contain a path —
    `../../../../etc/passwd.md` is a valid thing for a client to send — so only
    the basename survives, and it must then match SAFE_FILENAME.

    Names are otherwise PRESERVED, not rewritten, because the filename is what a
    PI sees as the citation on every answer the RAG service gives back
    (`sources[].source`). A corpus whose files were renamed to `file-1.md` would
    produce citations nobody could look up.
    """
    if not filename:
        return None
    name = os.path.basename(filename).strip()
    if not name or not SAFE_FILENAME.fullmatch(name):
        return None
    return name


# How many bytes to read when detecting a dataset's shape. Enough for a header
# plus several records of any realistic width, and bounded so that previewing a
# 200MB file costs the same as previewing a small one.
PREVIEW_BYTES = 64 * 1024
PREVIEW_ROWS = 3


def _dataset_preview(path: Path) -> dict[str, Any] | None:
    """What is in this file, enough for the wizard to stop asking free text.

    Returns the detected format, the column names, a row-count estimate and the
    first few rows — or None for a file that is not a dataset at all, which is
    the normal case for a RAG corpus and is not an error.

    ---------------------------------------------------------------------------
    WHY THE ENDPOINT DOES THIS AT ALL
    ---------------------------------------------------------------------------
    `inputField` decides which column a model reads. As free text it is the
    single most likely thing in the whole template to be wrong, and being wrong
    is expensive: without the driver's fail-fast check it would be a run that
    processes every row into an error. Detecting the columns here turns the
    question into a select, and a select cannot be misspelled.

    ---------------------------------------------------------------------------
    THE ROW COUNT IS AN ESTIMATE AND SAYS SO
    ---------------------------------------------------------------------------
    Counting exactly means reading 200MB to answer a question the PI is only
    using to sanity-check their own file. So it is extrapolated from the average
    line length in the sample, and the field is named `rowsEstimated` rather
    than `rows` so nothing downstream can mistake it for a fact. The driver
    counts properly, later, when the number has to be right.
    """
    suffix = path.suffix.lower()
    if suffix not in DATASET_EXTENSIONS:
        return None

    size = path.stat().st_size
    with open(path, "rb") as handle:
        head = handle.read(PREVIEW_BYTES)
    text = head.decode("utf-8", errors="replace")
    # Drop a trailing partial line: at a 64KB boundary the last line is almost
    # certainly cut in half, and half a CSV row parses as a real one.
    lines = text.splitlines()
    if len(text) >= PREVIEW_BYTES and lines:
        lines = lines[:-1]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return None

    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    detected = "jsonl" if suffix == ".jsonl" else "csv"

    if detected == "jsonl":
        for line in lines[: PREVIEW_ROWS + 1]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                if not columns:
                    columns = list(record.keys())
                rows.append(record)
        rows = rows[:PREVIEW_ROWS]
        data_lines = len(lines)
    else:
        reader = csv.DictReader(io.StringIO("\n".join(lines)))
        columns = list(reader.fieldnames or [])
        for index, record in enumerate(reader):
            if index >= PREVIEW_ROWS:
                break
            rows.append(dict(record))
        # The header is not a row.
        data_lines = max(0, len(lines) - 1)

    if not columns:
        return None

    sampled_bytes = len(head)
    if sampled_bytes >= size or data_lines == 0:
        estimate = data_lines
    else:
        per_line = sampled_bytes / max(1, len(lines))
        estimate = int(size / per_line) - (1 if detected == "csv" else 0)

    return {
        "format": detected,
        "columns": columns,
        "rowsEstimated": max(0, estimate),
        "rowsExact": sampled_bytes >= size,
        "sampleRows": rows,
    }


def _upload_manifest(upload_id: str) -> dict[str, Any]:
    """What is on disk under one upload id.

    READ FROM THE FILESYSTEM, never from a record of what was written. The
    directory is the truth: it is what the pipeline's `corpus-upload` step will
    actually copy, and a manifest that disagreed with it would be a promise the
    deployment then broke. Same reasoning as invariant (b), one layer out.
    """
    directory = uploads_root() / upload_id
    files = sorted(
        (
            {"name": f.name, "bytes": f.stat().st_size}
            for f in directory.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ),
        key=lambda f: f["name"],
    )
    manifest: dict[str, Any] = {
        "uploadId": upload_id,
        "files": files,
        "totalBytes": sum(f["bytes"] for f in files),
    }
    # THE PREVIEW IS OF THE FIRST FILE ONLY, and that is not a limitation being
    # papered over: a batch run processes ONE dataset. A multi-file upload is a
    # RAG corpus, and a corpus has no columns to preview — so `preview` is null
    # there, which is exactly the signal the wizard uses to decide whether to
    # show a column picker.
    if files:
        first = directory / files[0]["name"]
        if first.is_file():
            manifest["preview"] = _dataset_preview(first)
    return manifest


class UploadRejected(HTTPException):
    """A 400 whose message names the file and the rule it broke.

    A bare "invalid upload" makes a PI try the same thing again. Every rejection
    in this endpoint says which file and which limit, so the next attempt is an
    informed one.
    """

    def __init__(self, message: str, files: list[str] | None = None):
        super().__init__(
            status_code=400,
            detail={"message": message, **({"files": files} if files else {})},
        )


@app.post("/api/uploads", status_code=201, tags=["uploads"])
async def create_upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    """Stage a corpus and return the id the wizard submits as `corpusUploadId`.

    ---------------------------------------------------------------------------
    WHY THE WIZARD UPLOADS FIRST AND SUBMITS AN ID
    ---------------------------------------------------------------------------
    A deploy request is JSON that is validated, logged, stored in the registry as
    the record of intent, and shown back to the PI on the review screen. A corpus
    embedded in it would be megabytes of base64 in all four places. So the files
    go here, and the deployment carries a handle.

    ---------------------------------------------------------------------------
    ALL OR NOTHING
    ---------------------------------------------------------------------------
    One unusable file rejects the whole upload. The friendlier alternative —
    accept what we can, report the rest — was rejected deliberately: it means a
    PI who dropped ten PDFs and one README gets a project silently built from the
    README, and discovers a month later that their corpus was never indexed.
    Refusing outright, naming the files, costs one more click and cannot mislead.
    """
    if not files:
        raise UploadRejected("No files were uploaded.")

    if len(files) > MAX_UPLOAD_FILES:
        raise UploadRejected(
            f"That is {len(files)} files. Upload at most {MAX_UPLOAD_FILES} at a time."
        )

    # --- validate EVERYTHING before writing anything ----------------------
    # Nothing touches the disk until the whole set is known to be acceptable, so
    # a rejected upload leaves no directory behind to be cleaned up later.
    unusable: list[str] = []
    wrong_type: list[str] = []
    seen: set[str] = set()
    staged: list[tuple[str, bytes]] = []
    total = 0

    for item in files:
        name = _sanitize(item.filename)
        if name is None:
            unusable.append(item.filename or "(unnamed)")
            continue
        if not name.lower().endswith(UPLOAD_EXTENSIONS):
            wrong_type.append(name)
            continue
        if name in seen:
            raise UploadRejected(
                f"Two files are both called '{name}'. Give them different names — "
                f"the filename is what each answer cites."
            )
        seen.add(name)

        # STREAMED TO A TEMPORARY FILE, NOT BUFFERED.
        #
        # At the old 5MB ceiling, reading into memory was the simpler choice and
        # cost nothing. At 200MB it is a 200MB allocation in the BFF per upload,
        # and two concurrent uploads would be 400MB of resident memory in a
        # process whose whole job is to shell out to kubectl.
        #
        # So each part is copied to a temp file in 1MB chunks, and the ceiling is
        # enforced AS IT STREAMS — the upload is refused at the byte that crosses
        # the line, rather than after the whole thing has been received.
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
        staged.append((name, Path(handle.name)))
        try:
            with handle:
                while True:
                    chunk = await item.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_TOTAL_BYTES:
                        raise UploadRejected(
                            f"Those files come to more than "
                            f"{_human_bytes(MAX_UPLOAD_TOTAL_BYTES)}. "
                            f"Upload a smaller set."
                        )
                    handle.write(chunk)
        except BaseException:
            # A rejection mid-stream must not leave a temp file behind. The
            # cleanup below handles the accepted-then-rejected case; this one
            # covers the part that was being written when it was refused.
            Path(handle.name).unlink(missing_ok=True)
            raise

    if wrong_type:
        raise UploadRejected(
            f"Only {' and '.join(UPLOAD_EXTENSIONS)} files can be indexed. "
            f"{'This file is' if len(wrong_type) == 1 else 'These files are'} "
            f"not: {', '.join(wrong_type[:5])}"
            + (f" (and {len(wrong_type) - 5} more)" if len(wrong_type) > 5 else ""),
            files=wrong_type,
        )
    if unusable:
        raise UploadRejected(
            f"{len(unusable)} file(s) could not be read because of their names. "
            f"Use letters, numbers, dots, dashes and underscores.",
            files=unusable,
        )
    if not staged:
        raise UploadRejected("None of those files can be indexed.")
    if all(path.stat().st_size == 0 for _, path in staged):
        for _, path in staged:
            path.unlink(missing_ok=True)
        raise UploadRejected("Those files are empty, so there would be nothing to run over.")

    # --- write ------------------------------------------------------------
    # The id is SERVER-MINTED, so the directory a client writes into can never be
    # steered by anything the client sent.
    upload_id = uuid.uuid4().hex[:16]
    target = uploads_root() / upload_id
    target.mkdir(parents=True, exist_ok=True)
    # MOVED, not copied: the bytes are already on the same filesystem, and a
    # second copy of 200MB is 200MB of pointless I/O.
    for name, temp_path in staged:
        shutil.move(str(temp_path), str(target / name))

    return _upload_manifest(upload_id)


@app.get("/api/uploads/{upload_id}", tags=["uploads"])
def read_upload(upload_id: str) -> dict[str, Any]:
    """The manifest for a staged corpus.

    Used by the wizard's review step to list the documents that will be indexed,
    and usable afterwards to answer "what did this project actually get?" — the
    id is recorded in the deployment's params, so the two can be joined.
    """
    # The id goes into a path, so it is checked as a NAME rather than trusted.
    # validation.py enforces the same shape on the submitted parameter, and the
    # executor resolves the joined path and checks it again; this is the third.
    if not re.fullmatch(r"[a-z0-9]{1,40}", upload_id):
        raise HTTPException(status_code=404, detail=f"no such upload: {upload_id}")
    directory = uploads_root() / upload_id
    if not directory.is_dir():
        raise HTTPException(
            status_code=404,
            detail=(
                f"no staged documents for id '{upload_id}'. Uploads are kept on "
                f"this machine only; if the repository was cleaned, upload again."
            ),
        )
    return _upload_manifest(upload_id)


# The RAG service's own port. Its Service publishes 8080 (see
# templates/catalog/rag-stack/resources/rag-api.yaml.tmpl); the LOCAL side of
# the forward is chosen freshly each time from an ephemeral port, so it can
# never collide with 18080 or with a port a PI is forwarding by hand.
RAG_SERVICE_PORT = 8080


async def _invoke_rag(
    deployment_id: str, row: dict[str, Any], req: InvokeRequest
) -> dict[str, Any]:
    """Ask a RAG project a question, from the server.

    THE BFF DOES THE RETRIEVAL-FREE PART OF THE WORK AND NOTHING ELSE. It opens
    (or reuses) a forward to the project's own Service and passes the question
    through; the prompt, the search and the citations are all built inside the
    project, by the container the pipeline deployed. Studio holds no authority
    the platform does not already enforce (CLAUDE.md §6), and that includes not
    being the thing that decides what a PI's model is told.
    """
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(
            status_code=400,
            detail="this project answers questions about your documents; send {\"question\": \"...\"}",
        )

    ns = f"p-{deployment_id}"
    service = f"{deployment_id}-rag"

    # Lazily created, cached across requests, and health-checked on every use —
    # the same restart logic the Kourier forward uses, for the same reason:
    # `kubectl port-forward` stays alive with a broken tunnel underneath it more
    # often than it exits cleanly.
    forward = dataplane.forwards.get(ns, service, RAG_SERVICE_PORT, "/healthz")
    try:
        port = await forward.ensure()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"could not reach this project's endpoint ({exc}). If the project "
                f"was torn down or has expired, it is gone rather than asleep — a "
                f"RAG service does not scale to zero."
            ),
        ) from exc

    payload: dict[str, Any] = {"question": question}
    if req.topK is not None:
        payload["topK"] = req.topK

    started = time.monotonic()
    try:
        # Longer than the service's own 180s chat timeout, so a cold start
        # inside the project surfaces as the service's clear 504 rather than as
        # this proxy giving up first and reporting a less specific one.
        async with httpx.AsyncClient(timeout=200.0) as client:
            response = await client.post(f"http://127.0.0.1:{port}/chat", json=payload)
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=(
                "the project did not answer within 200 seconds. If its model was "
                "asleep, the first question also has to start it."
            ),
        ) from None
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"could not reach the RAG service: {exc}"
        ) from exc

    elapsed = int((time.monotonic() - started) * 1000)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text

    # RETURNED VERBATIM, under `body`, with the round-trip time beside it. The
    # service's own `latencyMs` measures retrieval plus the model; this one adds
    # the forward and the proxy, and a PI comparing the two can see which half
    # is slow. Reshaping the service's answer here would put a second definition
    # of "what a RAG response looks like" in a second codebase.
    return {
        "status": response.status_code,
        "latencyMs": elapsed,
        # A RAG service is an ordinary Deployment and does not scale to zero, so
        # there is no cold start of ITS OWN. The model behind it may well have
        # been asleep, and that shows up as a large latencyMs.
        "coldStart": False,
        "body": body,
    }


@app.post("/api/deployments/{deployment_id}/invoke", tags=["deployments"])
async def invoke(deployment_id: str, req: InvokeRequest) -> dict[str, Any]:
    """Call the model, from the server.

    NEVER FROM THE BROWSER. A local endpoint is only reachable through the
    port-forward plus a `Host:` header, which is a local shortcut standing in for
    MetalLB + Kong (CLAUDE.md §3). Proxying here means the browser never learns a
    URL shape that will not exist in production — at migration this function
    changes and no frontend code does.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    if executor_mode() != "real":
        raise HTTPException(
            status_code=409,
            detail="the playground needs STUDIO_EXECUTOR=real — a mock project has no endpoint to call",
        )

    # BRANCH ON THE TEMPLATE, NOT ON THE REQUEST BODY. A RAG project and an
    # inference project are reached by completely different paths — one through
    # Knative's gateway with a Host header, one through a per-project forward to
    # an ordinary Service — and which applies is a fact about the project, not
    # something a caller should be able to choose.
    if row["templateId"] == "rag-stack":
        return await _invoke_rag(deployment_id, row, req)

    if not dataplane.data_path.status["ready"]:
        raise HTTPException(
            status_code=503,
            detail=(
                "the data path to the cluster is not ready. "
                f"{dataplane.data_path.status['detail']}"
            ),
        )

    facts = await live.isvc_facts(deployment_id)
    if not facts["exists"] or not facts["url"]:
        raise HTTPException(status_code=409, detail="this project has no endpoint yet")

    model = get_catalog().model(row["params"].get("model", ""))
    if model is None:
        raise HTTPException(status_code=409, detail="this project's model is no longer in the catalogue")

    # The Host header IS the routing. Kourier has one address and decides which
    # service a request belongs to entirely from this header, which is why the
    # port-forward alone is not enough to reach anything.
    host = facts["url"].replace("http://", "").replace("https://", "").split("/")[0]

    if model.interface == "chat":
        path = model.invokePath
        payload: dict[str, Any] = {
            "model": model.id,
            "messages": req.messages or [],
            # v1 of the playground does not stream. A streamed reply would need an
            # SSE relay through the BFF, and the first thing to prove is that the
            # round trip works at all.
            "stream": False,
        }
    else:
        # KServe V1 puts the served model's name in the path, and KServe names the
        # model after the InferenceService — which on the host is the SYNCED name,
        # not the project name. This is the exact path scripts/curl-isvc.sh used
        # successfully in the S5 proof.
        path = model.invokePath.replace("{name}", facts["syncedName"] or deployment_id)
        payload = req.input if req.input is not None else model.sampleInput

    pods = await live.predictor_pods(deployment_id, facts["syncedName"])
    cold = pods == 0
    if cold:
        live.mark_waking(deployment_id)

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"http://127.0.0.1:{dataplane.PORT}{path}",
                json=payload,
                headers={"Host": host, "Content-Type": "application/json"},
            )
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            body: Any = response.json()
        except ValueError:
            # Pass the upstream text through unchanged. A gateway error page or a
            # runtime traceback is exactly what the person debugging needs to see.
            body = response.text
        return {
            "status": response.status_code,
            "latencyMs": elapsed,
            "coldStart": cold,
            "body": body,
        }
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=(
                "the model did not answer within 120 seconds. If it was asleep, the "
                "first request also has to pull the image and load the model."
            ),
        ) from None
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"could not reach the model: {exc}") from exc
    finally:
        live.clear_waking(deployment_id)


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    displayName: str


@app.patch("/api/deployments/{deployment_id}", tags=["deployments"])
def rename_deployment(deployment_id: str, req: RenameRequest) -> dict[str, Any]:
    """Change the human label. REGISTRY ONLY — nothing on the cluster moves.

    The project id is the namespace, the vCluster name and the endpoint hostname;
    renaming any of those would mean destroying and rebuilding the project. This
    is a label, and the UI says so where it is edited.
    """
    if db.get_deployment_full(deployment_id) is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    db.set_display_name(deployment_id, req.displayName)
    return {"id": deployment_id, "displayName": req.displayName.strip() or deployment_id}


@app.post("/api/deployments/{deployment_id}/renewal", status_code=201, tags=["deployments"])
def request_renewal(deployment_id: str) -> dict[str, Any]:
    """Record a request for more time. Studio grants nothing.

    Extending a project means holding scarce shared hardware for longer, which is
    a decision about other people's access as much as this PI's. Studio records
    the ask; in production it routes to the Research Committee. Locally there is
    nobody to route to, and the response says so rather than implying an approval
    queue exists.
    """
    row = db.get_deployment_full(deployment_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such deployment: {deployment_id}")
    when = db.record_renewal_request(deployment_id)
    return {
        "id": deployment_id,
        "requestedAt": when,
        "requests": db.renewal_requests(deployment_id),
    }
