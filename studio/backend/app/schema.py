# ===========================================================================
# HIVE STUDIO BFF — SCHEMA
# ===========================================================================
# Pydantic models for the two kinds of file in templates/catalog/: the
# UseCaseTemplates and the curated ModelCatalog.
#
# THE CATALOGUE IS DATA, NOT CODE (CLAUDE.md §6). Nothing here hard-codes a
# template, a parameter or a model. This module only describes the SHAPE those
# files must have, so that a malformed one is rejected at startup with a message
# naming the file and the field rather than surfacing as an AttributeError three
# layers deep in the wizard.
#
# ---------------------------------------------------------------------------
# WHY extra="forbid" EVERYWHERE
# ---------------------------------------------------------------------------
# A silently-ignored key is the worst failure mode for a config file. If someone
# writes `reservedPrefix:` instead of `reservedPrefixes:`, a permissive loader
# accepts it, the reserved-prefix rule quietly stops being enforced, and CLAUDE.md
# invariant (c) is broken with nothing in the logs. Forbidding unknown fields turns
# that typo into a startup failure that names the field.
#
# ---------------------------------------------------------------------------
# WHY VALIDATION IS TIERED BY maturity
# ---------------------------------------------------------------------------
# `inference-endpoint` and `rag-stack` are `available`; `fine-tune` is declared
# `coming-soon` and is explicitly a sketch — its enums have no values, it has no
# `resources` or `outputs`, and its `lifecycle.ttl.fromParameter` points at a
# `durationDays` parameter it does not define. Those are not defects, they are the
# point of the maturity flag.
#
# rag-stack WAS such a sketch and was promoted (studio R1), which is the exercise
# this split was written for: the moment `maturity` flipped, every one of those
# three defects became a startup error, and each had to be answered with a real
# decision rather than a placeholder.
#
# So there are two levels of strictness, and the split is deliberate:
#
#   STRUCTURE  — checked for every template regardless of maturity. Field names,
#                types, and enum-of-valid-values. A sketch must still be a
#                well-formed sketch.
#   REFERENTIAL — checked ONLY for maturity: available. Do the enums resolve? Does
#                ttl.fromParameter name a real parameter? Are resources and outputs
#                present? These are the questions that only mean something for a
#                template that actually provisions, and they live in catalog.py
#                where the cross-file context (models.yaml) exists.
#
# A template promoted from coming-soon to available therefore gets a much harder
# look, at the moment that promotion happens, which is exactly when it is wanted.
# ===========================================================================

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# The API version every catalogue file declares. Checked rather than ignored: a
# future v2 with a different shape must not be silently parsed as a v1.
CATALOG_API_VERSION = "studio.hive.ksl/v1"


