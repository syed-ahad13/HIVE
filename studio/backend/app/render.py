# ===========================================================================
# HIVE STUDIO BFF — THE RENDERER
# ===========================================================================
# Turns a template's fragments plus a PI's answers into finished Kubernetes
# manifests, as strings.
#
# ---------------------------------------------------------------------------
# PURE ON PURPOSE
# ---------------------------------------------------------------------------
# Nothing here writes a file, applies anything, or reads the clock unless it is
# handed one. `render_all(...)` takes its inputs and returns text. That matters
# for three reasons:
#
#   1. THE DRY RUN AND THE REAL RUN MUST PRODUCE IDENTICAL BYTES. The review step
#      of the wizard shows a PI exactly what will be created; if the preview came
#      from a different code path than the thing that eventually gets applied, the
#      preview would be a plausible-looking lie. Same function, same output.
#   2. IT IS TESTABLE WITHOUT A CLUSTER, without a database, and without a clock —
#      pass `now` and the output is deterministic.
#   3. INVARIANT (a) STAYS HONEST. "Render before apply" means Studio writes the
#      manifest to tenants/<project>/rendered/ and applies THE FILE IT WROTE. The
#      writing belongs to the executor (S5); this module only produces the content,
#      so there is exactly one place that decides what gets written and one place
#      that writes it.
#
# The dry-run endpoint therefore creates nothing at all — not even a rendered/
# directory. A preview that left files behind for a project that was never
# deployed would put junk in the repository under the name of an invariant.
#
# ---------------------------------------------------------------------------
# THE TWO-PASS CONTRACT
# ---------------------------------------------------------------------------
# Documented at the top of every fragment, and implemented here:
#
#   PASS 1 — CONDITIONAL BLOCKS. Lines between `#@if <key>=<value>` and `#@end`
#     survive only when the render facts contain that key/value pair. Marker lines
#     are always removed. Blocks do not nest.
#
#   PASS 2 — string.Template SUBSTITUTION over what pass 1 left. `$name` and
#     `${name}`; a literal dollar is `$$`.
#
# The order is not arbitrary. Conditionals run FIRST so that a placeholder inside
# a dropped block never has to resolve — which is what lets the sklearn entry's
# empty `runtime` work: the whole `runtime:` line is removed before substitution,
# rather than rendering as `runtime:` with nothing after it, which KServe rejects.
# ===========================================================================

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any

from .catalog import catalog_dir, fragment_for
from .schema import ModelEntry, UseCaseTemplate

# `vcluster` is a pipeline step but NOT a fragment: a vCluster is created by
# running `vcluster create`, not by applying a manifest (CLAUDE.md §6). It is the
# one resource with nothing to render.
FRAGMENTLESS = frozenset({"vcluster"})

# ---------------------------------------------------------------------------
# THE IMAGES THIS RENDERER WRITES INTO MANIFESTS
# ---------------------------------------------------------------------------
# Pinned here rather than in the fragments, so upgrading one is a single edit
# that every rendered manifest picks up, and so a rendered file records the
# exact reference that was current when it was applied.
#
# NOT read from the environment. A renderer that consulted os.environ would stop
# being pure and, worse, two people with different shells would render different
# manifests from the same inputs — which is precisely what invariant (a)'s
# "reviewable and reproducible" claim rules out.

# Built locally by scripts/build-rag-toolkit.sh and `kind load`ed; it exists in
# no registry, so every manifest using it sets imagePullPolicy: Never.
#
# THIS STRING IS ALSO IN scripts/build-rag-toolkit.sh. The script greps this
# very line and refuses to run if the two disagree, because a mismatch would put
# an image that was never built into every manifest and surface three steps
# later as ImagePullBackOff.
RAG_TOOLKIT_IMAGE = "hive-local/rag-toolkit:0.1.0"

# Pinned tag, arm64 confirmed by `docker manifest inspect qdrant/qdrant:v1.19.0`
# (linux/amd64, linux/arm64). Unlike the toolkit this IS pulled from a registry,
# so the kind nodes fetch it themselves.
QDRANT_IMAGE = "qdrant/qdrant:v1.19.0"

# The throwaway `kubectl cp` target. busybox because `kubectl cp` shells out to
# `tar` INSIDE the target container — verified by running
# `docker run --rm busybox:1.37 tar --help` — which a scratch or distroless
# image would not have. arm64 confirmed by `docker manifest inspect`.
STAGE_IMAGE = "busybox:1.37"

# ---------------------------------------------------------------------------
# THE RUNTIME A TEMPLATE PINS
# ---------------------------------------------------------------------------
# Normally the runtime is the MODEL's property: models.yaml says which
# ClusterServingRuntime an entry needs. rag-stack overrides it, because its
# requirement belongs to the use case rather than to the model: retrieval puts
# passages in the prompt, and a runtime whose context is too small truncates
# them SILENTLY, from the front, where the passages are.
#
# Expressed here, once, as a pure function — so the fragment that renders the
# runtime and the `approve` step that checks the runtime exists are asking the
# same question and cannot drift.
RAG_CHAT_RUNTIME = "hive-llama-cpp-rag"

