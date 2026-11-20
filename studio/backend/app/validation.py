# ===========================================================================
# HIVE STUDIO BFF — INPUT VALIDATION
# ===========================================================================
# `validate_params(template, params)` -> a list of {field, message}. Empty means
# the submission is acceptable.
#
# Used by the `approve` step, which is a REAL pipeline step precisely so that a
# rejection appears in history as a rejection rather than as a deployment that
# never started (CLAUDE.md §6).
#
# ---------------------------------------------------------------------------
# THIS IS THE CONTROL. THE WIZARD IS A HINT.
# ---------------------------------------------------------------------------
# CLAUDE.md invariant (c) is explicit that reserved names are "enforced server-side
# at the `approve` step, not by the wizard's regex — a client-side check is a hint,
# not a control." Everything in this module is written on that assumption: the
# submission is untrusted, the frontend may be out of date, bypassed, or replaced
# by curl, and nothing here may rely on a check having already happened.
#
# It returns a LIST rather than raising on the first problem. A PI who has answered
# five questions wrongly should learn all five at once, next to the fields
# concerned, instead of playing whack-a-mole one submit at a time.
# ===========================================================================

from __future__ import annotations

import re
from typing import Any, Callable

from .schema import Parameter, UseCaseTemplate

# The parameter that names the project. It becomes the host namespace
# (p-<name>), the vCluster name, the InferenceService name and the directory
# under tenants/ — which is why it carries rules no other parameter does.
PROJECT_NAME_PARAM = "projectName"

# ---------------------------------------------------------------------------
# INVARIANT (c), VERBATIM
# ---------------------------------------------------------------------------
# "Reserved project names are rejected. Exactly: inf-101, inf-103, default, and
#  any name starting kube- or hive-."
#
# These constants are the CONTROL. `inference-endpoint/template.yaml` also declares
# `reservedNames` and `reservedPrefixes`, and today the two agree — but the
# template's copy is there so the rule is visible in the catalogue, and a template
# is data that anyone can edit. The two are therefore UNIONED, never substituted: a
# template may reserve MORE names, and can never un-reserve these.
#
# inf-101 and inf-103 are on the list for a stronger reason than tidiness. They are
# the two hand-built reference tenants the whole rehearsal is measured against, and
# invariant (d) forbids Studio from writing to them at all. Letting a PI claim one
# of those names would point the pipeline straight at p-inf-101.
RESERVED_NAMES: frozenset[str] = frozenset({"inf-101", "inf-103", "default"})
RESERVED_PREFIXES: tuple[str, ...] = ("kube-", "hive-")


def _stub_namespace_exists(_project: str) -> bool:
    """Placeholder for the real cluster check, which arrives in S5.

    ALWAYS RETURNS False — "no collision" — so the pipeline is not blocked while
    the executor does not exist yet. That makes this check a genuine no-op today,
    and pretending otherwise would be worse than saying so:

      * a `p-<project>` namespace created outside Studio will NOT be caught here;
      * two people racing the same project name will both pass this check.

    Two real backstops exist in the meantime, which is why a permissive stub is
    tolerable rather than dangerous. The registry's PRIMARY KEY on
    `deployments.id` refuses a duplicate project outright, and the cluster itself
    returns AlreadyExists when `namespace-pack` is applied. Both fail later and
    with a worse message than this check would give, which is the entire reason
    S5 replaces it.
    """
    return False


