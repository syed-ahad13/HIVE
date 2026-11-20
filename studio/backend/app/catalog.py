# ===========================================================================
# HIVE STUDIO BFF — CATALOGUE LOADER
# ===========================================================================
# Loads and validates every templates/catalog/**/template.yaml and models.yaml
# ONCE, at startup, and refuses to start if any of them is malformed.
#
# ---------------------------------------------------------------------------
# WHY THIS FAILS LOUD RATHER THAN SKIPPING THE BAD FILE
# ---------------------------------------------------------------------------
# A half-loaded catalogue is the failure mode to design against. If a broken
# template were skipped with a warning, the console would come up looking healthy
# and simply not offer that use case — and the first person to notice would be a PI
# who cannot find the thing they were told to use. Worse, if models.yaml partially
# loaded, the wizard's `model` enum would silently become a SHORTER list, which
# quietly narrows the governance boundary that file exists to define.
#
# So: any error in any file aborts startup, and the message names the file and the
# field. The catalogue is data the platform team commits and reviews (CLAUDE.md §6);
# a syntax error in it is a deployment mistake, and deployment mistakes should be
# loud and immediate rather than discovered by a user.
#
# ---------------------------------------------------------------------------
# WHAT IS CHECKED
# ---------------------------------------------------------------------------
#   EVERY template, regardless of maturity — structure:
#     * parses as YAML, is a mapping, declares the right apiVersion and kind
#     * matches the Pydantic schema, unknown fields forbidden
#     * metadata.id equals its directory name (so a URL path can find the file)
#     * per-type parameter sanity (a `fixed` has a value, an `integer` has a
#       coherent min/max, a `string` pattern compiles)
#
#   ONLY maturity: available — referential integrity:
#     * every enum resolves to a non-empty set of choices
#     * lifecycle.ttl.fromParameter names a parameter that exists
#     * teardownOrder entries are canonical pipeline step ids
#     * every entry in `resources` has a matching fragment file — except
#       `vcluster`, which is a pipeline step but deliberately NOT a fragment
#     * resources and outputs are present at all
#
# The split is explained in schema.py. In short: a coming-soon template is an
# honest sketch and must be a well-formed one, but only a template that actually
# provisions can be asked whether it would work.
# ===========================================================================

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from .schema import (
    CATALOG_API_VERSION,
    EnumValue,
    ModelCatalog,
    ModelEntry,
    Parameter,
    TemplateSummary,
    UseCaseTemplate,
)

# ---------------------------------------------------------------------------
# THE CANONICAL PIPELINE STEP IDS (CLAUDE.md §6)
# ---------------------------------------------------------------------------
# These strings are ONE VOCABULARY SHARED BY FOUR PLACES: the provisioning UI's
# progress list, the registry's history rows, STUDIO_FAIL_STEP, and
# `teardownOrder` in template.yaml. They must not be renamed casually or spelled
# differently in any of them. Defined here, imported everywhere else, so there is
# exactly one spelling in the backend.
#
# The first six are the original vocabulary and every template uses them. The
# last five were added for the RAG stack (studio R1) and are used only by
# templates that declare the matching resources — `plan_steps` builds each run's
# plan from the template, so an inference-endpoint deployment still runs exactly
# six steps and nothing about its history changed.
PIPELINE_STEPS: tuple[str, ...] = (
    "approve",
    "namespace-pack",
    "model-seed",
    "vcluster",
    "inference-service",
    "ready-check",
    # --- RAG stack ---------------------------------------------------------
    "vector-db",        # Qdrant Deployment + Service, in the HOST namespace
    "corpus-upload",    # the PI's documents, staged into the scratch PVC
    "ingestion",        # chunk, embed, upsert — the Job that builds the index
    "rag-api",          # the retrieval service that fronts the chat model
    "rag-ready-check",  # health + one real question, before the PI asks the first
    # --- batch inference (studio B1) ---------------------------------------
    "dataset-upload",   # the PI's dataset, staged into the scratch PVC
    "batch-run",        # the Job: curated runtime as a native sidecar + driver
    "collect-results",  # pull the artifacts back out, then set the retention clock
)