# ---------------------------------------------------------------------------
# THE BATCH DRIVER IMAGE
# ---------------------------------------------------------------------------
# Built by scripts/build-batch-driver.sh and loaded into the kind nodes; it
# lives in no registry, which is why every manifest that names it also sets
# `imagePullPolicy: Never`.
#
# THE TAG IS WRITTEN IN TWO PLACES — here and in that script — and the script
# CHECKS they agree, failing loudly if they do not. A drift between them would
# render manifests naming an image that was never built, and the symptom would
# appear three steps later as ImagePullBackOff.
# 0.2.0: the driver was posting KServe V1 predictions to /v1/models/<catalogue
# model id>:predict while the sidecar served them under the PROJECT name, so
# every row of every classic-ML run 404'd. The tag is bumped rather than the
# contents quietly changed because scripts/build-batch-driver.sh builds only
# when the tag is MISSING — an edit under the same tag would never reach a
# machine that had already built it.
BATCH_DRIVER_IMAGE = "hive-local/batch-driver:0.2.0"

# The port every serving runtime in this catalogue listens on.
#
# A CONSTANT, BECAUSE THE ClusterServingRuntime DOES NOT CARRY IT. Both installed
# runtimes have `.spec.containers[0].ports == null` — verified by reading the
# live objects, not assumed — so there is nothing to read. 8080 is what both
# runtimes' own args actually bind to (`--http_port=8080`, `--port 8080`), and
# the batch Job publishes it and points the driver at it.
#
# If a future runtime binds elsewhere, this is the one place that changes, and
# the runtime's args are where the disagreement would be visible.
RUNTIME_PORT = 8080

# THE COLLECTION NAME IS DERIVED, NOT STORED. It is a pure function of the
# project name, so the ingestion Job, the RAG service, the teardown and the
# project page's live point-count read all agree without any of them having to
# read it back from the registry — which invariant (b) would forbid anyway.
COLLECTION_SUFFIX = "-corpus"

# ---------------------------------------------------------------------------
# WHAT THE PREVIEW SHOWS FOR A VALUE THAT CANNOT EXIST YET
# ---------------------------------------------------------------------------
# The wizard's review step renders every manifest BEFORE anything is created.
# One value is genuinely unknowable at that moment: the chat model's hostname,
# which `ready-check` reads LIVE from the InferenceService's `.status.url`
# (never constructed — local URLs carry a `-predictor` segment production will
# not have, DEVIATIONS.md).
#
# So the preview shows this sentinel and the deploy substitutes the real host.
# `.invalid` is the RFC 2606 reserved TLD: it can never resolve, so if this ever
# leaked into a running deployment the failure would be immediate and obvious
# rather than a subtly wrong Host header routing to the wrong service.
#
# This is the ONE place preview bytes and applied bytes differ, and it is why
# the difference is a named constant a reader can grep for rather than an empty
# string that would look like a bug.
PENDING_CHAT_HOST = "resolved-at-deploy-time.invalid"


def collection_name(project: str) -> str:
    """The Qdrant collection for a project. Derived, never stored."""
    return f"{project}{COLLECTION_SUFFIX}"


# ---------------------------------------------------------------------------
# THE ONE FRAGMENT RENDERED TWICE IN A SINGLE RUN
# ---------------------------------------------------------------------------
# `staging-pod.yaml.tmpl` serves both directions of the file move: the dataset
# in (read-write) and the results out (read-only). Same problem, same manifest,
# one fact apart — see RESOURCE_TO_FRAGMENT in app/catalog.py.
#
# That makes it the only resource whose FRAGMENT NAME is not a usable filename:
# two files called `staging-pod.yaml` would leave rendered/ holding whichever
# was written second, losing the record of what the first step applied, which is
# the whole of invariant (a). The executor already wrote them apart; this table
# is what lets the wizard's PREVIEW name them the same way, instead of showing
# the same filename twice for two different manifests.
#
# `mode` names the file (`staging-pod-rw.yaml`), `suffix` names the pod
# (`<project>-stage-in`). Two vocabularies because they answer different
# questions — how the volume is mounted, and which direction the files move —
# and collapsing them would make one of the two names lie.
STAGING_RENDERS: dict[str, tuple[str, str]] = {
    "dataset-upload": ("rw", "in"),
    "collect-results": ("ro", "out"),
}