def validate_params(
    template: UseCaseTemplate,
    params: dict[str, Any],
    namespace_exists: Callable[[str], bool] = _stub_namespace_exists,
) -> list[dict[str, str]]:
    """Check a wizard submission against its template.

    Args:
        template: the template being deployed — and it must be a RESOLVED one,
            from `Catalog.resolved_template_model(id)`, not the raw object off
            `Catalog.template(id)`. In the file, the `model` parameter is
            `source: models.yaml` with no inline `values`; validating against the
            raw template would therefore reject every model id, and the closed
            list would be enforced as empty rather than as itself.
        params:   the PI's raw answers, as submitted.
        namespace_exists: injected so the "does p-<project> already exist?" check
            can be a real cluster lookup in S5 and a stub today, without this
            module importing a Kubernetes client or this function becoming
            untestable offline. Called at most once, and only when the project
            name is otherwise valid — there is no point asking the cluster about
            a name that could never be used.

    Returns:
        A list of {"field", "message"} dicts, in template parameter order so the
        UI can show them beside the inputs. Empty means acceptable.
    """
    errors: list[dict[str, str]] = []

    def fail(field: str, message: str) -> None:
        errors.append({"field": field, "message": message})

    declared = {p.name for p in template.spec.parameters}

    # --- fields that are not in the template ------------------------------
    # Rejected rather than ignored. An unexpected field means the client and the
    # catalogue disagree about what this template is, and silently dropping it
    # would provision something other than what was submitted.
    for name in sorted(set(params) - declared):
        fail(name, f"'{name}' is not a parameter of template '{template.metadata.id}'")

    for param in template.spec.parameters:
        present = param.name in params
        value = params.get(param.name)

        # --- required / missing -------------------------------------------
        if not present or value is None or (isinstance(value, str) and not value.strip()):
            if param.type == "fixed":
                # Not an input. The PI is not expected to submit one, and the
                # renderer takes the value from the template.
                continue
            if param.required and param.default is None:
                fail(param.name, f"{param.label} is required")
            continue

        _check_value(param, value, fail)

        # --- immutable: a decision already made ---------------------------
        # Checked AFTER the type check, so a wrong-typed answer to an immutable
        # parameter gets the message about its type first.
        #
        # THE WIZARD RENDERS THIS READ-ONLY AND THAT IS A HINT, NOT A CONTROL —
        # the same reasoning as the reserved-name rule in invariant (c). A
        # disabled input is a suggestion to a browser; this is the refusal.
        if param.immutable and value != param.default:
            fail(
                param.name,
                f"{param.label} is fixed at '{param.default}' on this platform "
                f"and cannot be changed.",
            )

    # --- project name: the rules no other parameter has -------------------
    project_param = template.parameter(PROJECT_NAME_PARAM)
    if project_param is not None:
        raw = params.get(PROJECT_NAME_PARAM)
        if isinstance(raw, str) and raw.strip():
            name = raw.strip()
            # Only run the reserved and collision checks if the name is otherwise
            # well-formed. Telling someone their name is both malformed AND
            # reserved is noise; the first message is the one that matters.
            already_flagged = any(e["field"] == PROJECT_NAME_PARAM for e in errors)
            if not already_flagged:
                _check_project_name(name, project_param, namespace_exists, fail)

    return errors