class _Strict(BaseModel):
    """Base for every catalogue model: unknown fields are an error, not noise."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# USE-CASE TEMPLATE
# ---------------------------------------------------------------------------

# The six types the committed catalogue actually uses. The task specification named
# three (string, enum, integer); the other three appear in the catalogue's own
# data — `fixed` and `boolean` and `file` in fine-tune (method, deployAfter,
# datasetUpload). Supporting only three would refuse to start on this
# repository's own files, so all six are modelled.
#
# `fixed` LOST A USER WHEN rag-stack WAS PROMOTED, and the reason is worth
# keeping: its `vectorDb` was a `fixed` string as a sketch and is now an
# `immutable` enum. `fixed` says "this is not a question". `immutable` says
# "this is a question with one acceptable answer right now" — so when a second
# vector database is supported it becomes a live dropdown by deleting one line,
# rather than by changing its type and everything that reads it.
#
#   string   free text, constrained by `pattern`
#   enum     one of `values`, or of a list resolved from `source`
#   integer  bounded by `minimum` / `maximum`
#   boolean  a checkbox
#   fixed    NOT an input — a decision the platform has already made, rendered as
#            read-only. `value` is the answer; the PI cannot change it.
#   multiselect  SEVERAL of a set, in order. The value is a LIST of strings.
#            Distinct from `enum`, which picks exactly one, and the order is
#            part of the answer rather than incidental: batch-inference uses it
#            to name which dataset columns feed the model, and for a classic
#            model those columns ARE a feature vector, so `[petal_length,
#            sepal_length]` is a different question from `[sepal_length,
#            petal_length]`.
#
#            Like `enum` it can be filled from inline `values` or left empty for
#            the console to fill at runtime — the wizard populates it from the
#            columns the upload preview detected, which is why a template can
#            declare it with no values at all.
#   upload   a set of documents, staged BEFORE the deploy request and referred to
#            by the id POST /api/uploads returns. The submitted value is that id
#            — a short string — not the bytes, so the deploy request stays JSON
#            that can be logged, stored as intent and shown back on the review
#            screen. Distinct from `file` for exactly that reason: `file` means
#            "the form carries the bytes", which nothing supports.
#   file     an upload the form would carry inline. Nothing consumes one; only
#            fine-tune declares it, and validation.py refuses it outright.
ParameterType = Literal[
    "string", "enum", "integer", "boolean", "fixed", "file", "upload", "multiselect"
]


class EnumValue(_Strict):
    """One selectable option: the stored value, what the PI reads, and why.

    `description` is what makes an enum renderable as CHOICE CARDS rather than a
    dropdown — a sentence about the CONSEQUENCE of picking this option. It exists
    because the wizard used to carry that sentence in a ternary on the option's
    value ("always-on" ? this : that), which meant the console held copy about a
    trade-off the catalogue defines, and a second template with the same
    parameter would have inherited the first one's wording.

    Absent means "render this option as a plain choice", which is right for
    anything whose label already says everything (a model name, a size).
    """

    value: str
    label: str
    description: str | None = None


class Parameter(_Strict):
    """One question in the deploy wizard.

    The frontend renders the whole form from a list of these and knows nothing
    about namespaces, quotas or KServe (CLAUDE.md §6). Adding a question is a
    change to a YAML file, not to two codebases.
    """

    name: str
    type: ParameterType
    label: str
    help: str | None = None
    required: bool = False

    # WHICH SCREEN OF THE WIZARD THIS QUESTION BELONGS ON: "basics" or
    # "resources". (The third screen, Review, asks nothing.)
    #
    # DECLARED HERE RATHER THAN DECIDED IN THE FRONTEND, and that is the whole
    # point of the field. The wizard used to hard-code which five parameter NAMES
    # went on which screen, which meant a template with different questions —
    # rag-stack has nine — either rendered nothing or needed a second branch in
    # the React code. A template that adds a question now says where it goes, and
    # the console renders it without being edited.
    #
    # Absent means "resources": a question the platform does not classify is
    # about what the project runs, not about what it is called.
    group: Literal["basics", "resources"] = "resources"

    # --- type-specific ----------------------------------------------------
    # Deliberately all optional and validated per-type in catalog.py, rather than
    # modelled as a discriminated union. A union would be more precise Pydantic,
    # but it produces error messages about "which branch failed to match", and the
    # requirement here is a message naming the FIELD a human wrote wrongly.

    # string
    pattern: str | None = None

    # integer
    minimum: int | None = None
    maximum: int | None = None

    # enum — `values` is the inline list; `source` names a catalogue file to
    # resolve from instead (today only "models.yaml"). Exactly one is expected on
    # an `available` template; a `coming-soon` sketch may legitimately have neither.
    values: list[EnumValue] | None = None
    source: str | None = None
    # Narrows a `source` to the entries whose fields all match. Written as data
    # rather than as a rule in Python because the split it expresses is a
    # CATALOGUE fact, not a code fact: rag-stack asks two questions of one file —
    # "which model answers?" (interface: chat) and "which model indexes?"
    # (interface: embed) — and a hard-coded filter in the resolver would mean the
    # backend, not the catalogue, decides what a template may offer.
    #
    # Matching is exact equality against the ModelEntry field of the same name,
    # every key must match, and an unknown key is a startup error rather than a
    # filter that silently matches nothing (app/catalog.py).
    sourceFilter: dict[str, str] | None = None

    # fixed
    value: Any = None

    # any type
    default: Any = None

    # A DECISION ALREADY MADE, shown rather than asked. Distinct from `fixed`,
    # which is not an input at all: an `immutable` parameter is a real, typed,
    # validated parameter that happens to have exactly one acceptable answer
    # today, so the UI renders it read-only and the server refuses anything but
    # the default (app/validation.py).
    #
    # rag-stack's `vectorDb` is the case it exists for. It is an ENUM with one
    # value, not a `fixed` string, because the platform expects to support a
    # second vector database eventually — and when that happens the parameter
    # becomes a live dropdown by deleting one line here, rather than by changing
    # its type and every place that reads it.
    immutable: bool = False

    # --- upload only ------------------------------------------------------
    # WHAT THIS PARTICULAR UPLOAD WILL TAKE. Declared per parameter because the
    # two templates that ask for a file want completely different things:
    #
    #   rag-stack        prose to chunk and embed   .md/.txt, many files, 5MB
    #   batch-inference  records to stream          .csv/.jsonl, ONE file, 200MB
    #
    # These used to be constants inside the drop-zone component, which meant the
    # first template to exist silently set the rules for the second: the file
    # picker would not even SHOW a .csv, and the copy underneath promised
    # "Markdown or plain text" to somebody uploading a dataset.
    #
    # The endpoint accepts the UNION of every template's types, because a PI
    # uploads before the deploy request that names the template exists. These
    # narrow it to what this question is actually asking for — in the picker, in
    # the copy, and in validate_params, which is the half that is a control
    # rather than a hint.
    accept: list[str] | None = None
    maxFiles: int | None = None
    maxBytes: int | None = None

    # --- projectName only, and enforced SERVER-SIDE -----------------------
    # CLAUDE.md invariant (c). These live in the template so the rule is visible in
    # the catalogue, but the enforcement is in validation.py. The wizard's regex is
    # a hint; a client-side check is not a control.
    reservedNames: list[str] | None = None
    reservedPrefixes: list[str] | None = None


class TemplateMetadata(_Strict):
    id: str
    displayName: str
    category: str
    # `available` means this template really provisions. The UI must not let a PI
    # start anything else (CLAUDE.md §6).
    maturity: Literal["available", "coming-soon"]
    description: str


class TTL(_Strict):
    """Where the lifetime clock comes from.

    `fromParameter` names the parameter whose value is added to provisioning time
    to produce the RFC3339 UTC instant written as `hive.ksl/expiry` on the
    namespace. Studio only SETS that annotation; the host reconciler CronJob is
    what honours it (CLAUDE.md §2.6).
    """

    fromParameter: str


class Lifecycle(_Strict):
    # ---------------------------------------------------------------------
    # WHAT SHAPE OF THING THIS TEMPLATE PRODUCES
    # ---------------------------------------------------------------------
    # `service` — the default, and what every template did before batch existed.
    #   It provisions something that STAYS UP: an endpoint with replicas, a
    #   readiness state, a sleep/wake cycle, and a lifetime measured in days by
    #   `hive.ksl/expiry`. Success is "ready".
    #
    # `run` — it provisions something that FINISHES. There is no endpoint, no
    #   replicas, nothing to wake. Success is "completed", failure is a non-zero
    #   exit, and the lifetime that matters is not the project's but the
    #   ARTIFACT's — how long the results stay downloadable after the work is
    #   done.
    #
    # The distinction is not cosmetic and it is not a UI hint. It changes the
    # terminal state the pipeline writes, what the project page can truthfully
    # show (a run has no "sleeping"), and which clock is being counted down.
    # Declaring it in the catalogue keeps that decision where the rest of the
    # template's shape lives.
    kind: Literal["service", "run"] = "service"

    # SERVICES ONLY, and required for them — see the validation in catalog.py.
    # A run has no `ttl.fromParameter` because the project's own lifetime is not
    # the interesting clock: the work is over in minutes.
    ttl: TTL | None = None

    # RUNS ONLY. Both name a parameter, the same indirection `ttl.fromParameter`
    # uses, so the NUMBER stays a question the PI answers rather than a constant
    # buried in a template.
    #
    #   runTimeoutMinutes     -> the Job's activeDeadlineSeconds. A run that
    #                            overruns is killed by Kubernetes, not by Studio,
    #                            so the ceiling is enforced where the work is.
    #   artifactRetentionDays -> how long the results stay downloadable. Written
    #                            as `hive.ksl/expiry` on the namespace at
    #                            COMPLETION time, which is why a run sets that
    #                            annotation twice: once at provisioning as a
    #                            safety net, and once when it knows what it
    #                            produced.
    runTimeoutMinutes: str | None = None
    artifactRetentionDays: str | None = None

    # Reverse dependency order for teardown. Every entry must be one of the
    # canonical pipeline step ids — checked in catalog.py, because those strings
    # are one vocabulary shared by four places and a misspelling here would
    # strand a resource at teardown.
    teardownOrder: list[str] | None = None

    @property
    def is_run(self) -> bool:
        return self.kind == "run"


class TemplateSpec(_Strict):
    parameters: list[Parameter]
    # Absent on the coming-soon sketches, which provision nothing.
    resources: list[str] | None = None
    outputs: list[str] | None = None
    lifecycle: Lifecycle | None = None
    # Which ceiling this template's namespace-pack writes, when the tier's own
    # numbers are not the right ones. DECLARED, NOT COMPUTED: the quota is a
    # governance artefact and the whole set of ceilings the platform hands out
    # should be readable in the fragments and visible in a diff, so this names a
    # profile that a fragment spells out literally rather than carrying numbers
    # the renderer would assemble.
    #
    # rag-stack declares `rag-small`, which differs from the plain tier in
    # exactly two numbers, for exactly one reason: a RAG project has a third PVC
    # (Qdrant's) that an inference project does not.
    quotaProfile: str | None = None

    # HOW THIS TEMPLATE'S ENDPOINT IS TALKED TO, when the answer is a property of
    # the TEMPLATE rather than of the model.
    #
    # Usually it is the model's: an inference-endpoint project IS its model, so
    # `interface` there is whatever models.yaml says — chat for an LLM, predict-v1
    # for sklearn — and this field is absent so the model decides.
    #
    # A RAG project is different, and the difference is real rather than
    # cosmetic: the endpoint a PI calls is NOT the model. It is the retrieval
    # service in front of it, which takes a question and returns an answer with
    # the passages it used. The chat model underneath still has
    # `interface: chat`, and honouring that would give the PI a chat box wired
    # to a service that does not speak chat.
    #
    # So a template may override, and rag-stack declares `rag`. Absent means
    # "ask the model", which is what every existing template relies on.
    interface: Literal["chat", "predict-v1", "rag"] | None = None


class UseCaseTemplate(_Strict):
    apiVersion: str
    kind: Literal["UseCaseTemplate"]
    metadata: TemplateMetadata
    spec: TemplateSpec

    @property
    def id(self) -> str:
        return self.metadata.id

    def parameter(self, name: str) -> Parameter | None:
        return next((p for p in self.spec.parameters if p.name == name), None)


# ---------------------------------------------------------------------------
# MODEL CATALOGUE
# ---------------------------------------------------------------------------
# templates/catalog/models.yaml IS the governance boundary: a CLOSED list. The
# wizard's `model` enum is sourced from it and a PI cannot type a URL of their own,
# so `seedUrl` — the pipeline's only network egress — can only ever be a value that
# is in this file, in git, in a diff.


# The three interfaces, and the split that matters is not chat-vs-predict but
# SERVED-vs-LIBRARY. `chat` and `predict-v1` are both endpoints: something in the
# cluster serves them, they need a runtime, a modelFormat and a URL to fetch.
# `embed` is a model loaded IN-PROCESS by the RAG toolkit and served by nothing,
# so the whole serving half of the field contract is meaningless for it.
SERVED_INTERFACES: frozenset[str] = frozenset({"chat", "predict-v1"})


class ModelEntry(_Strict):
    """One catalogue model. Field contract documented in models.yaml itself.

    TWO SHAPES IN ONE MODEL, split by `interface`. Rather than a discriminated
    union — which would produce Pydantic errors about which branch failed to
    match — the serving fields default to empty and app/catalog.py enforces the
    per-interface contract, so a mistake names the field a human wrote wrongly.
    That check is not cosmetic: `seedUrl` is the governance boundary, and letting
    a `chat` entry omit it because an `embed` entry may is exactly the kind of
    quiet widening this file exists to prevent.
    """

    id: str
    displayName: str
    # How the model is USED, which is what the caller needs in order to render or
    # invoke it: OpenAI-compatible chat, KServe V1 predict, or an in-process
    # embedding model that is never served at all.
    interface: Literal["chat", "predict-v1", "embed"]

    # --- served models (chat, predict-v1) ---------------------------------
    # The ClusterServingRuntime to pin, or "" to let KServe resolve one from
    # modelFormat alone. EMPTY STRING IS MEANINGFUL, not missing — sklearn-iris
    # uses it deliberately so the platform can rename its runtime without breaking
    # the catalogue. Hence no `min_length`.
    runtime: str = ""
    modelFormat: str = ""
    seedUrl: str = ""
    invokePath: str = ""
    sampleInput: Any = None
    # What a classifier's outputs MEAN, keyed by the output as the model returns
    # it (as a string, since YAML and JSON keys are strings). Optional and
    # display-only: a batch run's results table shows `2 · virginica` rather than
    # a bare `2`, which on its own tells a PI nothing about their data. Lives in
    # the catalogue because it is a fact about the MODEL, reviewed with it.
    outputLabels: dict[str, str] | None = None

    # --- both --------------------------------------------------------------
    # Directory UNDER the models PVC root. "" means the claim ROOT. Joined, never
    # concatenated, into a storageUri — see models.yaml for why the difference
    # matters ("pvc://models" works; "pvc://models/" is not the proven form).
    # An embed entry uses it too, as the directory its ONNX cache is seeded into.
    storageSubpath: str

    # --- embed only --------------------------------------------------------
    # WHO loads it, rather than what serves it. Recorded because it says the
    # model runs inside a container this repository builds rather than in a
    # runtime the platform installed — a different trust story, worth naming.
    provider: str | None = None
    # The exact id from the provider's own registry. Verified against
    # fastembed 0.8.0's supported-models list, never spelled from memory.
    fastembedName: str | None = None
    # Vector width and Qdrant's similarity metric. Both go straight into the
    # create-collection request, so a wrong value here is only discovered when
    # the first upsert of an already-read corpus is rejected.
    dims: int | None = None
    distance: Literal["Cosine", "Dot", "Euclid"] | None = None

    @property
    def is_served(self) -> bool:
        """Does anything in the cluster serve this model?

        The question every consumer actually has. `interface != "embed"` would
        answer it today and would quietly become wrong the moment a fourth
        interface is added.
        """
        return self.interface in SERVED_INTERFACES

    def storage_uri(self, claim: str = "models") -> str:
        """Join claim and subpath into a KServe pvc:// URI.

        Kept here, next to the field it depends on, so there is exactly one
        implementation of the join and no caller is tempted to concatenate.
        """
        sub = self.storageSubpath.strip("/")
        return f"pvc://{claim}/{sub}" if sub else f"pvc://{claim}"


class ModelCatalogMetadata(_Strict):
    name: str
    description: str | None = None


class ModelCatalog(_Strict):
    apiVersion: str
    kind: Literal["ModelCatalog"]
    metadata: ModelCatalogMetadata
    models: list[ModelEntry]


# ---------------------------------------------------------------------------
# API RESPONSE MODELS
# ---------------------------------------------------------------------------
# What the HTTP layer returns, kept separate from what the catalogue files
# contain. The wire shape is allowed to differ from the file shape, and does:
# `displayName` becomes `name`, and enum `source` is resolved into real `values`
# so the frontend never has to know that models.yaml exists.


class TemplateSummary(BaseModel):
    """A catalogue card."""

    id: str
    name: str
    category: str
    maturity: str
    description: str


class Health(BaseModel):
    ok: bool
    # Present only in real mode: the state of the single Kourier port-forward the
    # playground invokes through. None in mock mode, where there is nothing to
    # forward to.
    dataPath: dict | None = None
    # Which executor the pipeline will use. `mock` is the default ON PURPOSE: the
    # dangerous direction is provisioning by accident, so the safe mode is the one
    # you get by forgetting to set anything (CLAUDE.md §6). The UI shows this.
    executor: str


class ValidationError(BaseModel):
    """One rejected field. Shaped for direct display next to the input."""

    field: str
    message: str