def staging_render(project: str, resource: str) -> tuple[str, dict[str, str]] | None:
    """(filename, extras) for a staging-pod render, or None for anything else."""
    entry = STAGING_RENDERS.get(resource)
    if entry is None:
        return None
    mode, suffix = entry
    return (
        f"staging-pod-{mode}.yaml",
        {"stage_mode": mode, "stage_name": f"{project}-stage-{suffix}"},
    )


# ===========================================================================
# THE CURATED RUNTIME, FOR A BATCH RUN
# ===========================================================================
# A batch run has no InferenceService, so KServe never gets to pick a runtime or
# render its container. Studio has to do both — and the whole argument of the
# batch template is that it does so from THE SAME OBJECTS KServe would have
# used, rather than from anything written in this repository.
#
# Both functions below are PURE. The executor performs the one `kubectl get
# clusterservingruntimes -o json` and hands the result in, so this module keeps
# its property of being inputs-to-text with no cluster access, and both rules
# are unit-testable with no cluster at all.


def select_runtime(model: ModelEntry, runtimes: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the ClusterServingRuntime a batch run should use for this model.

    TWO RULES, IN ORDER, MIRRORING WHAT KServe ITSELF DOES:

      1. THE CATALOGUE PINS ONE -> use exactly that. `models.yaml` naming a
         runtime is a governance decision by the platform team, and a batch run
         that quietly used a different one would break the claim this template
         exists to make.

      2. THE CATALOGUE PINS NOTHING (`runtime: ""`) -> resolve from
         `modelFormat`, the way KServe would: among runtimes declaring that
         format with `autoSelect: true`, take the lowest `priority` number,
         tie-broken by name so the choice is deterministic across runs rather
         than dependent on list order from the API server.

         `runtime: ""` is a MEANINGFUL value, not a missing one — sklearn-iris
         uses it deliberately so the platform can rename its runtime without
         editing the catalogue.

    Raises RuntimeUnavailable, naming what was looked for and what exists,
    because "the runtime is missing" is a platform problem a PI cannot act on
    and the message has to be aimed at whoever can.
    """
    by_name = {r.get("metadata", {}).get("name"): r for r in runtimes}
    available = ", ".join(sorted(n for n in by_name if n)) or "none"

    pinned = (model.runtime or "").strip()
    if pinned:
        if pinned not in by_name:
            raise RuntimeUnavailable(
                f"the model '{model.displayName}' pins the serving runtime "
                f"'{pinned}', which is not installed on this cluster.\n"
                f"Installed runtimes: {available}.\n"
                f"A batch run uses the same curated runtime a served endpoint "
                f"would, so it cannot fall back to another one. Either install "
                f"that runtime or re-point the catalogue entry at one that exists."
            )
        return by_name[pinned]

    fmt = (model.modelFormat or "").strip()
    if not fmt:
        raise RuntimeUnavailable(
            f"the model '{model.displayName}' pins no runtime and declares no "
            f"modelFormat, so there is nothing to resolve a runtime from."
        )

    candidates = []
    for runtime in runtimes:
        for declared in runtime.get("spec", {}).get("supportedModelFormats", []):
            if declared.get("name") != fmt or not declared.get("autoSelect"):
                continue
            # Absent priority sorts LAST. KServe treats an unset priority as the
            # weakest claim on a format, and a runtime that forgot to declare one
            # must not outrank a runtime that thought about it.
            priority = declared.get("priority")
            candidates.append(
                (priority if priority is not None else 10**6,
                 runtime.get("metadata", {}).get("name", ""),
                 runtime)
            )

    if not candidates:
        raise RuntimeUnavailable(
            f"no installed ClusterServingRuntime declares modelFormat '{fmt}' with "
            f"autoSelect, so nothing can serve '{model.displayName}' in a batch "
            f"run.\nInstalled runtimes: {available}."
        )
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def runtime_container(runtime: dict[str, Any], rendered_name: str) -> dict[str, Any]:
    """The image, command and args a batch Job should run, from one CSR.

    Returns JSON-encoded command/args, because the fragment substitutes them into
    a YAML flow sequence: a JSON array IS valid YAML, so one placeholder becomes
    one line and there is no indentation to get wrong. `null` for an absent
    command is exactly what Kubernetes wants — "use the image's ENTRYPOINT".

    THREE THINGS THE LIVE OBJECTS MADE NECESSARY, each found by reading them
    rather than assuming the shape:

      1. `command` IS SET on hive-llama-cpp-rag and ABSENT on
         kserve-sklearnserver. Returning only args would drop llama.cpp's entire
         startup script, and the container would run the image's default
         ENTRYPOINT against arguments meant for something else.

      2. `{{.Name}}` APPEARS IN THE ARGS OF BOTH, and KServe normally renders it
         from the InferenceService's name. There is no InferenceService here, so
         it is substituted with `rendered_name`. Left alone it reaches
         llama.cpp as a literal `--alias {{.Name}}`, and reaches sklearnserver
         as a model named `{{.Name}}` that no predict path will ever match.

      3. `ports` IS ABSENT on both, so there is nothing to read and RUNTIME_PORT
         is the answer — see the constant.
    """
    containers = runtime.get("spec", {}).get("containers", [])
    if not containers:
        name = runtime.get("metadata", {}).get("name", "<unnamed>")
        raise RuntimeUnavailable(f"ClusterServingRuntime '{name}' declares no containers")

    # `kserve-container` by convention on every runtime in this catalogue; the
    # first container is the fallback so a runtime that names it differently
    # still works rather than failing on a convention it never agreed to.
    container = next(
        (c for c in containers if c.get("name") == "kserve-container"), containers[0]
    )

    def render_template_vars(values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return [str(v).replace("{{.Name}}", rendered_name) for v in values]

    resources = container.get("resources", {}) or {}
    requests = resources.get("requests", {}) or {}
    limits = resources.get("limits", {}) or {}

    return {
        "name": runtime.get("metadata", {}).get("name", ""),
        # THE NAME THE RUNTIME IS ACTUALLY SERVING UNDER, returned rather than
        # left implicit — because a second thing in the pod has to agree with
        # it. `{{.Name}}` becomes `--model_name=<this>` for sklearnserver, and
        # KServe V1 puts that same name in the request path
        # (`/v1/models/{name}:predict`, models.yaml). The driver therefore has
        # to be told it; a driver that guessed — by using the catalogue's model
        # id, say — posts to a path the runtime does not serve and EVERY ROW
        # 404s, which reads as a bad dataset rather than a wrong URL.
        #
        # Carried out of the one function that performed the substitution, so
        # the args and the path are rendered from the same string by
        # construction.
        "served_as": rendered_name,
        "image": container["image"],
        # json.dumps, not str(): a Python list repr uses single quotes, which is
        # not valid JSON and only accidentally valid YAML.
        "command_json": json.dumps(render_template_vars(container.get("command"))),
        "args_json": json.dumps(render_template_vars(container.get("args"))),
        "port": RUNTIME_PORT,
        # The runtime's OWN resource numbers, carried through rather than
        # invented here. A platform engineer who sized a runtime did so for a
        # reason, and a batch run should not quietly give it less.
        "cpu_request": requests.get("cpu", "500m"),
        "memory_request": requests.get("memory", "1Gi"),
        "cpu_limit": limits.get("cpu", "2"),
        "memory_limit": limits.get("memory", "3Gi"),
    }


def required_runtime(template_id: str, model: ModelEntry | None) -> str:
    """The ClusterServingRuntime the rendered InferenceService will pin.

    "" means the template pins none and KServe resolves one from `modelFormat`
    alone — a MEANINGFUL value (sklearn-iris uses it deliberately), not a
    missing one.

    Used by the renderer AND by the real executor's precondition check, so that
    `approve` asks whether the runtime this deployment will actually use exists,
    rather than the one the catalogue entry happens to name.
    """
    if template_id == "rag-stack":
        return RAG_CHAT_RUNTIME
    return model.runtime.strip() if model else ""

_IF = re.compile(r"^\s*#@if\s+([A-Za-z0-9_]+)\s*=\s*(\S+)\s*$")
_END = re.compile(r"^\s*#@end\s*$")


class RenderError(RuntimeError):
    """A fragment could not be rendered. Always names the fragment and the cause."""


class RuntimeUnavailable(RenderError):
    """No usable ClusterServingRuntime for the chosen model. Names what was tried."""


# ---------------------------------------------------------------------------
# PASS 1
# ---------------------------------------------------------------------------


def apply_conditionals(text: str, facts: dict[str, str], where: str = "<fragment>") -> str:
    """Keep or drop `#@if` blocks according to the render facts.

    An `#@if` naming a fact that was not supplied is an ERROR, not a silent drop.
    A missing fact means the renderer and the fragment disagree about what this
    template needs, and silently dropping the block would quietly produce a
    manifest with a section missing — for example an on-demand service with no
    scale-down settings, which would then behave as always-on and hold a GPU
    forever. That is precisely the Gate G3 failure, and it must not be
    reintroduced by a typo in a fact name.
    """
    out: list[str] = []
    keep = True
    open_at: int | None = None

    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        match_if = _IF.match(line)
        if match_if:
            if open_at is not None:
                raise RenderError(
                    f"{where}: nested #@if at line {number} (opened at line {open_at}); "
                    f"blocks do not nest"
                )
            key, value = match_if.group(1), match_if.group(2)
            if key not in facts:
                raise RenderError(
                    f"{where}: line {number} tests unknown render fact {key!r} "
                    f"(known: {', '.join(sorted(facts))})"
                )
            keep = facts[key] == value
            open_at = number
            continue

        if _END.match(line):
            if open_at is None:
                raise RenderError(f"{where}: #@end at line {number} with no open #@if")
            keep = True
            open_at = None
            continue

        if keep:
            out.append(line)

    if open_at is not None:
        raise RenderError(f"{where}: #@if opened at line {open_at} was never closed")

    return "".join(out)


# ---------------------------------------------------------------------------
# PASS 2
# ---------------------------------------------------------------------------


def substitute(text: str, values: dict[str, Any], where: str = "<fragment>") -> str:
    """Fill placeholders, refusing to leave any unresolved.

    `substitute`, never `safe_substitute`. Safe substitution leaves an unknown
    `$placeholder` sitting in the output, which would be applied to the cluster
    verbatim — a manifest containing a literal `$project` is far worse than a
    render that refused to finish.
    """
    try:
        return Template(text).substitute(values)
    except KeyError as exc:
        raise RenderError(
            f"{where}: no value supplied for placeholder ${exc.args[0]} "
            f"(supplied: {', '.join(sorted(values))})"
        ) from exc
    except ValueError as exc:
        # Raised for a malformed placeholder, e.g. a bare `$` that should be `$$`.
        raise RenderError(f"{where}: malformed placeholder — {exc}") from exc


# ---------------------------------------------------------------------------
# FACTS AND VALUES
# ---------------------------------------------------------------------------


def expiry_from(now: datetime, duration_days: int) -> str:
    """The `hive.ksl/expiry` instant: RFC3339, UTC, seconds precision.

    THE CLOCK STARTS AT PROVISIONING AND NEVER PAUSES (CLAUDE.md §2.6). An
    on-demand project that sleeps for its entire allocation still expires on
    schedule — sleeping is a cost-saving behaviour, not a suspension of the
    lifetime. Studio only SETS this; the host reconciler CronJob is what honours it.
    """
    if not isinstance(duration_days, int) or isinstance(duration_days, bool):
        raise RenderError(f"durationDays must be a whole number, got {duration_days!r}")
    stamp = (now.astimezone(timezone.utc) + timedelta(days=duration_days)).replace(
        microsecond=0
    )
    return stamp.isoformat().replace("+00:00", "Z")


# The parameters that can drive the expiry a namespace is CREATED with, in the
# order they are tried. Two entries, because the two shapes of template answer a
# different question and neither declares the other's parameter:
#
#   durationDays           a SERVICE. "How long do you need it?" The PI is
#                          choosing the project's lifetime, the value is final,
#                          and nothing moves it afterwards.
#   artifactRetentionDays  a RUN. "Keep the results for". The PI is choosing how
#                          long RESULTS survive, and that clock cannot start
#                          until there are results — so this is written as a
#                          SAFETY NET at provisioning (always EARLIER than the
#                          real value) and `collect-results` replaces it with
#                          completion + retention once the work is done. See the
#                          expiry note in batch-inference's namespace-pack
#                          fragment, which states the same contract from the
#                          other side.
#
# A LIST RATHER THAN A PER-TEMPLATE TABLE. The question "which parameter is the
# clock?" is already answered, per template, in `spec.lifecycle` — and the
# catalogue loader refuses a template whose lifecycle names a parameter it does
# not declare. This module is pure and is handed params, not the template, so it
# asks the params; a second table here could disagree with the lifecycle, and
# there would be no way to tell which was right.
EXPIRY_PARAMETERS: tuple[str, ...] = ("durationDays", "artifactRetentionDays")


def provisioning_expiry(now: datetime, params: dict[str, Any]) -> str:
    """The `hive.ksl/expiry` a namespace is created with.

    EVERY PROVISIONED NAMESPACE GETS ONE. A namespace with no expiry is the
    failure worth designing against: the host reconciler would never collect it,
    and the project would outlive its own purpose with a GPU quota against its
    name. So this raises rather than omitting the annotation.

    This used to read `durationDays` unconditionally, which was correct while
    every template was a service. A run declares `artifactRetentionDays` instead
    and nothing else, so `namespace-pack` — the SECOND step of every batch
    deployment — could not render at all.
    """
    for name in EXPIRY_PARAMETERS:
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return expiry_from(now, value)
    raise RenderError(
        "namespace-pack needs an expiry clock, and this submission names none. "
        "A template must declare one of "
        + " / ".join(EXPIRY_PARAMETERS)
        + " (spec.lifecycle says which, and the catalogue loader enforces it)."
    )


def min_scale_for(availability: str) -> str:
    """"1" for always-on, "0" for on-demand.

    Returned as a STRING because it lands in two places with different needs: a
    Knative annotation, which must be quoted, and `spec.predictor.minReplicas`,
    which must not be. The fragment quotes the annotation itself and leaves the
    field bare, so one value serves both.
    """
    if availability == "always-on":
        return "1"
    if availability == "on-demand":
        return "0"
    raise RenderError(
        f"unknown availability {availability!r}; expected 'always-on' or 'on-demand'"
    )


def facts_for(
    template_id: str,
    fragment: str,
    params: dict[str, Any],
    model: ModelEntry | None,
    extras: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The pass-1 facts each fragment is entitled to test.

    Deliberately per-fragment rather than one global dictionary: a fragment that
    tests a fact it was not given is a mistake worth catching (see
    apply_conditionals), and that check only works if each fragment's fact set is
    exactly what it should be able to see.

    ALSO PER-TEMPLATE, and that is not redundancy. Both templates have an
    `inference-service` fragment, and they differ in exactly one way:
    inference-endpoint's takes the runtime from the catalogue entry and must
    therefore handle it being empty, while rag-stack's PINS the runtime and can
    never render it blank. Handing rag-stack's fragment a `runtime` fact it does
    not test would still work — an unused fact is harmless — but it would blunt
    the check above into "some facts are available", which is the version that
    stops catching typos.
    """
    if fragment == "namespace-pack":
        return {"tier": str(params.get("tier", ""))}

    if fragment == "inference-service":
        facts = {"availability": str(params.get("availability", ""))}
        if template_id != "rag-stack":
            # "set" only when the catalogue entry names a runtime. An empty
            # string is a MEANINGFUL value in models.yaml — it means "let KServe
            # resolve the runtime from modelFormat" — so emptiness selects the
            # branch that omits the key entirely rather than rendering it blank.
            facts["runtime"] = "set" if (model and model.runtime.strip()) else "unset"
        return facts

    # BATCH — the one fragment rendered twice, in two different modes.
    # `dataset-upload` pushes a file IN (read-write); `collect-results` pulls
    # files OUT (read-only). The same manifest, one fact apart, which is why a
    # single staging-pod.yaml.tmpl serves both steps.
    if fragment == "staging-pod":
        return {"stage_mode": str((extras or {}).get("stage_mode", "rw"))}

    return {}


def _need(model: ModelEntry | None, fragment: str, what: str) -> ModelEntry:
    """The model a fragment cannot render without, or a RenderError naming both."""
    if model is None:
        raise RenderError(f"{fragment} needs {what}, and none was selected")
    return model


def values_for(
    fragment: str,
    project: str,
    params: dict[str, Any],
    model: ModelEntry | None,
    now: datetime,
    template_id: str = "inference-endpoint",
    embed_model: ModelEntry | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The pass-2 placeholder values for one fragment.

    Args:
        model:       the SERVED model — the chat or predict entry the PI chose.
        embed_model: the `embed` entry, for the RAG fragments only. A separate
            argument rather than a second lookup inside this function, because
            this module must stay pure: it is handed its inputs and returns
            text, with no catalogue access of its own.
        extras:      values that can only be known once the cluster has been
            asked — today just `chat_host`, read live from the InferenceService's
            `.status.url` by ready-check. Absent during a dry run, where the
            documented sentinel stands in (see PENDING_CHAT_HOST).
    """
    extras = extras or {}

    if fragment == "namespace-pack":
        return {
            "project": project,
            "tier": params.get("tier", ""),
            # Which parameter this comes from depends on the SHAPE of the
            # template — a service's chosen lifetime, or a run's retention
            # period as a safety net. See provisioning_expiry.
            "ttl_expiry": provisioning_expiry(now, params),
        }

    if fragment == "model-seed-job":
        if model is None:
            raise RenderError("model-seed-job needs a model, and none was selected")
        return {
            "project": project,
            "model_seed_url": model.seedUrl,
            "model_subpath": model.storageSubpath,
        }

    if fragment == "inference-service":
        chat = _need(model, "inference-service", "a model")
        return {
            "project": project,
            "min_scale": min_scale_for(str(params.get("availability", ""))),
            "model_format": chat.modelFormat,
            # PINNED BY THE TEMPLATE for rag-stack, taken from the catalogue
            # entry otherwise — one function decides, so the `approve` step's
            # precondition check and this render can never disagree about which
            # runtime the deployment needs.
            "model_runtime": required_runtime(template_id, chat),
            # THE COMPLETE URI, joined by ModelEntry.storage_uri rather than
            # concatenated here. An empty storageSubpath must yield "pvc://models",
            # not "pvc://models/" — the trailing slash is not the form proven
            # working by the two reference tenants.
            "model_storage_uri": chat.storage_uri(),
        }

    # -----------------------------------------------------------------
    # RAG STACK
    # -----------------------------------------------------------------
    # Every fragment below is host-namespace machinery. The embedding values
    # come from a SECOND catalogue entry, and the same three of them
    # (`embed_fastembed_name`, `embed_dims`, `embed_subpath`) are rendered into
    # BOTH the ingestion Job and the RAG service from that one entry. That is
    # the correctness condition of retrieval — index and query must use the same
    # embedding — made structural: there is one source for both, so they cannot
    # be configured differently.

    if fragment == "model-seed":
        chat = _need(model, "model-seed", "a chat model")
        embed = _need(embed_model, "model-seed", "an embedding model")
        return {
            "project": project,
            "rag_image": RAG_TOOLKIT_IMAGE,
            "chat_seed_url": chat.seedUrl,
            "chat_subpath": chat.storageSubpath,
            "embed_subpath": embed.storageSubpath,
            "embed_fastembed_name": embed.fastembedName or "",
        }

    if fragment == "qdrant":
        return {"project": project, "qdrant_image": QDRANT_IMAGE}

    if fragment == "corpus-staging-pod":
        return {"project": project, "stage_image": STAGE_IMAGE}

    if fragment == "ingestion-job":
        embed = _need(embed_model, "ingestion-job", "an embedding model")
        return {
            "project": project,
            "rag_image": RAG_TOOLKIT_IMAGE,
            "embed_subpath": embed.storageSubpath,
            "embed_fastembed_name": embed.fastembedName or "",
            "embed_dims": embed.dims,
            "embed_distance": embed.distance,
            "collection": collection_name(project),
        }

    if fragment == "rag-api":
        chat = _need(model, "rag-api", "a chat model")
        embed = _need(embed_model, "rag-api", "an embedding model")
        return {
            "project": project,
            "rag_image": RAG_TOOLKIT_IMAGE,
            "collection": collection_name(project),
            "embed_subpath": embed.storageSubpath,
            "embed_fastembed_name": embed.fastembedName or "",
            "embed_dims": embed.dims,
            "top_k": params.get("topK", 3),
            # Knative's cluster-local gateway. A fixed cluster fact, VERIFIED
            # against the cluster (`kubectl get svc -n kourier-system` ->
            # `kourier` external, `kourier-internal` cluster-local) rather than
            # assumed — so the preview can show the real value. The executor
            # re-reads it live anyway and overrides this default, because a
            # cluster fact that changed silently is exactly the class of thing
            # invariant (b) exists for.
            "chat_endpoint": extras.get(
                "chat_endpoint",
                "http://kourier-internal.kourier-system.svc.cluster.local",
            ),
            # THE ONE VALUE THAT CANNOT BE KNOWN BEFORE THE DEPLOY. See
            # PENDING_CHAT_HOST — read live from .status.url, never constructed.
            "chat_host": extras.get("chat_host") or PENDING_CHAT_HOST,
            "chat_model_id": chat.id,
        }

    # =====================================================================
    # BATCH INFERENCE
    # =====================================================================
    if fragment == "staging-pod":
        # `stage_name` is passed in rather than derived so a rendered manifest is
        # traceable to the STEP that produced it — `<project>-stage-in` and
        # `<project>-stage-out` sit side by side in rendered/ and say which is
        # which, where two files both called `staging-pod.yaml` would not.
        return {
            "project": project,
            "stage_name": extras.get("stage_name", f"{project}-stage"),
        }

    if fragment == "batch-job":
        if model is None:
            raise RenderError("batch-job needs a model, and none was selected")

        # THE RUNTIME BLOCK IS NOT COMPUTED HERE. The executor did the one
        # `kubectl get clusterservingruntimes -o json`, ran it through
        # select_runtime + runtime_container, and passed the result in — so this
        # module keeps its property of being pure, and the preview a PI reads
        # comes from the same objects the deploy will use.
        runtime = extras.get("runtime")
        if not runtime:
            raise RenderError(
                "batch-job needs the resolved ClusterServingRuntime, which the "
                "executor reads from the cluster and passes in as extras['runtime']"
            )

        timeout_minutes = params.get("runTimeoutMinutes")
        if not isinstance(timeout_minutes, int) or isinstance(timeout_minutes, bool):
            raise RenderError(
                f"runTimeoutMinutes must be a whole number, got {timeout_minutes!r}"
            )

        # THE PROMPT IS JSON-ENCODED ON ITS WAY INTO THE MANIFEST. It is
        # multi-line free text written by a PI, and it has to survive a YAML
        # scalar, a container env var, and Python's os.environ before the driver
        # sees it. json.dumps makes it one safe line here; the driver json.loads
        # it back. Without this a prompt containing a newline or a quote either
        # breaks the manifest or arrives silently mangled.
        prompt = str(params.get("promptTemplate") or "")

        return {
            "project": project,
            "runtime_name": runtime["name"],
            "runtime_image": runtime["image"],
            "runtime_command_json": runtime["command_json"],
            "runtime_args_json": runtime["args_json"],
            "runtime_cpu_request": runtime["cpu_request"],
            "runtime_memory_request": runtime["memory_request"],
            "runtime_cpu_limit": runtime["cpu_limit"],
            "runtime_memory_limit": runtime["memory_limit"],
            "driver_image": BATCH_DRIVER_IMAGE,
            # What the sidecar called the model when it started. See
            # runtime_container's `served_as`.
            "model_serving_name": runtime["served_as"],
            # Minutes to seconds, once, here — rather than in the fragment where
            # arithmetic in a template is a thing nobody can test.
            "active_deadline": timeout_minutes * 60,
            # The dataset always lands under input/ with its original name; the
            # executor passes the filename it actually copied, so a run whose
            # upload was `abstracts-200.csv` reads that and not a guess.
            "input_path": extras.get("input_path", "/mnt/scratch/input/dataset.csv"),
            "input_format": params.get("inputFormat", "csv"),
            # Joined here, split in the driver. Rendering the list into the
            # manifest as a YAML sequence would mean the driver parsing YAML;
            # a comma-separated string is what an env var can carry, and column
            # names cannot contain a comma (the template's pattern forbids it).
            "input_fields": ",".join(params.get("inputFields") or []),
            "model_interface": model.interface,
            "model_id": model.id,
            "model_invoke_path": model.invokePath,
            "prompt_template_json": json.dumps(prompt),
            "max_rows": params.get("maxRows", 200),
            "batch_size": params.get("batchSize", 64),
            "concurrency": params.get("concurrency", 2),
            "max_failure_rate_pct": params.get("maxFailureRatePct", 10),
        }

    raise RenderError(f"no value set defined for fragment {fragment!r}")


# ---------------------------------------------------------------------------
# THE WHOLE RENDER
# ---------------------------------------------------------------------------


def fragment_dir(template_id: str) -> Path:
    return catalog_dir() / template_id / "resources"


def render_fragment(
    fragment: str,
    template_id: str,
    project: str,
    params: dict[str, Any],
    model: ModelEntry | None,
    now: datetime,
    embed_model: ModelEntry | None = None,
    extras: dict[str, Any] | None = None,
) -> str:
    """One fragment, both passes, as finished YAML.

    `fragment` is the FRAGMENT name (the .yaml.tmpl basename), not necessarily
    the `resources` entry — see catalog.fragment_for. Callers that start from a
    resource go through render_all or map it themselves; the executor names
    fragments directly, because it is applying a specific file.
    """
    path = fragment_dir(template_id) / f"{fragment}.yaml.tmpl"
    where = f"{template_id}/resources/{fragment}.yaml.tmpl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"{where}: cannot be read — {exc}") from exc

    stage_one = apply_conditionals(
        text, facts_for(template_id, fragment, params, model, extras), where
    )
    return substitute(
        stage_one,
        values_for(fragment, project, params, model, now, template_id, embed_model, extras),
        where,
    )


def render_all(
    template: UseCaseTemplate,
    params: dict[str, Any],
    model: ModelEntry | None,
    now: datetime | None = None,
    embed_model: ModelEntry | None = None,
    extras: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Every manifest this deployment would create, in the order they are applied.

    Returns [{name, content}]. `name` is the filename the executor will write into
    tenants/<project>/rendered/, so the preview a PI reads and the file that lands
    in git are the same thing under the same name.

    THE FILENAME IS THE FRAGMENT'S NAME, NOT THE RESOURCE'S. For most resources
    those are the same string. For the three that differ — `vector-db` renders
    qdrant.yaml, `corpus-upload` renders corpus-staging-pod.yaml, `ingestion`
    renders ingestion-job.yaml — using the resource name here would give the
    preview different filenames from the ones the executor writes, and the
    promise that the review screen shows exactly what will be applied would be
    quietly false in its most checkable detail.

    THE ONE EXCEPTION IS THE STAGING POD, for the same reason and in the other
    direction: two resources render that one fragment, so the fragment's name is
    not unique and the executor writes them apart. See staging_render.
    """
    moment = now or datetime.now(timezone.utc)
    project = str(params.get("projectName", "")).strip()
    if not project:
        raise RenderError("projectName is required to render")

    files: list[dict[str, str]] = []
    for resource in template.spec.resources or []:
        if resource in FRAGMENTLESS:
            continue
        fragment = fragment_for(resource)
        # The staging pod is the one fragment two resources share, and the two
        # renders differ. Everything else is one resource, one fragment, one
        # file — and keeps the fragment's own name.
        staged = staging_render(project, resource)
        name = staged[0] if staged else f"{fragment}.yaml"
        local_extras = {**(extras or {}), **staged[1]} if staged else extras
        files.append(
            {
                "name": name,
                "content": render_fragment(
                    fragment,
                    template.metadata.id,
                    project,
                    params,
                    model,
                    moment,
                    embed_model,
                    local_extras,
                ),
            }
        )
    return files