# `resources` in a template names RENDER FRAGMENTS, which is a different (though
# overlapping) vocabulary from the step ids above. `vcluster` appears in both and
# is the interesting case: it is a pipeline step but NOT a fragment, because a
# vCluster is not created by applying a manifest — the pipeline shells out to
# `vcluster create`. That is the one imperative call in the pipeline (CLAUDE.md §6),
# so it is the one `resources` entry allowed to have no .yaml.tmpl behind it.
FRAGMENTLESS_RESOURCES: frozenset[str] = frozenset({"vcluster"})

# ---------------------------------------------------------------------------
# RESOURCE -> FRAGMENT FILE
# ---------------------------------------------------------------------------
# A `resources` entry usually IS its fragment's filename, and where it is, it is
# absent from this table — the mapping falls back to identity. This holds the
# handful of places where the two vocabularies deliberately differ, because a
# resource is named for WHAT IT IS TO THE PI and a fragment is named for WHAT IS
# IN IT:
#
#   vector-db      -> qdrant.yaml.tmpl            the PI has a search index; the
#                                                 file contains Qdrant, and will
#                                                 name whatever replaces it
#   corpus-upload  -> corpus-staging-pod.yaml.tmpl  the step is an upload; the
#                                                 manifest is one throwaway pod
#                                                 that exists to be `kubectl cp`'d
#                                                 into and then deleted
#   ingestion      -> ingestion-job.yaml.tmpl     the step is indexing; the file
#                                                 is a Job
#
# Written out rather than derived by stripping suffixes: an unrecognised resource
# must be a startup error, and a rule like "try <name>-job.yaml.tmpl too" would
# turn a typo into a silently-skipped resource.
RESOURCE_TO_FRAGMENT: dict[str, str] = {
    "vector-db": "qdrant",
    "corpus-upload": "corpus-staging-pod",
    "ingestion": "ingestion-job",
    # --- batch inference ---------------------------------------------------
    # BOTH directions of the file move render the SAME fragment. A PVC is
    # writable and readable only from inside the cluster, so pushing a dataset
    # in and pulling results out are the same problem — a throwaway pod with
    # the volume mounted — and the fragment is parameterised by name and
    # access mode rather than written twice.
    "dataset-upload": "staging-pod",
    "collect-results": "staging-pod",
    "batch-run": "batch-job",
}


def fragment_for(resource: str) -> str:
    """The fragment file behind a `resources` entry. Identity unless aliased."""
    return RESOURCE_TO_FRAGMENT.get(resource, resource)


class CatalogError(RuntimeError):
    """A catalogue file is malformed. Raised at startup; never caught to continue.

    The message always names the file, and where possible the field.
    """