def _check_value(param: Parameter, value: Any, fail: Callable[[str, str], None]) -> None:
    """Type-appropriate checks for one supplied answer."""
    if param.type == "string":
        if not isinstance(value, str):
            fail(param.name, f"{param.label} must be text")
            return
        if param.pattern and not re.fullmatch(param.pattern, value):
            # The pattern is shown because these are developer-facing PIs and a
            # bare "invalid" leaves them guessing. The human-readable rule lives
            # in the template's `help`, which the UI renders alongside.
            fail(
                param.name,
                f"{param.label} does not match the required format {param.pattern}",
            )

    elif param.type == "integer":
        # bool is a subclass of int in Python; True must not pass as 1 here.
        if isinstance(value, bool) or not isinstance(value, int):
            fail(param.name, f"{param.label} must be a whole number")
            return
        if param.minimum is not None and value < param.minimum:
            fail(param.name, f"{param.label} must be at least {param.minimum}")
        if param.maximum is not None and value > param.maximum:
            fail(param.name, f"{param.label} must be at most {param.maximum}")

    elif param.type == "boolean":
        if not isinstance(value, bool):
            fail(param.name, f"{param.label} must be true or false")

    elif param.type == "enum":
        # `values` is what the RESOLVED template carries (catalog.resolved_template
        # fills it in for `source: models.yaml`). An unresolved enum means the
        # caller passed a raw template — refuse rather than accept anything, since
        # for the `model` parameter that closed list IS the governance boundary.
        if not param.values:
            fail(
                param.name,
                f"{param.label} has no available options; the template's choices "
                f"could not be resolved",
            )
            return
        allowed = [v.value for v in param.values]
        if value not in allowed:
            fail(param.name, f"{param.label} must be one of: {', '.join(allowed)}")

    elif param.type == "fixed":
        # Submitting a value for a fixed parameter is only an error if it differs
        # from the decision already made. A client that echoes the template back
        # unchanged is fine; a client trying to change it is not.
        if value != param.value:
            fail(
                param.name,
                f"{param.label} is fixed at '{param.value}' and cannot be changed",
            )

    elif param.type == "multiselect":
        # A LIST OF STRINGS, and the order is meaningful — see ParameterType.
        if not isinstance(value, list):
            fail(param.name, f"{param.label} must be a list of names")
            return
        if not all(isinstance(v, str) for v in value):
            fail(param.name, f"{param.label} must be a list of names")
            return
        if param.required and not value:
            fail(param.name, f"{param.label} is required")
            return
        # DUPLICATES ARE REFUSED rather than de-duplicated. For a feature vector
        # the same column twice is almost certainly a mis-click, and silently
        # collapsing it would change the width of every instance sent to the
        # model without saying so.
        if len(set(value)) != len(value):
            duplicated = sorted({v for v in value if value.count(v) > 1})
            fail(param.name, f"{param.label}: {', '.join(duplicated)} is listed twice")
            return
        if param.pattern:
            bad = [v for v in value if not re.fullmatch(param.pattern, v)]
            if bad:
                fail(param.name, f"{param.label}: {', '.join(bad[:3])} is not a valid name")
        # When the template supplies a closed list, membership is enforced. When
        # it does not — as batch-inference does, because the columns come from
        # the PI's own file — anything well-formed is allowed here and the DRIVER
        # is what refuses a column the dataset does not have. That check has to
        # be there anyway: the file is the only authority on its own columns.
        if param.values:
            allowed = {v.value for v in param.values}
            unknown = [v for v in value if v not in allowed]
            if unknown:
                fail(param.name, f"{param.label}: {', '.join(unknown)} is not one of the options")

    elif param.type == "upload":
        # THE VALUE IS AN ID, NOT A FILE. The wizard uploads to POST /api/uploads
        # first and submits the id it got back, so what arrives here is a short
        # server-minted string.
        #
        # It is checked as a NAME because it is about to be joined onto
        # tenants/_uploads/ by the pipeline's corpus-upload step: `../../etc`
        # must never survive. The template's own `pattern` carries the rule so it
        # is visible in the catalogue, and the executor resolves the joined path
        # and checks again — three layers, because this is the one parameter
        # whose value becomes a filesystem path.
        if not isinstance(value, str):
            fail(param.name, f"{param.label} must be an upload reference")
            return
        if param.pattern and not re.fullmatch(param.pattern, value):
            fail(
                param.name,
                f"{param.label}: that is not an upload we issued. Add your "
                f"documents again.",
            )

    elif param.type == "file":
        # No template that provisions uses one yet (only fine-tune, coming-soon).
        # Accepting it silently would be the wrong default: an upload that is
        # neither stored nor validated is worse than a clear refusal.
        fail(
            param.name,
            f"{param.label}: file parameters are not supported yet",
        )


def _check_project_name(
    name: str,
    param: Parameter,
    namespace_exists: Callable[[str], bool],
    fail: Callable[[str, str], None],
) -> None:
    """Invariant (c), plus the collision check.

    The template's own reserved lists are UNIONED with the constants above, so a
    template can add restrictions and can never remove them.
    """
    reserved = RESERVED_NAMES | set(param.reservedNames or [])
    prefixes = tuple(set(RESERVED_PREFIXES) | set(param.reservedPrefixes or []))

    if name in reserved:
        fail(
            PROJECT_NAME_PARAM,
            f"'{name}' is reserved by the platform. Reserved names: "
            f"{', '.join(sorted(reserved))}.",
        )
        return

    for prefix in sorted(prefixes):
        if name.startswith(prefix):
            fail(
                PROJECT_NAME_PARAM,
                f"Project names cannot start with '{prefix}' — that prefix is "
                f"reserved by the platform.",
            )
            return

    # Only now is it worth asking the cluster.
    if namespace_exists(name):
        # User-side language per invariant (e): the PI is choosing a project name,
        # and does not need to know it becomes a namespace called p-<name>.
        fail(
            PROJECT_NAME_PARAM,
            f"A project called '{name}' already exists. Choose a different name.",
        )