def _repo_root() -> Path:
    """Locate the repository root.

    Overridable so tests and a future container image do not have to sit at a
    fixed depth. Default walks up from studio/backend/app/ to hive-local/.
    """
    env = os.environ.get("HIVE_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def catalog_dir() -> Path:
    env = os.environ.get("STUDIO_CATALOG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root() / "templates" / "catalog"


# ---------------------------------------------------------------------------
# BUNDLED SAMPLE DATASETS
# ---------------------------------------------------------------------------
# A template whose `upload` parameter is optional promises a fallback: "If you
# leave this empty we use a small sample so you can see how it works"
# (batch-inference/template.yaml). That promise has two readers and they MUST
# agree — the executor, which copies the file into the project, and the wizard,
# which has to offer the PI that file's columns before there is any project at
# all. A `required` question about columns cannot be answered otherwise, and the
# advertised fallback would be unusable.
#
# So the choice lives HERE, once, and both sides call it. It used to be four
# lines inside RealExecutor._dataset_source, which meant the console had no way
# to ask the question the executor was about to answer.
#
# KEYED BY THE MODEL'S INTERFACE, not by its id: the two samples demonstrate
# different things — prose for a chat model to summarise, feature columns for a
# classifier — and that difference is a property of how the endpoint is TALKED
# TO, which is exactly what `interface` records in models.yaml. A third model
# with an existing interface needs no entry here.
SAMPLE_DIR_NAME = "sample-data"

SAMPLE_DATASETS: dict[str, str] = {
    "chat": "abstracts-200.csv",
    "predict-v1": "iris-5000.csv",
}


def sample_dataset(template_id: str, interface: str) -> Path | None:
    """The bundled dataset a run uses when the PI uploads nothing, or None.

    None is an ordinary answer, not an error: most templates ship no samples,
    and an interface with no sample (a future one, say) simply has no fallback —
    the caller decides what that means. The executor refuses the deployment; the
    wizard shows no columns and the PI uploads a file, which is what they would
    have had to do anyway.
    """
    name = SAMPLE_DATASETS.get(interface)
    if not name:
        return None
    candidate = catalog_dir() / template_id / SAMPLE_DIR_NAME / name
    return candidate if candidate.is_file() else None


def _rel(path: Path) -> str:
    """Path as written in the repo, for error messages."""
    try:
        return str(path.relative_to(_repo_root()))
    except ValueError:
        return str(path)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse one YAML file into a mapping, or raise CatalogError naming it."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"{_rel(path)}: cannot be read — {exc}") from exc

    try:
        # safe_load, not load: a catalogue file is data and must never be able to
        # construct arbitrary Python objects.
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        problem = getattr(exc, "problem", None) or str(exc)
        raise CatalogError(f"{_rel(path)}: invalid YAML{where} — {problem}") from exc

    if doc is None:
        raise CatalogError(f"{_rel(path)}: file is empty")
    if not isinstance(doc, dict):
        raise CatalogError(
            f"{_rel(path)}: expected a mapping at the top level, got {type(doc).__name__}"
        )
    return doc


def _field_path(loc: tuple[Any, ...]) -> str:
    """Render a Pydantic error location as a YAML-ish path a human can find.

    ('spec', 'parameters', 0, 'label')  ->  spec.parameters[0].label
    """
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def _validate_model(cls: type, doc: dict[str, Any], path: Path):
    """Run a Pydantic model, converting failures into a CatalogError.

    Pydantic's own multi-error output is accurate but reads like a stack trace.
    This reshapes it into one line per bad field, each naming the field.
    """
    try:
        return cls.model_validate(doc)
    except PydanticValidationError as exc:
        lines = []
        for err in exc.errors():
            field = _field_path(err["loc"])
            msg = err["msg"]
            # Name the offending value when it is small enough to be useful.
            got = err.get("input")
            shown = ""
            if got is not None and not isinstance(got, (dict, list)):
                shown = f" (got {got!r})"
            lines.append(f"    {field}: {msg}{shown}")
        raise CatalogError(
            f"{_rel(path)}: does not match the {cls.__name__} schema —\n"
            + "\n".join(lines)
        ) from exc


def _check_api_version(doc: dict[str, Any], path: Path, kind: str) -> None:
    api = doc.get("apiVersion")
    if api != CATALOG_API_VERSION:
        raise CatalogError(
            f"{_rel(path)}: apiVersion is {api!r}, expected {CATALOG_API_VERSION!r}"
        )
    if doc.get("kind") != kind:
        raise CatalogError(
            f"{_rel(path)}: kind is {doc.get('kind')!r}, expected {kind!r}"
        )


# ---------------------------------------------------------------------------
# PER-PARAMETER STRUCTURAL CHECKS
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DERIVED KEYS A sourceFilter MAY USE
# ---------------------------------------------------------------------------
# A filter normally names a models.yaml FIELD. These name a QUESTION that spans
# several of them, and they exist because the filter is exact-equality ANDed
# across keys — it cannot express "chat OR predict-v1".
#
# `served` is that question. batch-inference accepts any model something can
# actually serve, and excludes `embed` entries, which are libraries loaded
# in-process by the RAG toolkit with no runtime to run as a sidecar. Writing
# that as an interface list would need OR; writing it as `served: "True"` says
# what is meant and keeps working when a fourth interface is added.
#
# Mapped to the property rather than read by getattr alone so the set stays
# CLOSED: a template cannot filter on an arbitrary attribute of the model class.
DERIVED_FILTER_KEYS: dict[str, str] = {
    "served": "is_served",
}


def matching_models(models: list[ModelEntry], filters: dict[str, str] | None) -> list[ModelEntry]:
    """The catalogue entries an enum's `sourceFilter` selects.

    Exact string equality against the field of the same name, ALL keys must
    match, and an empty/absent filter selects everything — which is what keeps
    inference-endpoint's unfiltered `model` enum behaving exactly as before.

    Compared as strings so a filter can be written naturally in YAML
    (`dims: "384"` and `dims: 384` both work) without the template author having
    to know a field's Python type. `served: "True"` resolves through
    DERIVED_FILTER_KEYS above.
    """
    if not filters:
        return list(models)
    out = []
    for m in models:
        if all(
            str(getattr(m, DERIVED_FILTER_KEYS.get(key, key), None)) == str(value)
            for key, value in filters.items()
        ):
            out.append(m)
    return out


def _check_parameter(p: Parameter, path: Path, idx: int) -> None:
    """Type-appropriate sanity, for templates of any maturity."""
    where = f"spec.parameters[{idx}] ({p.name})"

    if p.sourceFilter is not None:
        if p.source is None:
            raise CatalogError(
                f"{_rel(path)}: {where} sets `sourceFilter` but has no `source` to "
                f"filter; a filter on an inline `values` list would do nothing"
            )
        # An unknown key would match no model, and an enum that silently offers
        # nothing is the failure this loader exists to make loud.
        allowed = set(ModelEntry.model_fields) | set(DERIVED_FILTER_KEYS)
        unknown = sorted(set(p.sourceFilter) - allowed)
        if unknown:
            raise CatalogError(
                f"{_rel(path)}: {where}.sourceFilter names {', '.join(unknown)}, "
                f"which {'are' if len(unknown) > 1 else 'is'} not "
                f"{'fields' if len(unknown) > 1 else 'a field'} of a models.yaml "
                f"entry (known: {', '.join(sorted(allowed))})"
            )

    if p.immutable and p.default is None and p.value is None:
        # An immutable parameter is answered by its default. Without one there is
        # no answer to render and nothing for the server to accept, so the wizard
        # would show a locked field with nothing in it.
        raise CatalogError(
            f"{_rel(path)}: {where} is `immutable` but has no `default`; an "
            f"immutable parameter is a decision already made and must say what it is"
        )

    if p.type == "string" and p.pattern is not None:
        try:
            re.compile(p.pattern)
        except re.error as exc:
            raise CatalogError(
                f"{_rel(path)}: {where}.pattern is not a valid regular "
                f"expression — {exc}"
            ) from exc

    if p.type == "integer":
        if p.minimum is not None and p.maximum is not None and p.minimum > p.maximum:
            raise CatalogError(
                f"{_rel(path)}: {where} has minimum {p.minimum} greater than "
                f"maximum {p.maximum}"
            )
        if p.default is not None:
            if not isinstance(p.default, int) or isinstance(p.default, bool):
                raise CatalogError(
                    f"{_rel(path)}: {where}.default must be an integer, "
                    f"got {p.default!r}"
                )
            if p.minimum is not None and p.default < p.minimum:
                raise CatalogError(
                    f"{_rel(path)}: {where}.default {p.default} is below "
                    f"minimum {p.minimum}"
                )
            if p.maximum is not None and p.default > p.maximum:
                raise CatalogError(
                    f"{_rel(path)}: {where}.default {p.default} is above "
                    f"maximum {p.maximum}"
                )

    if p.type == "upload":
        # An upload that does not say what it takes would fall back to whatever
        # the console's defaults happen to be — which is exactly the coupling
        # that made a dataset field advertise itself as Markdown.
        if not p.accept:
            raise CatalogError(
                f"{_rel(path)}: {where} is type 'upload' and must declare `accept`, "
                f"the file extensions this question takes (e.g. ['.csv', '.jsonl']). "
                f"Without it the console cannot tell a PI what to give it."
            )
        bad = [e for e in p.accept if not e.startswith(".") or e != e.lower()]
        if bad:
            raise CatalogError(
                f"{_rel(path)}: {where}.accept contains {', '.join(bad)} — each entry "
                f"must be a lowercase extension including the dot, because it is "
                f"compared against a lowercased filename suffix."
            )
        # An upload parameter's value is an id that becomes a path segment. A
        # template that declared one without a pattern would be relying on the
        # frontend to constrain it, and a client-side check is not a control
        # (invariant (c)). validation.py enforces the pattern; this makes sure
        # there is one to enforce.
        if not p.pattern:
            raise CatalogError(
                f"{_rel(path)}: {where} is type 'upload' and must declare a "
                f"`pattern`. Its value is joined onto tenants/_uploads/, so the "
                f"shape it may take has to be stated in the catalogue."
            )
        if p.required:
            # Requiring documents would make the template undemonstrable: the
            # pipeline falls back to the committed demo corpus precisely so a PI
            # can try it before they have anything of their own.
            raise CatalogError(
                f"{_rel(path)}: {where} is type 'upload' and cannot be `required` "
                f"— the pipeline indexes the bundled demo corpus when none is given."
            )

    if p.type == "fixed" and p.value is None:
        # A `fixed` parameter is not an input — it is a decision already made. With
        # no value there is nothing to render and nothing to submit.
        raise CatalogError(
            f"{_rel(path)}: {where} is type 'fixed' but has no `value`"
        )

    if p.type == "enum" and p.values is not None and p.source is not None:
        raise CatalogError(
            f"{_rel(path)}: {where} sets both `values` and `source`; "
            f"an enum is filled from exactly one of them"
        )

    if p.type == "boolean" and p.default is not None and not isinstance(p.default, bool):
        raise CatalogError(
            f"{_rel(path)}: {where}.default must be true or false, got {p.default!r}"
        )


# ---------------------------------------------------------------------------
# REFERENTIAL CHECKS — maturity: available ONLY
# ---------------------------------------------------------------------------


def _check_available_template(
    tpl: UseCaseTemplate, path: Path, models: list[ModelEntry]
) -> None:
    """The harder look a template gets once it claims to actually provision."""
    rel = _rel(path)

    if not tpl.spec.resources:
        raise CatalogError(
            f"{rel}: maturity is 'available' but spec.resources is missing or empty "
            f"— an available template must say what it provisions"
        )
    if not tpl.spec.outputs:
        raise CatalogError(
            f"{rel}: maturity is 'available' but spec.outputs is missing or empty "
            f"— an available template must say what the PI is handed"
        )
    if tpl.spec.lifecycle is None:
        raise CatalogError(
            f"{rel}: maturity is 'available' but spec.lifecycle is missing — every "
            f"provisioned project needs a clock, whether it is an expiry "
            f"(kind: service) or a retention (kind: run)"
        )

    # Every enum must offer the PI something to pick.
    for idx, p in enumerate(tpl.spec.parameters):
        if p.type != "enum":
            continue
        where = f"spec.parameters[{idx}] ({p.name})"
        if p.source is not None:
            if p.source != "models.yaml":
                raise CatalogError(
                    f"{rel}: {where}.source is {p.source!r}; the only supported "
                    f"source is 'models.yaml'"
                )
            if not models:
                raise CatalogError(
                    f"{rel}: {where} resolves from models.yaml, which has no entries"
                )
            # THE FILTER MUST SELECT SOMETHING. A filter that matches nothing
            # produces a 200 response, an empty dropdown, and a PI who cannot
            # proceed with no idea why — the exact failure mode described in this
            # module's header, one level deeper.
            if p.sourceFilter is not None and not matching_models(models, p.sourceFilter):
                shown = ", ".join(f"{k}={v}" for k, v in sorted(p.sourceFilter.items()))
                raise CatalogError(
                    f"{rel}: {where}.sourceFilter ({shown}) matches no entry in "
                    f"models.yaml, so this enum would offer the PI nothing"
                )
        elif not p.values:
            raise CatalogError(
                f"{rel}: {where} is an enum on an available template but has "
                f"neither inline `values` nor a `source` to resolve from"
            )

    # A default that is not one of the options is worse than none: the wizard
    # pre-selects it, the PI submits without touching the field, and the server
    # rejects an answer it supplied itself.
    for idx, p in enumerate(tpl.spec.parameters):
        if p.type != "enum" or p.default is None:
            continue
        if p.source is not None:
            allowed = [m.id for m in matching_models(models, p.sourceFilter)]
        else:
            allowed = [v.value for v in (p.values or [])]
        if p.default not in allowed:
            raise CatalogError(
                f"{rel}: spec.parameters[{idx}] ({p.name}).default is "
                f"{p.default!r}, which is not one of its options "
                f"({', '.join(allowed) or '<none>'})"
            )

    # The lifetime clock must be driven by a parameter that exists. `fine-tune`
    # gets this wrong today (it references `durationDays` without declaring it),
    # which is exactly why the check is scoped to `available` — and exactly why
    # it must fire the moment one is promoted. It did: rag-stack had the same
    # defect as a sketch, and promoting it is what forced the parameter to be
    # written.
    # ---------------------------------------------------------------------
    # THE CLOCKS, AND WHICH ONES THIS SHAPE OF TEMPLATE MUST DECLARE
    # ---------------------------------------------------------------------
    # A SERVICE is judged by how long the project lives, so it must name the
    # parameter driving `hive.ksl/expiry`. A RUN is judged by how long the work
    # may take and how long its results survive, so it must name those two
    # instead — and must NOT name a ttl, because a run's project lifetime is not
    # a thing the PI is choosing.
    #
    # Checked here rather than in the Pydantic model for the reason the whole
    # referential section exists: these are questions about whether a template
    # would WORK, and they only mean something once it claims to provision.
    declared = ", ".join(p.name for p in tpl.spec.parameters) or "<none>"
    lifecycle = tpl.spec.lifecycle

    def _must_name_a_parameter(field: str, value: str | None) -> None:
        if not value:
            raise CatalogError(
                f"{rel}: spec.lifecycle.kind is {lifecycle.kind!r} and must declare "
                f"{field}, naming one of this template's parameters ({declared})"
            )
        if tpl.parameter(value) is None:
            raise CatalogError(
                f"{rel}: spec.lifecycle.{field} is {value!r}, which is not one of "
                f"this template's parameters ({declared})"
            )

    if lifecycle.is_run:
        _must_name_a_parameter("runTimeoutMinutes", lifecycle.runTimeoutMinutes)
        _must_name_a_parameter("artifactRetentionDays", lifecycle.artifactRetentionDays)
        if lifecycle.ttl is not None:
            raise CatalogError(
                f"{rel}: spec.lifecycle.kind is 'run' but a `ttl` is declared. A run "
                f"finishes; its project lifetime is not a choice the PI makes. The "
                f"clock that matters is artifactRetentionDays, set at completion."
            )
    else:
        if lifecycle.ttl is None:
            raise CatalogError(
                f"{rel}: spec.lifecycle.kind is 'service' and must declare a `ttl` — "
                f"every provisioned service needs an expiry clock (CLAUDE.md §2.6)"
            )
        _must_name_a_parameter("ttl.fromParameter", lifecycle.ttl.fromParameter)
        for field in ("runTimeoutMinutes", "artifactRetentionDays"):
            if getattr(lifecycle, field) is not None:
                raise CatalogError(
                    f"{rel}: spec.lifecycle.kind is 'service' but sets {field}, "
                    f"which belongs to a run"
                )

    # teardownOrder speaks the canonical step vocabulary.
    for entry in tpl.spec.lifecycle.teardownOrder or []:
        if entry not in PIPELINE_STEPS:
            raise CatalogError(
                f"{rel}: spec.lifecycle.teardownOrder contains {entry!r}, which is "
                f"not a canonical pipeline step id ({', '.join(PIPELINE_STEPS)})"
            )

    # Every declared resource must have a fragment to render — except vcluster.
    # Resolved through fragment_for(), so a resource named for what the PI gets
    # can still point at a fragment named for what is in it.
    fragments = path.parent / "resources"
    for entry in tpl.spec.resources:
        if entry in FRAGMENTLESS_RESOURCES:
            continue
        fragment = fragment_for(entry)
        frag = fragments / f"{fragment}.yaml.tmpl"
        if not frag.is_file():
            alias = f" (aliased to {fragment!r})" if fragment != entry else ""
            raise CatalogError(
                f"{rel}: spec.resources names {entry!r}{alias} but {_rel(frag)} "
                f"does not exist. Every resource is rendered from a fragment, except "
                f"{sorted(FRAGMENTLESS_RESOURCES)} which the pipeline shells out for."
            )


# ---------------------------------------------------------------------------
# PER-INTERFACE MODEL CONTRACT
# ---------------------------------------------------------------------------


def _check_model_entry(m: ModelEntry, path: Path) -> None:
    """One models.yaml entry, checked against the contract for ITS interface.

    THE POINT OF THIS FUNCTION IS THAT `seedUrl` STAYS MANDATORY. Adding `embed`
    entries meant relaxing the schema — an in-process embedding model has no
    runtime, no modelFormat, no invokePath and no single URL — and a relaxation
    applied uniformly would have quietly made `seedUrl` optional for chat models
    too. `seedUrl` is the governance boundary: it is the only network egress the
    provisioning pipeline performs, and every possible value of it being in this
    file, in git, in a diff, is the entire claim ADR-11 makes.

    So the schema is permissive and this is strict, per interface.
    """
    where = f"models[{m.id}]"

    if m.is_served:
        for field in ("modelFormat", "seedUrl", "invokePath"):
            if not str(getattr(m, field) or "").strip():
                raise CatalogError(
                    f"{_rel(path)}: {where} has interface {m.interface!r} and must "
                    f"declare a non-empty {field}. (`runtime` may be empty — that "
                    f"means 'let KServe resolve one from modelFormat'.)"
                )
        if m.sampleInput is None:
            raise CatalogError(
                f"{_rel(path)}: {where} has interface {m.interface!r} and must "
                f"declare a sampleInput, so the playground has a known-good "
                f"request to show before the PI has typed anything"
            )
        for field in ("provider", "fastembedName", "dims", "distance"):
            if getattr(m, field) is not None:
                raise CatalogError(
                    f"{_rel(path)}: {where} has interface {m.interface!r} but sets "
                    f"{field}, which belongs only to an `embed` entry"
                )
        return

    # embed
    for field in ("provider", "fastembedName", "dims", "distance"):
        if getattr(m, field) is None:
            raise CatalogError(
                f"{_rel(path)}: {where} has interface 'embed' and must declare "
                f"{field}"
            )
    if m.dims is not None and m.dims <= 0:
        raise CatalogError(f"{_rel(path)}: {where}.dims must be positive, got {m.dims}")
    for field in ("runtime", "modelFormat", "seedUrl", "invokePath"):
        if str(getattr(m, field) or "").strip():
            raise CatalogError(
                f"{_rel(path)}: {where} has interface 'embed' but sets {field}. An "
                f"embedding model is loaded in-process by the RAG toolkit and "
                f"served by nothing, so it has no runtime and no URL of its own — "
                f"its bytes are fetched by the model-seed Job through its provider."
            )


# ---------------------------------------------------------------------------
# THE CATALOGUE
# ---------------------------------------------------------------------------


class Catalog:
    """Everything in templates/catalog/, loaded and checked.

    Held in memory for the process lifetime. The catalogue is committed data that
    changes by a git commit and a restart, not at runtime — so there is no reload
    endpoint and no cache invalidation to get wrong.
    """

    def __init__(self, templates: list[UseCaseTemplate], models: list[ModelEntry]):
        self.templates = templates
        self.models = models
        self._by_id = {t.metadata.id: t for t in templates}
        self._models_by_id = {m.id: m for m in models}

    # --- lookups ----------------------------------------------------------

    def template(self, template_id: str) -> UseCaseTemplate | None:
        return self._by_id.get(template_id)

    def model(self, model_id: str) -> ModelEntry | None:
        return self._models_by_id.get(model_id)

    def summaries(self) -> list[TemplateSummary]:
        """Catalogue cards.

        Ordered available-first, then alphabetically: what a PI can actually use
        comes before what they cannot, and the order does not wobble between
        restarts the way directory iteration would.
        """
        ordered = sorted(
            self.templates,
            key=lambda t: (t.metadata.maturity != "available", t.metadata.id),
        )
        return [
            TemplateSummary(
                id=t.metadata.id,
                name=t.metadata.displayName,
                category=t.metadata.category,
                maturity=t.metadata.maturity,
                description=t.metadata.description,
            )
            for t in ordered
        ]

    # --- enum resolution --------------------------------------------------

    def resolved_template(self, template_id: str) -> dict[str, Any] | None:
        """A template as the WIZARD needs it: every enum carrying real choices.

        `source: models.yaml` is replaced by the resolved `values`, so the frontend
        renders one uniform kind of enum and never learns that models.yaml exists.
        The `source` key is kept in the response for traceability — it says WHERE
        the options came from, which matters when someone asks why a model is
        missing (answer: it is not in the closed list).
        """
        tpl = self.template(template_id)
        if tpl is None:
            return None

        doc = tpl.model_dump(exclude_none=True)
        for param in doc["spec"]["parameters"]:
            if param.get("type") == "enum" and param.get("source") == "models.yaml":
                # `sourceFilter` is applied HERE, once, so that every consumer —
                # the wizard, validate_params, the smoke script — sees the same
                # closed list. A filter applied only in the UI would be a hint;
                # applied here it is the control, because validation runs against
                # this same resolved object (see resolved_template_model).
                param["values"] = [
                    EnumValue(value=m.id, label=m.displayName).model_dump()
                    for m in matching_models(self.models, param.get("sourceFilter"))
                ]
        return doc

    def resolved_template_model(self, template_id: str) -> UseCaseTemplate | None:
        """The same resolution, as a typed object rather than a dict.

        This is what `validation.validate_params` must be given. A RAW template's
        `model` parameter carries `source: models.yaml` and no `values` at all, so
        validating against it would reject every model id — the closed list would
        not be enforced, it would be empty. Resolving first means the validator
        checks submissions against the real, current governance boundary.
        """
        doc = self.resolved_template(template_id)
        if doc is None:
            return None
        return UseCaseTemplate.model_validate(doc)


def load_catalog(directory: Path | None = None) -> Catalog:
    """Load and validate the whole catalogue, or raise CatalogError.

    Called once from main.py's lifespan. Any failure propagates and the process
    does not come up.
    """
    root = directory or catalog_dir()
    if not root.is_dir():
        raise CatalogError(f"catalogue directory not found: {_rel(root)}")

    # --- models.yaml, first: templates resolve their enums against it -----
    models_path = root / "models.yaml"
    if not models_path.is_file():
        raise CatalogError(
            f"{_rel(models_path)} not found — it is the closed model list every "
            f"deploy resolves against and is not optional"
        )
    models_doc = _load_yaml(models_path)
    _check_api_version(models_doc, models_path, "ModelCatalog")
    model_catalog: ModelCatalog = _validate_model(ModelCatalog, models_doc, models_path)

    seen_models: set[str] = set()
    for m in model_catalog.models:
        if m.id in seen_models:
            # Ids are what registry history points at. A duplicate would make a
            # past deployment ambiguous about what it actually deployed.
            raise CatalogError(
                f"{_rel(models_path)}: duplicate model id {m.id!r}. Ids are "
                f"referenced by deployment history and must be unique."
            )
        seen_models.add(m.id)
        _check_model_entry(m, models_path)

    # --- every template ---------------------------------------------------
    templates: list[UseCaseTemplate] = []
    # Sorted so load order — and therefore which error surfaces first — is stable.
    for path in sorted(root.glob("*/template.yaml")):
        doc = _load_yaml(path)
        _check_api_version(doc, path, "UseCaseTemplate")
        tpl: UseCaseTemplate = _validate_model(UseCaseTemplate, doc, path)

        # The directory name is the id the URL /api/templates/{id} will carry.
        # Letting them drift produces a 404 for a template that visibly exists.
        expected = path.parent.name
        if tpl.metadata.id != expected:
            raise CatalogError(
                f"{_rel(path)}: metadata.id is {tpl.metadata.id!r} but the directory "
                f"is {expected!r}; they must match"
            )

        names: set[str] = set()
        for idx, p in enumerate(tpl.spec.parameters):
            if p.name in names:
                raise CatalogError(
                    f"{_rel(path)}: duplicate parameter name {p.name!r}"
                )
            names.add(p.name)
            _check_parameter(p, path, idx)

        if tpl.metadata.maturity == "available":
            _check_available_template(tpl, path, model_catalog.models)

        templates.append(tpl)

    if not templates:
        raise CatalogError(
            f"{_rel(root)}: no */template.yaml found — the catalogue is empty"
        )

    return Catalog(templates=templates, models=model_catalog.models)
