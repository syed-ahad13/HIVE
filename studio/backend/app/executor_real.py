# ===========================================================================
# HIVE STUDIO BFF — THE REAL EXECUTOR
# ===========================================================================
# The same six steps as MockExecutor, performed against the actual cluster.
# Reached only with STUDIO_EXECUTOR=real, which must be set deliberately — the
# default is mock precisely because the dangerous direction is provisioning by
# accident (CLAUDE.md §6).
#
# ---------------------------------------------------------------------------
# INVARIANT (a): RENDER, WRITE, THEN APPLY THE FILE THAT WAS WRITTEN
# ---------------------------------------------------------------------------
# Every step that creates something does it in three separate motions:
#
#     text = render.render_fragment(...)      # from app/render.py, unchanged
#     path.write_text(text)                   # into tenants/<project>/rendered/
#     kubectl apply -f path                   # THE FILE, never stdin
#
# Applying from a pipe would work identically and would break the invariant: the
# point is that what ran is on disk, diffable, and reviewable afterwards. A
# Studio-provisioned project must be as inspectable as a hand-written one.
#
# ---------------------------------------------------------------------------
# INVARIANT (d): THE TWO REFERENCE TENANTS ARE UNTOUCHABLE
# ---------------------------------------------------------------------------
# p-inf-101 and p-inf-103 are the hand-built reference implementation the whole
# rehearsal is measured against. Three independent guards stand between this
# module and them: validation.py rejects those project names, namespace-pack
# refuses a namespace that is not labelled as this project's, and every
# destructive command is checked against `_assert_ours` before it runs. Three,
# because this is the one module that could do real damage quickly.
#
# ---------------------------------------------------------------------------
# THE SYNCER PROBLEM, AND WHAT THIS DOES ABOUT IT
# ---------------------------------------------------------------------------
# The architecture is: the tenant AUTHORS an InferenceService inside their own
# vCluster, and the ONE central KServe on the host RECONCILES it. Locally the
# mirroring between those two is done by scripts/sync-isvc.sh — a polling
# stand-in for a vCluster Pro feature (DEVIATIONS.md, Fallback B).
#
# That script CANNOT serve a Studio project. Its tenant list is hard-coded to
# inf-101 and inf-103 and it rejects any other name, and it is a daemon that has
# to be running. So this executor performs the mirror itself, once, at the moment
# it is needed: apply inside the vCluster (the tenant is the author), then mirror
# to the host namespace under the same `<name>-x-<ns>-x-<vcluster>` convention the
# script uses, where the central KServe picks it up.
#
# Same architecture, deterministic mechanism, no daemon. Logged in DEVIATIONS.md.
# ===========================================================================

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from . import dataplane, render
from .catalog import fragment_for as catalog_fragment_for
from .catalog import sample_dataset as catalog_sample_dataset
from .paths import repo_root as _repo_root, uploads_root
from .pipeline import (
    RESOURCE_TO_STEP,
    Executor,
    StepContext,
    StepResult,
    _raise_if_injected,
    _raise_if_injected_teardown,
)

# Host namespaces this module may never write to, whatever it is asked to do.
# Invariant (d), enforced at the last possible moment rather than only at the
# first: validation.py already rejects these project names, and this is the guard
# that still holds if some future caller bypasses it.
PROTECTED_NAMESPACES = frozenset({"p-inf-101", "p-inf-103", "default", "kube-system", "kserve"})

# ---------------------------------------------------------------------------
# THE OWNERSHIP LABEL, AND WHY IT IS A SET
# ---------------------------------------------------------------------------
# CLAUDE.md Studio invariant (d): "Studio only acts on namespaces carrying
# hive.ksl/template: studio-inference." There are now THREE templates that
# provision, so that value is one of three, and the check is membership rather
# than string equality.
#
# Getting this wrong is bad in a specific direction each way: too narrow and a
# PI cannot delete their own project (the delete guard refuses a namespace it
# does not recognise); too wide — say, `startswith("studio")` — and the check
# stops being a whitelist. Every template's namespace-pack fragment writes one
# of these literals, and nothing constructs the value.
#
# `studio-batch` was missing here while batch-inference's namespace-pack was
# already writing it, so a finished run could be created and then never removed:
# DELETE answered 403 naming a label the fragment had written itself. A value
# added to a fragment and not to this set is exactly that failure, which is why
# they are listed rather than derived.
STUDIO_TEMPLATE_LABELS = frozenset({"studio-inference", "studio-rag", "studio-batch"})

REQUIRED_CONTEXT = "kind-hive-host"

# ---------------------------------------------------------------------------
# THE ONE PLACE THE GGUF RUNTIMES ARE NAMED
# ---------------------------------------------------------------------------
# Which curated runtimes this executor knows how to install on demand, and the
# file each lives in. `hive-llama-cpp` is NOT in this repository — Step 9b was
# never done here (docs/studio-real-proof.md §7) — so its entry maps to a file
# that does not exist and the precondition reports it as missing, which is
# exactly what it should say. `hive-llama-cpp-rag` was authored for the RAG
# stack (docs/catalog-curation.md) and does exist.
CURATED_RUNTIMES: dict[str, str] = {
    "hive-llama-cpp": "host/serving/runtimes/llama-cpp.yaml",
    "hive-llama-cpp-rag": "host/serving/runtimes/llama-cpp-rag.yaml",
}

# Per-step ceilings. Generous where a first-ever image pull or a large download
# can legitimately take minutes; tight where a hang means something is wrong.
TIMEOUTS = {
    "kubectl": 120,
    "apply": 180,
    "seed-wait": 900,      # a gguf download is ~400MB on a domestic line
    "vcluster-create": 420,
    "vcluster-ready": 300,
    "isvc-apply": 180,
    "ready": 900,          # cold image pull the first time a runtime lands on a node
    "delete-ns": 300,
    # 120s was too tight, and the reason is not CRD size. A vCluster control
    # plane that restarts costs ~30s of unreachable API server per restart, and
    # three restarts fits inside one deploy on a loaded laptop. The budget now
    # covers a control plane that wobbles and recovers, while the message on
    # expiry says which of the two things actually went wrong.
    "crd-established": 300,
    # How long the tenant's API may take to answer THROUGH THE KUBECONFIG after
    # `vcluster connect --print` returns. That command starts a proxy container
    # and returns before the proxy forwards anything — measured at 44s on a
    # loaded laptop (2026-09-29), ~2s on a quiet one.
    "tenant-api": 180,
    # --- RAG stack ---------------------------------------------------------
    "rollout": 300,        # Qdrant or the RAG service: one pod, image already loaded
    "pod-ready": 180,      # the staging pod — busybox, nothing to pull after the first
    "cp": 300,             # kubectl cp of a corpus; a tar stream over exec
    "ingest-wait": 900,    # chunk + embed every passage on CPU, then upsert
    "rag-ready": 300,      # /healthz, then one real question through the stack
    # Building the toolkit image is minutes on a cold machine (onnxruntime is
    # ~200MB of wheels) and seconds once it exists. The approve step runs the
    # build script every time because it is idempotent, so this ceiling only
    # ever bites on the very first RAG deployment on a fresh laptop.
    # --- batch inference ---------------------------------------------------
    # How long to wait for the Job overall is NOT here: it comes from the PI's
    # own `runTimeoutMinutes`, enforced by the Job's activeDeadlineSeconds. This
    # is only the poll cadence and the per-call ceilings around it.
    "batch-poll": 5,          # seconds between polls of a running Job
    "batch-grace": 120,       # extra wall-clock allowed past the Job's own deadline
    "image-build": 900,
}


class StepFailed(RuntimeError):
    """A step could not complete. The message is what a PI reads."""


@dataclass
class Precondition:
    name: str
    ok: bool
    detail: str
    # Does a failure here stop EVERY deployment, or only some?
    #
    # The kubectl context and the GPU capacity are blocking: nothing can be
    # provisioned without them. A missing serving runtime is not — it matters
    # only to a deployment whose model needs it, and refusing to provision a
    # sklearn endpoint because a gguf runtime is absent would block the one
    # workload this build already serves.
    #
    # A FLAG RATHER THAN A NAME COMPARISON. This used to be
    # `c.name != "hive-llama-cpp runtime"` in approve(), which meant adding a
    # second non-blocking check required remembering to edit a string literal in
    # a different method.
    blocking: bool = True

    def line(self) -> str:
        return f"[{'OK ' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


# Re-exported so the many callers in this module keep reading `repo_root()`,
# while there is exactly one implementation (app/paths.py).
repo_root = _repo_root


def rendered_dir(project: str) -> Path:
    return repo_root() / "tenants" / project / "rendered"


def _tail(text: str, lines: int = 25) -> str:
    kept = [l for l in text.splitlines() if l.strip()]
    return "\n".join(kept[-lines:])


async def run(
    cmd: list[str],
    timeout: int,
    emit=None,
    label: str | None = None,
    check: bool = True,
) -> tuple[int, str]:
    """Run a command, capturing stdout AND stderr together.

    Combined rather than separate because these tools split their output on no
    principle a reader cares about: `vcluster` prints progress to stderr and its
    result to stdout, `kubectl wait` the reverse. A PI reading a failed step wants
    the transcript in the order it happened.

    Returns (returncode, output). Raises StepFailed on non-zero when `check`, with
    the TAIL of the output — the first lines of a kubectl error are usually
    boilerplate and the last lines are the reason.
    """
    shown = " ".join(cmd)
    if emit:
        emit(f"$ {shown}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(repo_root()),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise StepFailed(
            f"timed out after {timeout}s: {label or shown}\n"
            f"The command was killed. Nothing was rolled back automatically."
        ) from None

    text = (out or b"").decode("utf-8", "replace")
    if emit and text.strip():
        emit(_tail(text, 40))

    if check and proc.returncode != 0:
        raise StepFailed(
            f"{label or shown} failed (exit {proc.returncode}):\n{_tail(text)}"
        )
    return proc.returncode, text


# The marker /app/ingest.py prints its one machine-readable line on, and the
# MockExecutor prints a canned line of the same shape — so a mock smoke run
# exercises this parser too, and a drift in the format is caught without a
# cluster. Spelled in both places; ingest.py holds the producer's copy as
# RESULT_PREFIX.
INGEST_RESULT_PREFIX = "INGEST_RESULT"


def _parse_ingest_result(log: str) -> dict | None:
    """Pull the ingestion Job's result line out of its log, or None.

    Scans BACKWARDS and takes the last match. A retried Job's pod may carry
    output from more than one attempt, and the most recent line is the one that
    describes the index that now exists.

    Tolerant of the line being prefixed — the mock writes `[mock] INGEST_RESULT
    {...}` — because the marker is what identifies it, not the line start.
    Returns None rather than raising: the caller decides what a missing result
    means, and for the real executor it means the step failed.
    """
    for line in reversed(log.splitlines()):
        if INGEST_RESULT_PREFIX not in line:
            continue
        payload = line.split(INGEST_RESULT_PREFIX, 1)[1].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if not {"files", "chunks", "collection"} <= set(parsed):
            continue
        parsed.setdefault("dims", 0)
        return parsed
    return None


# ---------------------------------------------------------------------------
# A DROPPED CONNECTION IS NOT A REJECTED REQUEST
# ---------------------------------------------------------------------------
# What kubectl prints when it never got an answer, as opposed to when the server
# answered "no". Only the first kind is worth retrying: a manifest the server
# refused will be refused again, and retrying it only delays the real message.
#
# Every entry was seen for real against a tenant's API reached through
# `vcluster connect`'s background proxy, which accepts connections before it is
# forwarding them and resets them until it is.
_TRANSIENT_MARKERS = (
    "connection reset by peer",
    "connection refused",
    "tls handshake timeout",
    "i/o timeout",
    "unexpected eof",
    "unable to connect to the server",
    "failed to download openapi",
    "http2: client connection lost",
    "context deadline exceeded",
)


def _is_transient(output: str) -> bool:
    text = output.lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _needs_rag_toolkit(template) -> bool:
    """Does this template run the locally-built toolkit image?

    Asked of the TEMPLATE's declared resources rather than of its id, so a
    future template that reuses the ingestion machinery gets the image
    precondition by declaring the resource — which is the thing that actually
    makes the image necessary — rather than by being added to a list here.
    """
    declared = set(template.spec.resources or [])
    return bool(declared & {"ingestion", "rag-api"})


def _needs_batch_driver(template) -> bool:
    """Does this template run the locally-built batch driver image?

    Asked of the declared resources for the same reason as above: a template
    that runs a batch Job needs the driver because it declares `batch-run`, not
    because of what it is called.
    """
    return "batch-run" in set(template.spec.resources or [])


def _assert_ours(namespace: str) -> None:
    """Last line of defence before anything destructive. Invariant (d)."""
    if namespace in PROTECTED_NAMESPACES:
        raise StepFailed(
            f"refusing to act on {namespace!r}: it is a protected namespace. "
            f"Studio only ever touches namespaces it created "
            f"(labelled hive.ksl/template=studio-inference)."
        )


# ---------------------------------------------------------------------------
# PRECONDITIONS
# ---------------------------------------------------------------------------


def _sync_check(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """Blocking command runner, for the preconditions only.

    __init__ is not async, and these three checks are a couple of seconds of
    kubectl. Everything inside a step uses the async runner above.
    """
    import subprocess

    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(repo_root())
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: timed out after {timeout}s"


# ---------------------------------------------------------------------------
# IS THE HOST CLUSTER'S OWN CONTROL PLANE ACTUALLY RUNNING?
# ---------------------------------------------------------------------------
# Every step Studio takes ends in a controller doing the work: the Job controller
# creates the seed pod, the StatefulSet controller creates the vCluster's pod, the
# scheduler places both, and the namespace controller empties a namespace on
# delete. All of those live in TWO processes — kube-controller-manager and
# kube-scheduler — and if either is down, Studio's `kubectl apply` still succeeds
# and then nothing happens. The step waits out its timeout and blames whatever it
# was waiting for.
#
# THIS IS NOT HYPOTHETICAL. On 2026-09-29, after a Docker Desktop restart, the
# 7.7GiB VM was thrashing swap; etcd requests reached a p95 of 6.4s, and both
# processes lost their 10-second leader leases and crash-looped ~90 times each.
# A new project "failed at vcluster" and a delete "timed out on the namespace",
# and neither message named the actual cause. See docs/control-plane-recovery.md.
#
# TWO SIGNALS, BECAUSE ONE IS NOT ENOUGH:
#
#   the pod        CrashLoopBackOff / not Ready. Cheap and obvious, but a
#                  crash-looping controller is Ready for the ~30s it survives
#                  each restart, so a check that lands in that window passes.
#   the lease      coordination.k8s.io Lease renewTime. This is the
#                  AUTHORITATIVE answer to "is anyone doing the work right now":
#                  a leader renews every couple of seconds, so a lease older
#                  than its duration plus a grace means nobody is leading.
#
# Either one failing means unhealthy. Both are reads, and nothing here writes.
CONTROL_PLANE_COMPONENTS = ("kube-controller-manager", "kube-scheduler")

# How far past its own duration a lease may be before it counts as abandoned.
# A healthy leader renews every ~2s against a 15s lease, so 30s of slack never
# fires on a working cluster, and still catches a dead one within half a minute.
LEASE_GRACE_SECONDS = 30

# What each component DOES, in the terms of what a PI would notice missing.
_COMPONENT_ROLE = {
    "kube-controller-manager": (
        "creates pods for Jobs and StatefulSets, binds volumes, and deletes "
        "namespaces"
    ),
    "kube-scheduler": "places every new pod on a node",
}


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


def _human_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def control_plane_health() -> tuple[bool, str]:
    """(healthy, explanation) for the host cluster's controller-manager and scheduler.

    SYNCHRONOUS on purpose — two short kubectl reads — so it can be run from a
    thread by the async steps and directly by the precondition gathering, which
    already runs in a thread.

    The explanation is written for the person reading a failed step: which
    component, what the evidence is, what it means for their project, and what
    fixes it. An unreachable API server is itself reported as unhealthy, because
    "we could not ask" is not evidence that things are fine.
    """
    rc, raw_pods = _sync_check(
        ["kubectl", "get", "pods", "-n", "kube-system", "-l", "tier=control-plane",
         "-o", "json"],
        timeout=20,
    )
    rc2, raw_leases = _sync_check(
        ["kubectl", "get", "lease", "-n", "kube-system", *CONTROL_PLANE_COMPONENTS,
         "-o", "json"],
        timeout=20,
    )
    if rc != 0 or rc2 != 0:
        return False, (
            "the host API server did not answer a basic read within 20s, so the "
            "cluster's own controllers cannot be doing their work either.\n"
            + _tail(raw_pods if rc != 0 else raw_leases, 3)
        )

    try:
        pods = json.loads(raw_pods).get("items", [])
        leases = {
            lease["metadata"]["name"]: lease.get("spec", {})
            for lease in json.loads(raw_leases).get("items", [])
        }
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return False, f"could not read the control plane's status: {exc}"

    problems: list[str] = []
    for component in CONTROL_PLANE_COMPONENTS:
        pod = next(
            (p for p in pods if p.get("metadata", {}).get("labels", {}).get("component")
             == component),
            None,
        )
        evidence: list[str] = []
        if pod is None:
            evidence.append("no pod found in kube-system")
        else:
            status = (pod.get("status", {}).get("containerStatuses") or [{}])[0]
            waiting = (status.get("state", {}).get("waiting") or {}).get("reason")
            restarts = status.get("restartCount", 0)
            if waiting:
                evidence.append(f"{waiting}, {restarts} restarts")
            elif not status.get("ready"):
                evidence.append(f"not Ready, {restarts} restarts")

        lease = leases.get(component, {})
        age = _age_seconds(lease.get("renewTime"))
        duration = int(lease.get("leaseDurationSeconds") or 15)
        if not lease.get("holderIdentity"):
            evidence.append("no leader holds its lease")
        elif age is not None and age > duration + LEASE_GRACE_SECONDS:
            evidence.append(
                f"its leader lease was last renewed {_human_age(age)} ago "
                f"(a working leader renews every few seconds)"
            )

        if evidence:
            problems.append(
                f"  - {component} ({_COMPONENT_ROLE[component]}): "
                + "; ".join(evidence)
            )

    if not problems:
        return True, "kube-controller-manager and kube-scheduler are leading normally"

    return False, (
        "the host cluster's own control plane is not running:\n"
        + "\n".join(problems)
        + "\n\n"
        "While this lasts, anything created is accepted by the API and then never "
        "acted on — pods are not created, not scheduled, and namespaces are not "
        "emptied. This is not a problem with your project.\n"
        "The usual cause on this laptop is memory pressure in the Docker Desktop "
        "VM, which makes etcd too slow for these components to keep their leases. "
        "See docs/control-plane-recovery.md."
    )


class RealExecutor(Executor):
    """Provisions for real.

    Preconditions are gathered LAZILY, the first time `approve` runs — not when
    the executor is built. An executor is built for every DELETE and every retry
    too, and teardown reads none of them; gathering them in __init__ made each of
    those requests run five synchronous kubectl calls on the event loop first,
    which on a degraded API server stalled the whole BFF, the console's polling
    included.
    """

    name = "real"

    def __init__(self) -> None:
        # Gathered on first use by `approve`. See the class docstring.
        self.preconditions: list[Precondition] | None = None
        # Set by the vcluster step; used by everything that talks to the tenant's
        # own cluster. See _vc() for why it may or may not be needed.
        self._kubeconfig: dict[str, Path] = {}
        self._vc_mode: dict[str, str] = {}

    def _gather_preconditions_sync(self) -> list[Precondition]:
        """Every precondition, in the order approve prints them.

        The context comes first because every later check asks the cluster
        something, and asking the wrong cluster is worse than asking none. The
        control plane comes next because if it is down, the rest are answering
        questions about a cluster that will not act on the answers.
        """
        context = self._check_context()
        if not context.ok:
            # Nothing else is meaningful against the wrong cluster — and the
            # runtime check APPLIES a file when a runtime is missing, which
            # must never happen to a cluster Studio was not pointed at.
            return [context]
        return [
            context,
            self._check_control_plane(),
            *self._check_curated_runtimes(),
            self._check_gpus(),
        ]

    async def _gather_preconditions(self) -> list[Precondition]:
        """The preconditions, gathered once per executor, off the event loop."""
        if self.preconditions is None:
            self.preconditions = await asyncio.to_thread(self._gather_preconditions_sync)
        return self.preconditions

    # --- (0) the host cluster's own controllers -----------------------------
    def _check_control_plane(self) -> Precondition:
        """BLOCKING, and it has to be.

        Provisioning into a cluster whose controller-manager is down cannot
        succeed: every manifest applies, and then nothing reconciles it, so the
        run times out several steps later with a message about whichever step
        happened to be waiting. Refusing here takes a second and names the real
        cause, instead of taking ten minutes and naming the wrong one.
        """
        healthy, why = control_plane_health()
        if healthy:
            return Precondition("host control plane", True, why)
        return Precondition("host control plane", False, why)

    # --- (a) the right cluster --------------------------------------------
    def _check_context(self) -> Precondition:
        rc, out = _sync_check(["kubectl", "config", "current-context"])
        current = out.strip()
        if rc != 0:
            return Precondition("kubectl context", False, f"could not be read — {_tail(out, 3)}")
        if current != REQUIRED_CONTEXT:
            return Precondition(
                "kubectl context",
                False,
                f"is {current!r}, but Studio only provisions into "
                f"{REQUIRED_CONTEXT!r}. Switch with: kubectl config use-context {REQUIRED_CONTEXT}",
            )
        return Precondition("kubectl context", True, current)

    # --- (b) the curated gguf runtimes ------------------------------------
    def _check_curated_runtimes(self) -> list[Precondition]:
        """Is each curated serving runtime installed? One Precondition each.

        NONE OF THESE IS A HARD GATE, and that is a deliberate departure from a
        literal reading of the task. A runtime is needed only by deployments
        whose model uses it; a sklearn endpoint has no use for a gguf runtime,
        and refusing to provision anything at all because an unrelated runtime
        is absent would block the reference workload this build already serves.

        So they are recorded here, surfaced in every approve log, and enforced
        in `approve` ONLY for the runtime the deployment in hand will actually
        use — see render.required_runtime, which is also what the fragment
        renders, so the check and the manifest cannot disagree.

        Each one SELF-HEALS where it can: a runtime that is missing but has a
        file committed under host/serving/runtimes/ is applied on the spot. That
        is not a shortcut around "everything goes through a file in the repo"
        (CLAUDE.md §5) — it applies exactly that file.
        """
        results: list[Precondition] = []
        for name, relative in sorted(CURATED_RUNTIMES.items()):
            results.append(self._check_one_runtime(name, relative))
        return results

    def _check_one_runtime(self, name: str, relative: str) -> Precondition:
        label = f"{name} runtime"
        rc, _ = _sync_check(
            ["kubectl", "get", "clusterservingruntime", name, "-o", "name"]
        )
        if rc == 0:
            return Precondition(label, True, "installed", blocking=False)

        candidate = repo_root() / relative
        if candidate.is_file():
            arc, aout = _sync_check(["kubectl", "apply", "-f", str(candidate)], timeout=60)
            if arc == 0:
                return Precondition(
                    label, True, f"was missing; applied {relative}", blocking=False
                )
            return Precondition(
                label,
                False,
                f"missing, and applying {relative} failed: {_tail(aout, 3)}",
                blocking=False,
            )

        return Precondition(
            label,
            False,
            f"NOT INSTALLED, and {relative} does not exist. This runtime is "
            f"created by Step 9b, which has not been done in this repository. "
            f"Models needing it cannot be deployed; everything else is unaffected.",
            blocking=False,
        )

    # --- (c) GPUs ----------------------------------------------------------
    def _check_gpus(self) -> Precondition:
        rc, out = _sync_check(
            [
                "kubectl",
                "get",
                "nodes",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}={.status.capacity.nvidia\\.com/gpu}{\"\\n\"}{end}",
            ]
        )
        if rc != 0:
            return Precondition("nvidia.com/gpu capacity", False, f"could not be read — {_tail(out, 3)}")

        advertised = []
        for line in out.strip().splitlines():
            if "=" not in line:
                continue
            node, _, count = line.partition("=")
            if count.strip().isdigit() and int(count) > 0:
                advertised.append(f"{node}={count}")

        if not advertised:
            return Precondition(
                "nvidia.com/gpu capacity",
                False,
                "no node advertises nvidia.com/gpu. The fake GPU capacity is a "
                "node-status patch and any kubelet restart (a Docker Desktop "
                "restart included) wipes it. Re-run: scripts/fake-gpus.sh",
            )
        return Precondition("nvidia.com/gpu capacity", True, ", ".join(advertised))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _host_ns(self, project: str) -> str:
        return f"p-{project}"

    def _synced_name(self, project: str) -> str:
        """The host-side name, following vCluster's translation convention.

        `<name>-x-<namespace>-x-<vcluster>`. The isvc is authored as `<project>`
        in the vCluster's `default` namespace, and the vCluster is named after the
        project, so all three parts are known — but ready-check still DISCOVERS
        the real name from the cluster rather than trusting this, because a
        constructed identifier that drifts is exactly the class of bug the
        `-predictor` URL finding warned about.
        """
        return f"{project}-x-default-x-{project}"

    async def _ensure_batch_image(self, ctx: StepContext) -> None:
        """Build the batch driver image if needed, and make sure the NODES have it.

        Runs scripts/build-batch-driver.sh, which is idempotent and cheap when
        the image is already there — it checks first and builds only if needed,
        which is what makes it safe to call on every deployment rather than
        only by a human before a demo.

        THE FAILURE THIS PREVENTS is `ImagePullPolicy: Never` meeting a node
        that does not have the image: the pod sits in ImagePullBackOff naming an
        image `docker images` clearly shows is present, because the host Docker
        daemon and the nodes' containerd are separate stores.
        """
        script = repo_root() / "scripts" / "build-batch-driver.sh"
        if not script.is_file():
            raise StepFailed(
                f"{script.relative_to(repo_root())} is missing, so the batch driver "
                f"image cannot be built. A batch run cannot start without it."
            )
        ctx.emit("checking the batch driver image (builds only if missing)")
        rc, out = await run(
            ["bash", str(script)],
            timeout=TIMEOUTS["image-build"], emit=ctx.emit,
            label="build-batch-driver.sh", check=False,
        )
        if rc != 0:
            raise StepFailed(
                f"the batch driver image could not be prepared, so the run has "
                f"nothing to execute with.\n{_tail(out, 20)}"
            )

    async def _ensure_rag_image(self, ctx: StepContext) -> None:
        """Build the RAG toolkit image if needed, and make sure the NODES have it.

        Runs scripts/build-rag-toolkit.sh, which is idempotent: it skips the
        build when the image already exists on the host and skips the load when
        the nodes already have it, so on every deployment after the first this
        is a few `docker inspect` calls.

        WHY A PRECONDITION AND NOT A DOCUMENTED MANUAL STEP. The image lives in
        no registry, so a pod referencing it sets `imagePullPolicy: Never` — and
        if the nodes do not have it the failure is ImagePullBackOff on an image
        `docker images` clearly shows is present. That is a genuinely confusing
        error, it appears three steps later than its cause, and it is entirely
        preventable here.

        Shelling out to the script rather than reimplementing it keeps ONE
        procedure for building this image, wrapped in scripts/ where CLAUDE.md
        §5 says imperative commands live.
        """
        script = repo_root() / "scripts" / "build-rag-toolkit.sh"
        if not script.is_file():
            raise StepFailed(
                f"this template needs the RAG toolkit image, and "
                f"scripts/build-rag-toolkit.sh is missing."
            )
        ctx.emit(f"checking the RAG toolkit image ({render.RAG_TOOLKIT_IMAGE})")
        await run(
            ["bash", str(script)],
            timeout=TIMEOUTS["image-build"],
            emit=ctx.emit,
            label="scripts/build-rag-toolkit.sh",
        )

        # VERIFY WHAT THE RENDERER WILL ACTUALLY NAME. The script checks its own
        # `IMAGE`, which is overridable from the environment; this checks the
        # constant every rendered manifest carries. They agree today and the
        # script refuses to run if they do not — this is the assertion that they
        # still agreed after it ran.
        rc, out = await run(
            ["bash", str(script), "--check"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        if rc != 0:
            raise StepFailed(
                f"the RAG toolkit image {render.RAG_TOOLKIT_IMAGE} is not available "
                f"to the cluster after running the build script:\n{_tail(out, 10)}"
            )
        ctx.emit(f"  {render.RAG_TOOLKIT_IMAGE} is on the host and on the kind nodes")

    def _fragment(self, ctx: StepContext, step_id: str) -> str:
        """The fragment file this STEP renders, for THIS template.

        A step is not a resource and a resource is not a fragment, and
        `model-seed` is where all three come apart. Both templates run a step
        called `model-seed`; inference-endpoint declares the resource
        `model-seed-job` (fragment model-seed-job.yaml.tmpl) while rag-stack
        declares `model-seed` (fragment model-seed.yaml.tmpl), because rag-stack's
        seeds two models and is not simply "the Job for the model".

        So this walks the template's OWN resources, finds the one that plans to
        this step, and resolves its fragment. A step therefore does not have to
        know which template it is running under — which is what lets one
        `model_seed` implementation serve both.
        """
        for entry in ctx.template.spec.resources or []:
            if RESOURCE_TO_STEP.get(entry) == step_id:
                return catalog_fragment_for(entry)
        # Not declared by this template, so this step should never have been
        # planned. Fall back to the alias table so the failure names a fragment
        # file that does not exist rather than raising a bare KeyError here.
        return catalog_fragment_for(step_id)

    async def _write_rendered(
        self,
        ctx: StepContext,
        fragment: str,
        extras: dict | None = None,
        filename: str | None = None,
    ) -> Path:
        """Render one fragment and WRITE it. Invariant (a), first two motions.

        `extras` carries values that could only be learned by asking the cluster
        — today the chat model's live hostname and the gateway service, for the
        RAG service. Everything else comes from the context, so the render stays
        a pure function of inputs the caller can see.
        """
        out_dir = rendered_dir(ctx.project)
        out_dir.mkdir(parents=True, exist_ok=True)
        text = render.render_fragment(
            fragment,
            ctx.template.metadata.id,
            ctx.project,
            ctx.params,
            ctx.model,
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            embed_model=ctx.embed_model,
            extras=extras,
        )
        # `filename` overrides the default, for the one fragment that is
        # rendered TWICE in a single run. staging-pod.yaml.tmpl is applied once
        # to push a dataset in and once to pull results out, and writing both to
        # `staging-pod.yaml` would leave rendered/ holding only the second —
        # losing the record of what the first step actually applied, which is
        # the whole of invariant (a).
        path = out_dir / (filename or f"{fragment}.yaml")
        path.write_text(text, encoding="utf-8")
        ctx.emit(f"rendered -> {path.relative_to(repo_root())}")
        return path

    async def _vc(self, ctx: StepContext, args: list[str], timeout: int, label: str):
        """Run a kubectl command INSIDE the tenant's vCluster.

        FALLBACK LADDER (A), decided once per deployment and logged:

          primary   `vcluster connect <p> -n p-<p> --silent -- kubectl ...`
          fallback  write the kubeconfig once with `--print`, then plain
                    `kubectl --kubeconfig=... ...`

        The primary is one process and no secret on disk. If it fails for a
        reason that is about the connect mechanism rather than the command, this
        switches to the explicit kubeconfig for the rest of the deployment — same
        result, more moving parts, so it is the fallback rather than the default.
        """
        project = ctx.project
        mode = self._vc_mode.get(project, "connect")

        if mode == "connect":
            rc, out = await run(
                ["vcluster", "connect", project, "-n", self._host_ns(project), "--silent",
                 "--", *args],
                timeout=timeout,
                emit=ctx.emit,
                label=label,
                check=False,
            )
            if rc == 0:
                return out
            ctx.emit(
                "note: `vcluster connect -- CMD` did not succeed; falling back to an "
                "explicit kubeconfig for the rest of this deployment (DEVIATIONS.md, "
                "Studio fallback A)"
            )
            self._vc_mode[project] = "kubeconfig"

        kubeconfig = await self._ensure_kubeconfig(ctx)
        _, out = await run(
            [args[0], f"--kubeconfig={kubeconfig}", *args[1:]],
            timeout=timeout,
            emit=ctx.emit,
            label=label,
        )
        return out

    async def _ensure_kubeconfig(self, ctx: StepContext) -> Path:
        """Write the tenant's kubeconfig, once per deployment.

        `--silent` matters: without it the CLI prints a "vCluster is up and
        running" banner to STDOUT, which would land inside the kubeconfig file and
        make it unparseable. VERSIONS.md records that banner as a known trap.
        """
        existing = self._kubeconfig.get(ctx.project)
        if existing and existing.is_file():
            return existing

        out_dir = rendered_dir(ctx.project)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "kubeconfig"

        _, text = await run(
            ["vcluster", "connect", ctx.project, "-n", self._host_ns(ctx.project),
             "--silent", "--print"],
            timeout=TIMEOUTS["kubectl"],
            label="vcluster connect --print",
        )
        if "apiVersion" not in text:
            raise StepFailed(
                "vcluster connect --print did not return a kubeconfig:\n" + _tail(text)
            )
        path.write_text(text, encoding="utf-8")
        # It is an admin credential for the tenant's control plane. .gitignore
        # already excludes it; the mode makes it unreadable to other accounts.
        path.chmod(0o600)
        self._kubeconfig[ctx.project] = path
        ctx.emit(f"kubeconfig -> {path.relative_to(repo_root())} (mode 600)")
        return path

    # ------------------------------------------------------------------
    # STEPS
    # ------------------------------------------------------------------

    async def approve(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("approve")
        ctx.emit("checking preconditions")
        preconditions = await self._gather_preconditions()
        lines = ["Preconditions:"]
        for check in preconditions:
            lines.append("  " + check.line())
            ctx.emit("  " + check.line())

        # Hard failures: anything that makes provisioning impossible or unsafe.
        # `blocking` is a property of the check, not of its name — see
        # Precondition.blocking.
        blocking = [c for c in preconditions if not c.ok and c.blocking]
        if blocking:
            raise StepFailed(
                "cannot provision — "
                + "; ".join(f"{c.name}: {c.detail}" for c in blocking)
            )

        # THE RUNTIME THIS DEPLOYMENT WILL ACTUALLY USE, which is not always the
        # one the catalogue entry names: rag-stack pins its own, because the
        # >=4096-token context is a requirement of retrieval rather than of the
        # model. render.required_runtime is the single place that decision is
        # expressed, and it is the same function the fragment renders from — so
        # this check can never be asking about a different runtime than the one
        # the manifest will pin.
        needed = render.required_runtime(ctx.template.metadata.id, ctx.model)
        if needed:
            check = next(
                (c for c in preconditions if c.name == f"{needed} runtime"), None
            )
            if check is None:
                # A runtime the catalogue pins but this executor does not know
                # how to install. Not fatal to check for — ask the cluster.
                rc, _ = await run(
                    ["kubectl", "get", "clusterservingruntime", needed, "-o", "name"],
                    timeout=TIMEOUTS["kubectl"], check=False,
                )
                if rc != 0:
                    raise StepFailed(
                        f"this deployment needs the ClusterServingRuntime {needed!r}, "
                        f"which is not installed and is not one this executor knows "
                        f"how to apply (see CURATED_RUNTIMES)."
                    )
                ctx.emit(f"  runtime {needed}: installed")
            elif not check.ok:
                raise StepFailed(
                    f"this deployment needs the ClusterServingRuntime {needed!r}, "
                    f"which is not installed on this cluster.\n"
                    f"{check.detail}\n"
                    f"Choose a different model, or author the runtime under "
                    f"host/serving/runtimes/ and record it in VERSIONS.md "
                    f"(docs/catalog-curation.md describes the curation flow)."
                )
            else:
                ctx.emit(f"  runtime {needed}: installed")
        for check in preconditions:
            if not check.ok and not check.blocking and check.name != f"{needed} runtime":
                ctx.emit(
                    f"  note: {check.name} is absent, which does not affect this "
                    f"deployment — it does not use that runtime."
                )

        # --- the RAG toolkit image ---------------------------------------
        # Only for templates that need it, and only ever as the LAST thing
        # `approve` does before it writes the snapshot: it is minutes on a cold
        # machine, and there is no point paying that before the cheap checks
        # above have had their chance to reject the request.
        if _needs_rag_toolkit(ctx.template):
            await self._ensure_rag_image(ctx)
        if _needs_batch_driver(ctx.template):
            await self._ensure_batch_image(ctx)

        # A snapshot of exactly what was asked for, beside the manifests it
        # produced. Answers "what did this PI actually request?" from the
        # filesystem alone, without the registry.
        out_dir = rendered_dir(ctx.project)
        out_dir.mkdir(parents=True, exist_ok=True)
        snapshot = out_dir / "params.json"
        snapshot.write_text(
            json.dumps(
                {
                    "project": ctx.project,
                    "templateId": ctx.template.metadata.id,
                    "params": ctx.params,
                    "model": ctx.model.model_dump() if ctx.model else None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        lines.append(f"wrote {snapshot.relative_to(repo_root())}")
        ctx.emit(f"wrote {snapshot.relative_to(repo_root())}")
        return StepResult(ok=True, log="\n".join(lines))

    async def namespace_pack(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("namespace-pack")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        # If the namespace already exists it must be OURS. A namespace someone
        # else created that happens to collide with p-<project> must never be
        # adopted, relabelled, quota'd or later deleted by Studio.
        rc, out = await run(
            ["kubectl", "get", "namespace", ns, "-o",
             "jsonpath={.metadata.labels.hive\\.ksl/project}"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        if rc == 0:
            owner = out.strip()
            if owner != ctx.project:
                raise StepFailed(
                    f"namespace {ns} already exists but is not labelled "
                    f"hive.ksl/project={ctx.project} (found {owner or 'no label'}). "
                    f"Refusing to touch a namespace Studio did not create."
                )
            ctx.emit(f"namespace {ns} already exists and belongs to this project — reapplying")

        path = await self._write_rendered(ctx, "namespace-pack")
        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply namespace-pack")

        _, quota = await run(
            ["kubectl", "get", "resourcequota", "tier-quota", "-n", ns,
             "-o", "jsonpath={.status.hard}"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        ctx.emit(f"quota in place: {quota.strip()}")
        return StepResult(ok=True, log=f"namespace {ns} applied from {path.name}")

    async def model_seed(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("model-seed")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        path = await self._write_rendered(ctx, self._fragment(ctx, "model-seed"))
        job = f"seed-{ctx.project}"

        # A completed Job's pod template is immutable, so re-applying an existing
        # one fails. Delete first if it is already there — the Job's own script is
        # idempotent (it skips a file that is already present), so re-running is
        # cheap and re-downloading is not.
        rc, _ = await run(["kubectl", "get", "job", job, "-n", ns],
                          timeout=TIMEOUTS["kubectl"], check=False)
        if rc == 0:
            ctx.emit(f"job/{job} exists — deleting it so the manifest can be reapplied")
            await run(["kubectl", "delete", "job", job, "-n", ns, "--wait=true"],
                      timeout=TIMEOUTS["kubectl"], emit=ctx.emit, check=False)

        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label=f"kubectl apply {path.name}")

        ctx.emit(f"waiting for job/{job} (up to {TIMEOUTS['seed-wait']}s — the model is downloaded once)")
        rc, out = await run(
            ["kubectl", "wait", "--for=condition=complete", f"job/{job}", "-n", ns,
             f"--timeout={TIMEOUTS['seed-wait']}s"],
            timeout=TIMEOUTS["seed-wait"] + 30, emit=ctx.emit, check=False,
        )
        if rc != 0:
            # Distinguish "still running" from "failed", and show the pod's own
            # output — the Job's log is the only place the real reason appears.
            _, logs = await run(["kubectl", "logs", f"job/{job}", "-n", ns, "--tail=30"],
                                timeout=TIMEOUTS["kubectl"], check=False)
            raise StepFailed(
                f"the model-seed Job did not complete.\n{_tail(out, 6)}\n"
                f"--- job log ---\n{_tail(logs, 20)}"
            )

        _, logs = await run(["kubectl", "logs", f"job/{job}", "-n", ns, "--tail=10"],
                            timeout=TIMEOUTS["kubectl"], check=False)
        ctx.emit(_tail(logs, 10))
        return StepResult(ok=True, log=f"job/{job} completed")

    async def vcluster(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("vcluster")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        _, listing = await run(["vcluster", "list", "--output", "json"],
                               timeout=TIMEOUTS["kubectl"], check=False)
        existing = []
        try:
            existing = [v for v in json.loads(listing or "[]")
                        if v.get("Name") == ctx.project or v.get("name") == ctx.project]
        except json.JSONDecodeError:
            ctx.emit("note: could not parse `vcluster list --output json`; assuming none exists")

        if existing:
            ctx.emit(f"vCluster {ctx.project} already exists — skipping creation")
        else:
            values = ("templates/t-ao-values.yaml"
                      if ctx.params.get("availability") == "always-on"
                      else "templates/t-sz-values.yaml")
            ctx.emit(f"creating vCluster with {values}")
            await run(
                ["vcluster", "create", ctx.project, "--namespace", ns,
                 "-f", values,
                 # --connect=false: do not switch the operator's kubecontext.
                 # --add=false: MANDATORY offline — the default tries to register
                 # with vCluster Platform and aborts when not logged in
                 # (VERSIONS.md, step 6).
                 "--connect=false", "--add=false"],
                timeout=TIMEOUTS["vcluster-create"], emit=ctx.emit, label="vcluster create",
            )

        # Poll the StatefulSet rather than trusting `create` to mean "ready".
        # Worth remembering from step 6: when the quota blocks it, NO pod is
        # created at all, so `kubectl get pods` is empty and the explanation lives
        # only in the StatefulSet's events.
        ctx.emit(f"waiting for the control plane to be ready (up to {TIMEOUTS['vcluster-ready']}s)")
        deadline = asyncio.get_running_loop().time() + TIMEOUTS["vcluster-ready"]
        while True:
            _, ready = await run(
                ["kubectl", "get", "statefulset", ctx.project, "-n", ns,
                 "-o", "jsonpath={.status.readyReplicas}"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            if ready.strip() == "1":
                break
            if asyncio.get_running_loop().time() > deadline:
                raise StepFailed(await self._vcluster_not_ready(ctx, ns))
            await asyncio.sleep(5)

        ctx.emit(f"vCluster {ctx.project} is ready")

        # A FRESH vCLUSTER HAS NO KSERVE CRDs, so the tenant cannot author an
        # InferenceService in it at all — `kubectl apply` fails with "resource
        # mapping not found". The two hand-built tenants have them only because
        # scripts/sync-catalog.sh copied the ClusterServingRuntime CRD (Fallback A)
        # and someone applied the InferenceService CRD by hand during step 6; no
        # script records that second act, which is why a Studio project hit it
        # immediately. Seeded here so every Studio vCluster starts able to do the
        # one thing the platform promises it can.
        await self._seed_tenant_api(ctx)

        return StepResult(ok=True, log=f"vCluster {ctx.project} ready in {ns}, KServe API seeded")

    async def _vcluster_not_ready(self, ctx: StepContext, ns: str) -> str:
        """The failure message for a vCluster that never became ready.

        THE CONTROL PLANE IS ASKED FIRST, because it is the one cause that is
        not about this project at all. When kube-controller-manager or the
        scheduler is down, the StatefulSet exists and its pod is never created
        or never placed — and the old message said only "did not become ready",
        which sent the reader to debug a vCluster that was fine.

        THE POD'S OWN STATUS IS INCLUDED, not just the event tail. The tail was
        sorted by `.lastTimestamp`, which newer event sources (the scheduler's
        among them) leave empty — so they sorted to the top and were cut off,
        and the one line saying why the pod was not placed never appeared.
        `kubectl events` orders by the time each event actually happened.
        """
        limit = TIMEOUTS["vcluster-ready"]
        healthy, why = await asyncio.to_thread(control_plane_health)

        _, pods = await run(
            ["kubectl", "get", "pods", "-n", ns, "-o", "wide"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        _, events = await run(
            ["kubectl", "events", "-n", ns],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        detail = f"Pods in {ns}:\n{_tail(pods, 10)}\nRecent events:\n{_tail(events, 15)}"

        if not healthy:
            return (
                f"your private cluster (vCluster) was created, but could not start "
                f"within {limit}s because the platform's own cluster is not "
                f"working right now.\n\n{why}\n\n"
                f"Once it recovers, press Retry — this resumes from this step and "
                f"keeps everything already done.\n\n{detail}"
            )
        return (
            f"the vCluster control plane did not become ready in {limit}s. The "
            f"host control plane is healthy, so the cause is in this project — "
            f"the pod status and events below say what it was waiting for.\n\n"
            f"{detail}"
        )

    async def _namespace_stuck(self, ns: str) -> str:
        """Why a namespace is still Terminating, from the two places that know.

        THE CONTROL PLANE FIRST. A stuck namespace used to be reported as
        "something in it has a finalizer", which is one cause of three and was
        the wrong one on 2026-09-29: the namespace controller was not running at
        all, so the namespace had NO conditions and its PVCs were still Bound.

        THEN THE NAMESPACE'S OWN CONDITIONS. When the controller IS running and
        still cannot finish, it says exactly what is left —
        NamespaceContentRemaining names the resources, NamespaceFinalizersRemaining
        names the finalizers — and that is the line a person needs.

        Both end with the same instruction, because both have the same way out:
        teardown is idempotent, so pressing Delete again once the cause is gone
        re-runs every step and finishes.
        """
        healthy, why = await asyncio.to_thread(control_plane_health)
        again = (
            "Nothing is left half-removed in a way that matters: the deletion is "
            "queued. Press Delete again once this is resolved — teardown re-runs "
            "every step, skips what is already gone, and finishes."
        )
        if not healthy:
            return (
                f"namespace {ns} is still terminating, and the reason is the "
                f"platform rather than your project: the component that empties a "
                f"namespace is not running.\n\n{why}\n\n{again}"
            )

        _, conditions = await run(
            ["kubectl", "get", "namespace", ns, "-o",
             "jsonpath={range .status.conditions[*]}{.type}={.status}: {.message}{\"\\n\"}{end}"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        # Only the conditions that are TRUE describe something blocking; the
        # "…Failure=False" ones say a class of problem did NOT happen.
        blocking = [
            line for line in conditions.splitlines()
            if "=True:" in line
        ]
        if blocking:
            return (
                f"namespace {ns} is still terminating after 180s. Kubernetes says "
                f"what is left:\n" + "\n".join(f"  {line}" for line in blocking)
                + f"\n\n{again}"
            )
        return (
            f"namespace {ns} is still terminating after 180s. The control plane is "
            f"healthy and the namespace reports no blocking condition, so it is "
            f"most likely still working through its contents.\n\n{again}"
        )

    async def _wait_established(self, ctx: StepContext, kubeconfig: Path, crd: str) -> None:
        """Block until the API server has registered a freshly-applied CRD.

        POLLED, NOT `kubectl wait`. Two separate reasons, and the second is why the
        obvious command does not work here:

          1. `kubectl apply` on a CRD returns once the object is stored. The API
             server still has to register the type and refresh discovery, and a
             custom resource created inside that gap fails with
             `no matches for kind` — an error that reads like a missing CRD rather
             than a race.
          2. `kubectl wait --for condition=established` FAILS OUTRIGHT in that same
             gap, with `.status.conditions accessor error: <nil> is of the type
             <nil>, expected []interface{}` — because the status subresource has
             not been populated at all yet. It waits for a condition to become
             true; it does not wait for the conditions list to exist.

        So this polls the condition directly and treats "no status yet" as "not
        ready yet", which is what was meant all along.
        """
        budget = TIMEOUTS["crd-established"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget

        # TWO FAILURES THAT LOOK IDENTICAL, AND MUST NOT.
        #
        # The first version ran this poll with `check=False` and compared the
        # output to "True". A kubectl that FAILED — because the vCluster's API
        # server was not answering — produced empty output, which is exactly what
        # "not established yet" produces. So an unreachable control plane was
        # reported as a CRD that would not establish, and the message sent the
        # reader off to look at the CRD.
        #
        # Observed for real: a tenant's kube-controller-manager lost its leader
        # election under load and the pod restarted three times. Every restart is
        # tens of seconds of `TLS handshake timeout` on this poll. The step failed
        # saying the CRD "never became Established" while the CRD was fine and the
        # control plane was crash-looping — a diagnosis that costs somebody an hour.
        #
        # So count the two outcomes separately, and let the message say which one
        # actually happened.
        unreachable = 0
        polls = 0
        last_error = ""

        while True:
            rc, out = await run(
                ["kubectl", f"--kubeconfig={kubeconfig}", "get", "crd", crd, "-o",
                 'jsonpath={.status.conditions[?(@.type=="Established")].status}'],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            polls += 1
            if rc == 0 and out.strip() == "True":
                if unreachable:
                    # Worth saying even on success: it means the control plane
                    # wobbled, which predicts trouble in the steps after this one.
                    ctx.emit(
                        f"{crd} is established (the tenant's API server was "
                        f"unreachable for {unreachable} of {polls} checks — its "
                        f"control plane is restarting under load)"
                    )
                else:
                    ctx.emit(f"{crd} is established")
                return

            if rc != 0:
                unreachable += 1
                last_error = _tail(out, 2)

            if loop.time() > deadline:
                if unreachable > polls / 2:
                    raise StepFailed(
                        f"the tenant's own API server was unreachable for "
                        f"{unreachable} of {polls} checks over {budget}s, so "
                        f"{crd} could not be confirmed.\n"
                        f"THE CRD IS PROBABLY FINE — the vCluster control plane is "
                        f"not staying up. Check it with:\n"
                        f"    kubectl get pod {ctx.project}-0 -n p-{ctx.project}\n"
                        f"    kubectl logs {ctx.project}-0 -n p-{ctx.project} --previous | tail -20\n"
                        f"A control plane that loses its leader election and exits "
                        f"is starved rather than broken: too many tenants for this "
                        f"machine, or another project mid-deploy. Remove a project "
                        f"you have finished with, then retry — this step is "
                        f"idempotent and a retry resumes here.\n"
                        f"Last error: {last_error}"
                    )
                raise StepFailed(
                    f"{crd} was applied but never became Established within "
                    f"{budget}s, and the tenant's API server answered throughout "
                    f"({polls} checks). This is the CRD itself, not the connection."
                )
            await asyncio.sleep(2)

    async def _wait_tenant_api(self, ctx: StepContext, kubeconfig: Path) -> None:
        """Block until the tenant's API answers through `kubeconfig`, reliably.

        THE RACE THIS CLOSES. `vcluster connect --print` starts a background
        proxy container, writes a kubeconfig pointing at it, and RETURNS — before
        the proxy is forwarding anything. Docker accepts connections on the
        published port straight away and the proxy resets them until its own
        port-forward is up. On 2026-09-29 that took 44 seconds, and Studio's
        first `kubectl apply` landed inside them: two resets, one second before
        the proxy logged "Forwarding from 0.0.0.0:8443", and the step failed on
        a vCluster that was working perfectly.

        THREE IN A ROW, NOT ONE. A freshly-started port-forward answers one
        request and drops the next more often than it does neither, so a single
        success proves less than it looks. Three consecutive answers two seconds
        apart is cheap on a quiet machine (~6s) and is what "ready" means here.
        """
        loop = asyncio.get_running_loop()
        budget = TIMEOUTS["tenant-api"]
        deadline = loop.time() + budget
        started = loop.time()
        streak, attempts, last = 0, 0, ""
        while True:
            attempts += 1
            rc, out = await run(
                ["kubectl", f"--kubeconfig={kubeconfig}", "get", "--raw=/readyz",
                 "--request-timeout=10s"],
                timeout=20, check=False,
            )
            if rc == 0 and out.strip() == "ok":
                streak += 1
                if streak >= 3:
                    waited = loop.time() - started
                    if attempts > 3:
                        ctx.emit(
                            f"your cluster's API is answering (after {waited:.0f}s — "
                            f"its connection took {attempts - 3} extra tries to come up)"
                        )
                    else:
                        ctx.emit("your cluster's API is answering")
                    return
            else:
                streak = 0
                last = _tail(out, 2)
            if loop.time() > deadline:
                raise StepFailed(
                    f"your private cluster (vCluster) is running, but it could not "
                    f"be reached through its connection for {budget}s, so its KServe "
                    f"API could not be installed.\nLast error: {last}\n"
                    f"Press Retry — this step is idempotent and resumes here."
                )
            await asyncio.sleep(2)

    async def _tenant_apply(
        self, ctx: StepContext, kubeconfig: Path, path: Path, label: str
    ) -> None:
        """`kubectl apply --server-side` into the tenant's cluster, surviving drops.

        Retried ONLY for a dropped connection (see _is_transient). Server-side
        apply is idempotent — the same object applied twice is the same object —
        so retrying after a reset cannot double-create anything; it only finishes
        what the reset interrupted. A request the server REJECTED fails at once,
        with the server's own words.
        """
        attempts = 5
        for attempt in range(1, attempts + 1):
            rc, out = await run(
                ["kubectl", f"--kubeconfig={kubeconfig}", "apply",
                 "--server-side", "--force-conflicts", "-f", str(path)],
                timeout=TIMEOUTS["apply"], emit=ctx.emit, label=label, check=False,
            )
            if rc == 0:
                return
            if not _is_transient(out) or attempt == attempts:
                raise StepFailed(f"{label} failed (exit {rc}):\n{_tail(out)}")
            delay = 2 * attempt
            ctx.emit(
                f"  {label}: the connection to your cluster dropped "
                f"(attempt {attempt}/{attempts}) — retrying in {delay}s"
            )
            await asyncio.sleep(delay)

    async def _seed_tenant_api(self, ctx: StepContext) -> None:
        """Copy the KServe API surface into the tenant's own cluster.

        Two CRDs and the runtime catalogue:

          inferenceservices.serving.kserve.io   so the tenant can AUTHOR one
          clusterservingruntimes.serving.kserve.io + its objects, so
                                                `modelFormat: sklearn` resolves to
                                                the same runtime the host would
                                                pick, and a tenant can see what is
                                                on offer

        This is Fallback A's job, done inline for the same reason the isvc mirror
        is (see the module header): the script's tenant list is hard-coded and it
        is a daemon. READ-ONLY IS BY CONVENTION here exactly as it is there — a PI
        is cluster-admin inside their own vCluster and can edit these copies.

        `--server-side` is not optional: the KServe CRDs are far larger than the
        262144-byte limit on the client-side last-applied annotation.
        """
        kubeconfig = await self._ensure_kubeconfig(ctx)
        # Before the first request through it — see _wait_tenant_api.
        await self._wait_tenant_api(ctx, kubeconfig)

        # SCRATCH, NOT tenants/<project>/rendered/. These are verbatim copies of
        # host objects — the InferenceService CRD alone is ~700 KB — identical for
        # every tenant and already vendored in host/serving/kserve/. Writing them
        # into the tenant's rendered/ directory would put three quarters of a
        # megabyte of duplicated upstream schema in git per project.
        #
        # Invariant (a) is about THIS TENANT'S manifests being reviewable and
        # diffable: the namespace pack, the seed Job, the InferenceService. Copies
        # of cluster-wide API definitions are plumbing that makes the tenant's
        # cluster usable, not configuration anyone will review per project.
        out_dir = Path(tempfile.mkdtemp(prefix=f"studio-seed-{ctx.project}-"))

        for crd in ("inferenceservices.serving.kserve.io",
                    "clusterservingruntimes.serving.kserve.io"):
            _, raw = await run(["kubectl", "get", "crd", crd, "-o", "json"],
                               timeout=TIMEOUTS["kubectl"], label=f"read {crd}")
            doc = json.loads(raw)
            # Server-assigned fields cannot be applied into another cluster.
            for key in ("resourceVersion", "uid", "creationTimestamp", "generation",
                        "managedFields", "selfLink"):
                doc.get("metadata", {}).pop(key, None)
            doc.pop("status", None)
            path = out_dir / f"crd-{crd.split('.')[0]}.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            await self._tenant_apply(ctx, kubeconfig, path, f"seed {crd}")
            await self._wait_established(ctx, kubeconfig, crd)

        # The runtime catalogue itself. Authored on the host, so no tenant can
        # change what another tenant's `modelFormat: sklearn` resolves to.
        _, raw = await run(
            ["kubectl", "get", "clusterservingruntimes", "-o", "json"],
            timeout=TIMEOUTS["kubectl"], label="read runtime catalogue",
        )
        catalogue = json.loads(raw)
        cleaned = []
        for item in catalogue.get("items", []):
            meta = item.get("metadata", {})
            for key in ("resourceVersion", "uid", "creationTimestamp", "generation",
                        "managedFields", "selfLink"):
                meta.pop(key, None)
            meta.setdefault("labels", {})["hive.ksl/managed-by"] = "studio-executor"
            meta["labels"]["hive.ksl/read-only"] = "true"
            item.pop("status", None)
            cleaned.append(item)

        bundle = out_dir / "runtimes.json"
        bundle.write_text(
            json.dumps({"apiVersion": "v1", "kind": "List", "items": cleaned}), encoding="utf-8"
        )
        await self._tenant_apply(ctx, kubeconfig, bundle, "seed runtime catalogue")
        ctx.emit(f"seeded 2 CRDs and {len(cleaned)} serving runtimes into the tenant's cluster")
        shutil.rmtree(out_dir, ignore_errors=True)

    async def inference_service(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("inference-service")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        # Self-healing guard. The API is seeded by the vcluster step, which a RETRY
        # skips because it already succeeded — so a run that failed here for want
        # of the CRD could never recover on its own. Checking is one cheap call;
        # seeding is idempotent (server-side apply).
        kubeconfig = await self._ensure_kubeconfig(ctx)
        rc, _ = await run(
            ["kubectl", f"--kubeconfig={kubeconfig}", "get", "crd",
             "inferenceservices.serving.kserve.io", "-o", "name"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        if rc != 0:
            ctx.emit("the tenant cluster has no KServe API yet — seeding it now")
            await self._seed_tenant_api(ctx)

        path = await self._write_rendered(ctx, "inference-service")

        # 1. THE TENANT IS THE AUTHOR. Applied inside their own control plane,
        #    which is the promise the platform makes (CLAUDE.md §2.1).
        ctx.emit("applying inside the tenant's own cluster")
        await self._vc(ctx, ["kubectl", "apply", "-f", str(path)],
                       timeout=TIMEOUTS["isvc-apply"], label="apply isvc inside vCluster")

        # 2. MIRROR TO THE HOST, where the one central KServe reconciles it.
        #    scripts/sync-isvc.sh would normally do this and cannot: its tenant
        #    list is hard-coded to inf-101/inf-103. See the module header.
        synced = self._synced_name(ctx.project)
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["metadata"]["name"] = synced
        doc["metadata"]["namespace"] = ns
        labels = doc["metadata"].setdefault("labels", {})
        labels["hive.ksl/project"] = ctx.project
        labels["hive.ksl/managed-by"] = "studio-executor"
        labels["hive.ksl/virtual-name"] = ctx.project

        mirror = rendered_dir(ctx.project) / "inference-service.host.yaml"
        mirror.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        ctx.emit(f"mirroring to host as {ns}/{synced}")
        await run(["kubectl", "apply", "-f", str(mirror)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply host mirror")

        return StepResult(ok=True, log=f"authored in-cluster, mirrored to {ns}/{synced}")

    async def ready_check(self, ctx: StepContext) -> StepResult:
        _raise_if_injected("ready-check")
        ns = self._host_ns(ctx.project)

        # DISCOVER the host object by label rather than trusting the constructed
        # name (fallback B of the ladder, adopted up front — the label is already
        # in the rendered manifest, so discovering by it costs nothing and cannot
        # drift the way a hand-assembled identifier can).
        _, found = await run(
            ["kubectl", "get", "isvc", "-n", ns,
             "-l", f"hive.ksl/project={ctx.project}",
             "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        names = found.split()
        if not names:
            raise StepFailed(
                f"no InferenceService labelled hive.ksl/project={ctx.project} in {ns}. "
                f"The mirror step did not produce one."
            )
        synced = names[0]
        ctx.emit(f"host object: {ns}/{synced}")

        ctx.emit(f"waiting for Ready (up to {TIMEOUTS['ready']}s — the first pull of a runtime image is slow)")
        deadline = asyncio.get_running_loop().time() + TIMEOUTS["ready"]
        url = ""
        while True:
            _, ready = await run(
                ["kubectl", "get", "isvc", synced, "-n", ns, "-o",
                 'jsonpath={.status.conditions[?(@.type=="Ready")].status}'],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            if ready.strip() == "True":
                break
            if asyncio.get_running_loop().time() > deadline:
                _, describe = await run(
                    ["kubectl", "get", "isvc", synced, "-n", ns, "-o",
                     "jsonpath={range .status.conditions[*]}{.type}={.status} {.reason} {.message}{\"\\n\"}{end}"],
                    timeout=TIMEOUTS["kubectl"], check=False,
                )
                raise StepFailed(
                    f"the service did not become Ready in {TIMEOUTS['ready']}s.\n"
                    f"Conditions:\n{_tail(describe, 12)}"
                )
            await asyncio.sleep(6)

        # READ .status.url LIVE. Never construct it: locally KServe publishes a
        # `-predictor` segment that production will not have (DEVIATIONS.md), so
        # anything that assembles a hostname by hand breaks at migration.
        _, url = await run(
            ["kubectl", "get", "isvc", synced, "-n", ns, "-o", "jsonpath={.status.url}"],
            timeout=TIMEOUTS["kubectl"],
        )
        url = url.strip()
        ctx.emit(f"endpoint (read live): {url}")

        kubeconfig = await self._ensure_kubeconfig(ctx)

        # Mirror the host's verdict back INTO the tenant's cluster as annotations,
        # the same contract scripts/sync-isvc.sh offers the hand-built tenants, so
        # a PI can see readiness from inside their own cluster with no host access.
        await self._vc(
            ctx,
            ["kubectl", "annotate", "isvc", ctx.project, "--overwrite",
             f"hive.ksl/host-name={synced}",
             f"hive.ksl/host-namespace={ns}",
             "hive.ksl/host-ready=True",
             f"hive.ksl/host-url={url}"],
            timeout=TIMEOUTS["kubectl"], label="annotate virtual isvc",
        )

        outputs = {
            "endpointUrl": url,
            "syncedName": synced,
            "namespace": ns,
            "kubeconfigAvailable": True,
            "kubeconfigPath": str(kubeconfig.relative_to(repo_root())),
        }
        # A RAG project has TWO endpoints and its template says so in `outputs`.
        # Keyed on that declaration rather than on the template id, so a future
        # template that also wants the raw model surfaced gets it by naming the
        # output rather than by being added to a list in here.
        #
        # It is the SAME url under a second name, and that is the point: the
        # `rag-api` step reads it from the context to build the Host header, and
        # the project page shows it so a PI debugging a bad answer can ask the
        # model directly, without retrieval in front of it.
        if "chatEndpointUrl" in (ctx.template.spec.outputs or []):
            outputs["chatEndpointUrl"] = url

        return StepResult(ok=True, log=f"Ready. endpoint={url}", outputs=outputs)

    # ------------------------------------------------------------------
    # RAG STACK
    # ------------------------------------------------------------------

    async def vector_db(self, ctx: StepContext) -> StepResult:
        """Qdrant: one Deployment, one Service, in the HOST namespace."""
        _raise_if_injected("vector-db")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        path = await self._write_rendered(ctx, self._fragment(ctx, "vector-db"))
        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply qdrant")

        deployment = f"{ctx.project}-qdrant"
        ctx.emit(f"waiting for deploy/{deployment} to roll out")
        rc, out = await run(
            ["kubectl", "rollout", "status", f"deploy/{deployment}", "-n", ns,
             f"--timeout={TIMEOUTS['rollout']}s"],
            timeout=TIMEOUTS["rollout"] + 30, emit=ctx.emit, check=False,
        )
        if rc != 0:
            # A rollout that does not converge says almost nothing in its own
            # output. The pod's events are where the reason lives — and for this
            # workload the likely reasons are a Pending PVC or a quota refusal,
            # neither of which produces a pod log to read.
            _, events = await run(
                ["kubectl", "get", "events", "-n", ns, "--sort-by=.lastTimestamp"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            raise StepFailed(
                f"the vector database did not start.\n{_tail(out, 6)}\n"
                f"Recent events:\n{_tail(events, 15)}"
            )

        # The rollout being complete means the pod passed its readiness probe,
        # which for Qdrant is /readyz — "all shards are ready", not merely
        # "the process started". So there is nothing further to poll here.
        ctx.emit(f"{deployment} is ready; svc/{deployment} listens on 6333 (http) and 6334 (grpc)")
        return StepResult(
            ok=True,
            log=f"Qdrant running in {ns} as deploy/{deployment}",
            outputs={"vectorDbService": f"{deployment}.{ns}.svc.cluster.local:6333"},
        )

    def _corpus_source(self, ctx: StepContext) -> tuple[Path, str]:
        """Where the PI's documents are, and how to describe that in the log.

        Returns (directory, description). Falls back to the committed demo
        corpus when no upload was made, so the template can be demonstrated end
        to end without anyone having to find documents first — and SAYS SO,
        because a project silently indexing documents the PI did not provide
        would be the worst kind of helpful.
        """
        upload = str(ctx.params.get("corpusUploadId", "") or "").strip()
        if not upload:
            demo = repo_root() / "templates" / "catalog" / "rag-stack" / "demo-corpus"
            return demo, "the built-in demo corpus (no documents were uploaded)"

        # THE SAME DIRECTORY main.py STAGED INTO, from the same function. A
        # second `repo_root() / "tenants" / "_uploads"` here would ignore the
        # STUDIO_UPLOADS override and fall back to the demo corpus without
        # reporting anything.
        base = uploads_root()
        # DEFENCE IN DEPTH AGAINST PATH TRAVERSAL. The parameter's pattern
        # already restricts it to [a-z0-9-]{0,40} and validation.py enforces
        # that server-side, so "../.." cannot get this far. This is the check
        # that still holds if some future caller bypasses validation: resolve
        # the joined path and refuse anything that escapes the uploads
        # directory. An id is a name, and it is checked as one.
        candidate = (base / upload).resolve()
        if not str(candidate).startswith(str(base.resolve()) + os.sep):
            raise StepFailed(
                f"upload id {upload!r} does not name a directory inside "
                f"tenants/_uploads/. Refusing to copy from it."
            )
        if not candidate.is_dir():
            raise StepFailed(
                f"no uploaded documents found for id {upload!r} "
                f"(expected tenants/_uploads/{upload}/). The upload may have "
                f"been cleaned up; upload again and redeploy."
            )
        return candidate, f"tenants/_uploads/{upload}/"

    async def corpus_upload(self, ctx: StepContext) -> StepResult:
        """Get the PI's documents onto the `scratch` PVC.

        A PVC is not writable from outside the cluster, and `kubectl cp` copies
        into a CONTAINER rather than into a volume — so this starts a throwaway
        pod with `scratch` mounted, copies into it, verifies, and deletes it.
        The pod's whole lifetime is this step.
        """
        _raise_if_injected("corpus-upload")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        source, description = self._corpus_source(ctx)
        local_files = sorted(
            f.name for f in source.iterdir()
            if f.is_file() and not f.name.startswith(".")
        )
        if not local_files:
            raise StepFailed(f"{description} contains no files to index")
        ctx.emit(f"corpus source: {description} ({len(local_files)} file(s))")

        pod = f"{ctx.project}-stage"
        path = await self._write_rendered(ctx, self._fragment(ctx, "corpus-upload"))

        # A Pod's spec is immutable, so re-applying a leftover one fails. It is
        # ephemeral by design, so deleting first is always right.
        await run(["kubectl", "delete", "pod", pod, "-n", ns, "--ignore-not-found",
                   "--wait=true", "--timeout=60s"],
                  timeout=90, emit=ctx.emit, check=False)

        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply staging pod")

        ctx.emit(f"waiting for pod/{pod} to be Ready")
        rc, out = await run(
            ["kubectl", "wait", "--for=condition=Ready", f"pod/{pod}", "-n", ns,
             f"--timeout={TIMEOUTS['pod-ready']}s"],
            timeout=TIMEOUTS["pod-ready"] + 30, emit=ctx.emit, check=False,
        )
        if rc != 0:
            _, events = await run(
                ["kubectl", "get", "events", "-n", ns, "--sort-by=.lastTimestamp"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            raise StepFailed(
                f"the staging pod did not start, so there is nowhere to copy the "
                f"documents to.\n{_tail(out, 4)}\nRecent events:\n{_tail(events, 12)}"
            )

        # `<dir>/.` copies the CONTENTS of the directory rather than the
        # directory itself. Without the trailing `/.` the corpus lands at
        # /mnt/scratch/corpus/<dirname>/, one level too deep, and the ingestion
        # Job — which walks recursively — would still find it, but every
        # chunk's `source` would be right while CORPUS_DIR's own listing (used
        # to report skipped files) would show a directory and no files.
        #
        # kubectl cp needs `tar` INSIDE the target container; busybox has it.
        ctx.emit(f"copying {len(local_files)} file(s) into {pod}:/mnt/scratch/corpus")
        await run(
            ["kubectl", "cp", f"{source}/.", f"{ns}/{pod}:/mnt/scratch/corpus"],
            timeout=TIMEOUTS["cp"], emit=ctx.emit, label="kubectl cp corpus",
        )

        # VERIFY INSIDE THE POD. `kubectl cp` can exit 0 having copied nothing —
        # it is a tar stream over exec, and a partial or empty stream is not
        # always an error. The only trustworthy evidence is the file list on the
        # volume, so it is read back and compared.
        _, listing = await run(
            ["kubectl", "exec", pod, "-n", ns, "--", "ls", "-1", "/mnt/scratch/corpus"],
            timeout=TIMEOUTS["kubectl"], label="verify the copied corpus",
        )
        copied = sorted(name for name in listing.split() if name)
        missing = [name for name in local_files if name not in copied]
        if missing:
            raise StepFailed(
                f"the copy did not land: {len(missing)} of {len(local_files)} file(s) "
                f"are missing from the volume ({', '.join(missing[:5])}). "
                f"Found instead: {', '.join(copied[:10]) or '<nothing>'}"
            )
        ctx.emit(f"verified on the volume: {', '.join(copied[:10])}")

        # Delete it here, in the step that created it, rather than at teardown.
        # It holds the ReadWriteOnce `scratch` claim, and the ingestion Job needs
        # that claim next.
        await run(["kubectl", "delete", "pod", pod, "-n", ns, "--ignore-not-found",
                   "--wait=true", "--timeout=60s"],
                  timeout=90, emit=ctx.emit, check=False)
        ctx.emit("staging pod removed")

        return StepResult(
            ok=True,
            log=f"{len(copied)} file(s) staged from {description}",
            outputs={"corpusFiles": len(copied), "corpusSource": description},
        )

    async def ingestion(self, ctx: StepContext) -> StepResult:
        """Run the indexing Job and read its one machine-readable result line."""
        _raise_if_injected("ingestion")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        job = f"{ctx.project}-ingest"
        path = await self._write_rendered(ctx, self._fragment(ctx, "ingestion"))

        # A completed Job's pod template is immutable, so re-applying an
        # existing one fails. ingest.py recreates the collection, so re-running
        # is safe and produces the same index rather than a doubled one.
        rc, _ = await run(["kubectl", "get", "job", job, "-n", ns],
                          timeout=TIMEOUTS["kubectl"], check=False)
        if rc == 0:
            ctx.emit(f"job/{job} exists — deleting it so the manifest can be reapplied")
            await run(["kubectl", "delete", "job", job, "-n", ns, "--wait=true"],
                      timeout=TIMEOUTS["kubectl"], emit=ctx.emit, check=False)

        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply ingestion job")

        ctx.emit(
            f"indexing (up to {TIMEOUTS['ingest-wait']}s — every passage is embedded on CPU)"
        )
        rc, out = await run(
            ["kubectl", "wait", "--for=condition=complete", f"job/{job}", "-n", ns,
             f"--timeout={TIMEOUTS['ingest-wait']}s"],
            timeout=TIMEOUTS["ingest-wait"] + 30, emit=ctx.emit, check=False,
        )

        # The log is read WHETHER OR NOT the wait succeeded. On failure it is
        # the only place the reason exists — an unreadable corpus, a missing
        # embedding cache, a Qdrant that never came up — and ingest.py is
        # written to say which.
        _, logs = await run(["kubectl", "logs", f"job/{job}", "-n", ns, "--tail=100"],
                            timeout=TIMEOUTS["kubectl"], check=False)
        if rc != 0:
            raise StepFailed(
                f"the indexing Job did not complete.\n{_tail(out, 6)}\n"
                f"--- job log ---\n{_tail(logs, 25)}"
            )

        result = _parse_ingest_result(logs)
        if result is None:
            raise StepFailed(
                f"the indexing Job completed but printed no {INGEST_RESULT_PREFIX} "
                f"line, so how much was indexed is unknown. Treating that as a "
                f"failure rather than reporting an index of unknown size.\n"
                f"--- job log ---\n{_tail(logs, 20)}"
            )

        ctx.emit(
            f"indexed {result['chunks']} passage(s) from {result['files']} file(s) "
            f"into '{result['collection']}' ({result['dims']} dimensions)"
        )
        return StepResult(
            ok=True,
            log=(
                f"{result['chunks']} passages from {result['files']} files indexed "
                f"into {result['collection']}"
            ),
            outputs={
                "indexedChunks": result["chunks"],
                "indexedFiles": result["files"],
                "collectionName": result["collection"],
                "embeddingDims": result["dims"],
            },
        )

    async def _cluster_local_gateway(self, ctx: StepContext) -> str:
        """Knative's cluster-local gateway, DISCOVERED rather than assumed.

        `kourier-system` holds two Services and the difference matters: `kourier`
        is the external LoadBalancer and `kourier-internal` is the ClusterIP one
        that in-cluster callers use. Picking the wrong one gives a RAG service
        that either cannot resolve its chat model or reaches it by the path
        production will not use.

        Discovered by TYPE — the ClusterIP service in kourier-system — rather
        than by name, because the name is a Kourier packaging detail and the type
        is the property that makes it the right one. The verified value today is
        `kourier-internal`; the log records what was actually found.
        """
        _, raw = await run(
            ["kubectl", "get", "svc", "-n", "kourier-system", "-o", "json"],
            timeout=TIMEOUTS["kubectl"], label="find the cluster-local gateway",
        )
        try:
            items = json.loads(raw).get("items", [])
        except json.JSONDecodeError as exc:
            raise StepFailed(f"could not read the services in kourier-system: {exc}") from exc

        candidates = [
            item["metadata"]["name"]
            for item in items
            if item.get("spec", {}).get("type") == "ClusterIP"
        ]
        if not candidates:
            raise StepFailed(
                "no ClusterIP Service in kourier-system, so there is no "
                "cluster-local gateway for the RAG service to call the model "
                "through. Found: "
                + (", ".join(i["metadata"]["name"] for i in items) or "<none>")
            )
        # Prefer the conventional name when it is present, so the common case is
        # deterministic rather than dependent on listing order.
        name = "kourier-internal" if "kourier-internal" in candidates else candidates[0]
        ctx.emit(f"cluster-local gateway: svc/{name} in kourier-system")
        return f"http://{name}.kourier-system.svc.cluster.local"

    async def rag_api(self, ctx: StepContext) -> StepResult:
        """The retrieval service, wired to the chat model that already exists."""
        _raise_if_injected("rag-api")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        # The chat model's hostname, produced by ready-check, which read
        # `.status.url` LIVE. NEVER CONSTRUCTED — local URLs carry a
        # `-predictor` segment production will not have (DEVIATIONS.md), so a
        # hostname assembled here would break at migration.
        url = str(ctx.outputs.get("chatEndpointUrl") or ctx.outputs.get("endpointUrl") or "")
        if not url:
            raise StepFailed(
                "the chat model's URL is not known, so the RAG service cannot be "
                "pointed at it. That URL is produced by the ready-check step; a "
                "run that reaches here without it has a broken step order."
            )
        chat_host = url.replace("https://", "").replace("http://", "").split("/")[0]
        chat_endpoint = await self._cluster_local_gateway(ctx)
        ctx.emit(f"chat model: Host: {chat_host} via {chat_endpoint}")

        path = await self._write_rendered(
            ctx,
            self._fragment(ctx, "rag-api"),
            extras={"chat_host": chat_host, "chat_endpoint": chat_endpoint},
        )
        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply rag-api")

        deployment = f"{ctx.project}-rag"
        ctx.emit(f"waiting for deploy/{deployment} to roll out")
        rc, out = await run(
            ["kubectl", "rollout", "status", f"deploy/{deployment}", "-n", ns,
             f"--timeout={TIMEOUTS['rollout']}s"],
            timeout=TIMEOUTS["rollout"] + 30, emit=ctx.emit, check=False,
        )
        if rc != 0:
            # This pod's readiness probe is /healthz, which checks Qdrant AND the
            # embedding cache — so a rollout that never converges usually means
            # one of those, and the pod's own log says which.
            _, logs = await run(
                ["kubectl", "logs", f"deploy/{deployment}", "-n", ns, "--tail=40"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            raise StepFailed(
                f"the RAG service did not become ready.\n{_tail(out, 6)}\n"
                f"--- pod log ---\n{_tail(logs, 20)}"
            )

        return StepResult(
            ok=True,
            log=f"RAG service running in {ns} as deploy/{deployment}",
            outputs={"chatHost": chat_host, "chatEndpoint": chat_endpoint},
        )

    async def rag_ready_check(self, ctx: StepContext) -> StepResult:
        """Health, then ONE REAL QUESTION through the whole stack.

        THE SMOKE QUESTION IS THE POINT OF THIS STEP. Everything before it can
        succeed with a project that still cannot answer anything: Qdrant up but
        empty, the service ready but unable to reach the model, the model asleep
        and slow to wake. Asking one question exercises embed -> search ->
        prompt -> gateway -> model -> answer, end to end, and records the result
        in the step log.

        It also means THE PI'S FIRST CLICK IS NOT THE FIRST REAL REQUEST. On an
        on-demand project the first question pays a cold start; paying it here,
        while a progress bar is already on screen, is far better than paying it
        in the playground where it looks like the endpoint is broken.
        """
        _raise_if_injected("rag-ready-check")
        ns = self._host_ns(ctx.project)
        service = f"{ctx.project}-rag"
        internal = f"http://{service}.{ns}.svc.cluster.local:8080"

        lines: list[str] = []
        async with dataplane.temporary_forward(ns, service, 8080, "/healthz") as forward:
            ctx.emit(f"opening a temporary port-forward to svc/{service}")
            try:
                port = await forward.ensure()
            except RuntimeError as exc:
                raise StepFailed(
                    f"could not reach the RAG service to verify it: {exc}"
                ) from exc

            base = f"http://127.0.0.1:{port}"

            # --- 1. healthy? -------------------------------------------
            deadline = asyncio.get_running_loop().time() + TIMEOUTS["rag-ready"]
            detail = ""
            while True:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.get(f"{base}/healthz")
                    if response.status_code == 200:
                        break
                    detail = f"HTTP {response.status_code}: {response.text[:300]}"
                except httpx.HTTPError as exc:
                    detail = str(exc)
                if asyncio.get_running_loop().time() > deadline:
                    raise StepFailed(
                        f"the RAG service did not report healthy within "
                        f"{TIMEOUTS['rag-ready']}s. /healthz checks that Qdrant is "
                        f"reachable and the embedding model is loaded.\nLast: {detail}"
                    )
                await asyncio.sleep(5)

            ctx.emit("/healthz is 200 — Qdrant reachable and the embedder loaded")
            lines.append("health: OK")

            # --- 2. one real question ----------------------------------
            question = "What is this collection of documents about?"
            ctx.emit(f"smoke question: {question!r} (this also wakes the model)")
            try:
                # The chat timeout inside the service is 180s for a cold start;
                # this must be longer or it would time out on the wire while the
                # answer was still coming.
                async with httpx.AsyncClient(timeout=200.0) as client:
                    response = await client.post(
                        f"{base}/chat", json={"question": question}
                    )
            except httpx.HTTPError as exc:
                raise StepFailed(f"the smoke question could not be sent: {exc}") from exc

            if response.status_code != 200:
                raise StepFailed(
                    f"the smoke question failed with HTTP {response.status_code}:\n"
                    f"{response.text[:600]}"
                )

            try:
                body = response.json()
                answer = str(body.get("answer", "")).strip()
                sources = body.get("sources") or []
            except ValueError as exc:
                raise StepFailed(
                    f"the RAG service answered with something that is not JSON: "
                    f"{response.text[:300]}"
                ) from exc

            if not answer:
                raise StepFailed("the RAG service returned an empty answer")

            # RECORDED IN THE LOG, NOT ASSERTED. Whether the answer is GOOD is a
            # judgement about a model, and a pipeline step must not fail a
            # correctly-built project because a 0.5B model gave a weak answer.
            # What is asserted is that the round trip works and cited something.
            ctx.emit(f"answered in {body.get('latencyMs', '?')}ms from {len(sources)} source(s)")
            ctx.emit(f"answer: {answer[:300]}")
            lines.append(f"smoke question answered from {len(sources)} source(s)")
            lines.append(f"answer: {answer[:300]}")
            if not sources:
                # Not a failure: an empty index answers honestly and correctly.
                # But it is worth saying loudly, because the likeliest cause is a
                # corpus that produced nothing useful.
                ctx.emit(
                    "note: the answer cited NO sources. The index may be empty or "
                    "the question may genuinely not be covered by these documents."
                )

        return StepResult(
            ok=True,
            log="\n".join(lines),
            outputs={
                "ragEndpointUrl": internal,
                "smokeOk": True,
                "smokeSources": len(sources),
            },
        )

    # ------------------------------------------------------------------
    # BATCH INFERENCE
    # ------------------------------------------------------------------

    def _dataset_source(self, ctx: StepContext) -> tuple[Path, str]:
        """The file to copy in: the PI's upload, or a bundled sample.

        THE PATH IS RESOLVED AND CHECKED, not joined and trusted. `datasetUploadId`
        reaches here from a submitted form; validation.py constrains its shape and
        the template states the pattern, and this is the third check — the one
        that runs immediately before the path is used. A resolved path that is not
        under tenants/_uploads/ is refused rather than copied from.
        """
        upload = (ctx.params.get("datasetUploadId") or "").strip()
        if upload:
            base = uploads_root()
            candidate = (base / upload).resolve()
            if not str(candidate).startswith(str(base.resolve())):
                raise StepFailed(
                    f"the dataset reference {upload!r} resolves outside "
                    f"tenants/_uploads/. Refusing to copy from it."
                )
            if not candidate.is_dir():
                raise StepFailed(
                    f"no staged dataset for id {upload!r} (expected "
                    f"tenants/_uploads/{upload}/). The upload may have been "
                    f"cleaned up; add the file again."
                )
            files = [f for f in candidate.iterdir() if f.is_file() and not f.name.startswith(".")]
            if not files:
                raise StepFailed(f"tenants/_uploads/{upload}/ contains no file to run over")
            return files[0], f"your upload ({files[0].name})"

        # THE BUNDLED SAMPLE. Which one depends on the model's interface, because
        # the two samples demonstrate different things: abstracts for a chat
        # model, iris for a classifier. Picking by interface rather than asking
        # again means "No dataset? Use a sample" is one click rather than two.
        #
        # RESOLVED BY catalog.sample_dataset, WHICH THE WIZARD ALSO CALLS. This
        # used to be four lines here, with the directory and both filenames
        # written out — which meant the console had no way to ask which file a
        # run would use, and so could not offer its columns. `inputFields` is
        # required, so that gap made the advertised "leave it empty" fallback
        # impossible to actually take. One function now answers for both.
        interface = ctx.model.interface if ctx.model else "chat"
        path = catalog_sample_dataset(ctx.template.metadata.id, interface)
        if path is None:
            raise StepFailed(
                f"no dataset was uploaded and this template ships no sample for a "
                f"{interface!r} model, so there is nothing to run over. Upload a "
                f"CSV or JSONL file and deploy again."
            )
        return path, f"the bundled sample {path.name}"

    async def _staging_pod(self, ctx: StepContext, resource: str) -> str:
        """Render, apply and wait for a staging pod. Returns its name.

        Shared by both directions — dataset in, results out — because both are
        the same problem and the only difference is the volume's access mode.

        THE POD'S NAME, ITS MODE AND ITS FILENAME ALL COME FROM ONE TABLE
        (render.staging_render), which the wizard's preview reads too. They used
        to be arguments spelled out at each call site, which meant the preview
        had no way to know them and showed both renders under one filename —
        the same manifest twice, for two different pods.
        """
        ns = self._host_ns(ctx.project)
        staged = render.staging_render(ctx.project, resource)
        if staged is None:  # pragma: no cover — guarded by the table above
            raise StepFailed(f"{resource!r} does not render a staging pod")
        filename, extras = staged
        name = extras["stage_name"]
        path = await self._write_rendered(
            ctx,
            self._fragment(ctx, resource),
            extras=extras,
            filename=filename,
        )

        # A Pod's spec is immutable, so re-applying a leftover one fails. It is
        # ephemeral by design, so deleting first is always right.
        await run(["kubectl", "delete", "pod", name, "-n", ns, "--ignore-not-found",
                   "--wait=true", "--timeout=60s"],
                  timeout=90, emit=ctx.emit, check=False)
        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply staging pod")

        ctx.emit(f"waiting for pod/{name} to be Ready")
        rc, out = await run(
            ["kubectl", "wait", "--for=condition=Ready", f"pod/{name}", "-n", ns,
             f"--timeout={TIMEOUTS['pod-ready']}s"],
            timeout=TIMEOUTS["pod-ready"] + 30, emit=ctx.emit, check=False,
        )
        if rc != 0:
            _, events = await run(
                ["kubectl", "get", "events", "-n", ns, "--sort-by=.lastTimestamp"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            raise StepFailed(
                f"the staging pod did not start, so the files cannot be moved.\n"
                f"{_tail(out, 4)}\nRecent events:\n{_tail(events, 12)}"
            )
        return name

    async def _delete_staging_pod(self, ctx: StepContext, name: str) -> None:
        """Always, even on the failure paths — it holds a ReadWriteOnce volume."""
        await run(
            ["kubectl", "delete", "pod", name, "-n", self._host_ns(ctx.project),
             "--ignore-not-found", "--wait=true", "--timeout=60s"],
            timeout=90, emit=ctx.emit, check=False,
        )
        ctx.emit("staging pod removed")

    async def dataset_upload(self, ctx: StepContext) -> StepResult:
        """Get the PI's dataset onto the `scratch` PVC."""
        _raise_if_injected("dataset-upload")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        source, description = self._dataset_source(ctx)
        size = source.stat().st_size
        ctx.emit(f"dataset source: {description} — {size:,} bytes")

        pod = await self._staging_pod(ctx, "dataset-upload")
        try:
            # mkdir first: `kubectl cp` into a path whose parent does not exist
            # fails with a tar error that reads like a permissions problem.
            await run(["kubectl", "exec", pod, "-n", ns, "--",
                       "mkdir", "-p", "/mnt/scratch/input", "/mnt/scratch/output"],
                      timeout=TIMEOUTS["kubectl"], emit=ctx.emit, label="prepare scratch")

            target = f"/mnt/scratch/input/{source.name}"
            ctx.emit(f"copying {source.name} into {pod}:{target}")
            await run(
                ["kubectl", "cp", str(source), f"{ns}/{pod}:{target}"],
                timeout=TIMEOUTS["cp"], emit=ctx.emit, label="kubectl cp dataset",
            )

            # VERIFY BY BYTE COUNT, INSIDE THE POD. `kubectl cp` can exit 0
            # having copied nothing or copied partially — it is a tar stream over
            # exec, and a truncated stream is not always an error. The only
            # trustworthy evidence is the size on the volume, compared with the
            # size on disk. A short file here would surface as a run that
            # silently processed half a dataset.
            _, listing = await run(
                ["kubectl", "exec", pod, "-n", ns, "--", "stat", "-c", "%s", target],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            landed = "".join(c for c in listing if c.isdigit())
            if not landed or int(landed) != size:
                raise StepFailed(
                    f"the dataset did not land intact: {size:,} bytes on disk, "
                    f"{landed or 'nothing'} on the volume. Nothing was run."
                )
            ctx.emit(f"verified on the volume: {int(landed):,} bytes")
        finally:
            # ALWAYS. The pod holds a ReadWriteOnce volume, and leaving it
            # running would make the batch Job unschedulable for the whole 900s
            # of the pod's own deadline.
            await self._delete_staging_pod(ctx, pod)

        return StepResult(
            ok=True,
            log=f"{description} staged to /mnt/scratch/input/{source.name} ({size:,} bytes)",
            outputs={"datasetFile": source.name, "datasetBytes": size},
        )

    async def _read_runtime(self, ctx: StepContext) -> dict:
        """Resolve the ClusterServingRuntime for the chosen model. (D2.)

        THE ONE CLUSTER READ THAT MAKES THE WHOLE TEMPLATE'S ARGUMENT. The batch
        Job's serving container is not described anywhere in this repository —
        it is whatever the curated runtime says it is, read here and handed to a
        pure renderer.
        """
        if ctx.model is None:
            raise StepFailed("batch-run needs a model, and none was selected")

        _, raw = await run(
            ["kubectl", "get", "clusterservingruntimes", "-o", "json"],
            timeout=TIMEOUTS["kubectl"], label="read the runtime catalogue",
        )
        try:
            runtimes = json.loads(raw).get("items", [])
        except json.JSONDecodeError as exc:
            raise StepFailed(f"could not read the runtime catalogue: {exc}") from exc

        try:
            chosen = render.select_runtime(ctx.model, runtimes)
            container = render.runtime_container(chosen, ctx.project)
        except render.RuntimeUnavailable as exc:
            # Already written for a human, and aimed at a platform engineer
            # rather than the PI — a missing runtime is not something a PI can
            # fix by choosing differently.
            raise StepFailed(str(exc)) from exc

        ctx.emit(
            f"curated runtime: {container['name']} -> {container['image']}"
        )
        ctx.emit("the batch Job runs the SAME image a served endpoint would")
        return container

    # --- the two reasons a run has not started yet, which look identical ---
    #
    # D10. Both present as "nothing is happening", and they are completely
    # different problems with completely different answers:
    #
    #   QUOTA     the API server REFUSED to create the pod. No pod exists at all,
    #             and the reason lives only in the Job's events. The project's own
    #             GPU is already in use — by its own other run, or a served
    #             endpoint in the same namespace. The PI waits, and it is their
    #             own ceiling they are waiting on.
    #
    #   CAPACITY  the pod EXISTS and is Pending because the scheduler cannot
    #             place it. The cluster's GPUs are all taken by other tenants.
    #             The PI waits, but nothing about their project is the cause.
    #
    # Telling a PI "waiting for GPU" without which one is true is telling them
    # nothing they can act on. Neither is an error, and both render amber.
    _QUOTA_MARKERS = ("exceeded quota", "forbidden", "must specify")
    _CAPACITY_MARKERS = ("insufficient nvidia.com/gpu", "unschedulable",
                         "didn't match", "untolerated taint")

    async def _why_not_started(self, ctx: StepContext, job: str) -> str | None:
        """A PI-facing reason the Job has produced no running pod, or None."""
        ns = self._host_ns(ctx.project)

        _, pods = await run(
            ["kubectl", "get", "pods", "-n", ns, "-l", f"job-name={job}",
             "-o", "jsonpath={range .items[*]}{.status.phase}|{range .status.conditions[*]}{.reason} {.message}{end}{\"\\n\"}{end}"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        pods = pods.strip()

        if not pods:
            # NO POD AT ALL -> the quota refused it. The Job's events are the
            # only place that says so; `kubectl get pods` is simply empty, which
            # is the same thing it says when a pod is about to appear.
            _, events = await run(
                ["kubectl", "get", "events", "-n", ns, "--field-selector",
                 f"involvedObject.name={job}", "--sort-by=.lastTimestamp",
                 "-o", "jsonpath={range .items[*]}{.message}{\"\\n\"}{end}"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            lowered = events.lower()
            if any(marker in lowered for marker in self._QUOTA_MARKERS):
                return ("waiting for GPU — your project's GPU is currently in use")
            return None

        lowered = pods.lower()
        if "pending" in lowered and any(m in lowered for m in self._CAPACITY_MARKERS):
            return "waiting for GPU — no free GPU in the shared pool"
        if "pending" in lowered:
            # Pending for some other reason — an image pull, a volume binding.
            # Named honestly rather than blamed on the GPU.
            return "starting up — waiting for the runtime image and your model"
        return None

    @staticmethod
    def _last_json_line(text: str, prefix: str) -> dict | None:
        """The LAST line of a given machine-readable shape, parsed.

        The last, not the first: BATCH_PROGRESS is printed repeatedly and only
        the most recent one is current. Malformed lines are skipped rather than
        raising — a driver mid-write must never break the poller that is
        watching it.
        """
        found = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(prefix):
                continue
            try:
                candidate = json.loads(line[len(prefix):].strip())
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, dict):
                found = candidate
        return found

    async def batch_run(self, ctx: StepContext) -> StepResult:
        """Render and apply the Job, then watch it until it ends.

        THE ONE STEP THAT CAN LAST AN HOUR, which is why it is the only one that
        reports structured progress rather than just a log. Everything the UI
        draws — the bar, the throughput, the ETA, the amber "waiting for GPU" —
        comes from `ctx.progress` calls made in this loop.
        """
        _raise_if_injected("batch-run")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        runtime = await self._read_runtime(ctx)
        dataset = ctx.outputs.get("datasetFile") or "dataset.csv"
        job = f"{ctx.project}-batch"

        path = await self._write_rendered(
            ctx,
            self._fragment(ctx, "batch-run"),
            extras={"runtime": runtime, "input_path": f"/mnt/scratch/input/{dataset}"},
        )

        # A completed Job's pod template is immutable, so re-applying over a
        # previous attempt fails. A retry genuinely wants a fresh Job.
        rc, _ = await run(["kubectl", "get", "job", job, "-n", ns],
                          timeout=TIMEOUTS["kubectl"], check=False)
        if rc == 0:
            ctx.emit(f"job/{job} exists — deleting it so this attempt starts clean")
            await run(["kubectl", "delete", "job", job, "-n", ns, "--wait=true"],
                      timeout=TIMEOUTS["kubectl"], emit=ctx.emit, check=False)

        await run(["kubectl", "apply", "-f", str(path)],
                  timeout=TIMEOUTS["apply"], emit=ctx.emit, label="kubectl apply batch job")

        timeout_minutes = int(ctx.params.get("runTimeoutMinutes") or 60)
        total_hint = int(ctx.params.get("maxRows") or 0)
        ctx.emit(
            f"watching the run (the Job stops itself after {timeout_minutes} minutes)"
        )
        ctx.progress({
            "percent": 0, "done": 0, "total": total_hint or None, "failed": 0,
            "rowsPerSec": 0, "etaSeconds": None, "detail": "starting the run",
        })

        # The Job enforces its own deadline; this is the outer bound on OUR
        # watching, generous enough that Kubernetes always wins the race and
        # reports a clean DeadlineExceeded rather than Studio giving up first.
        deadline = (
            asyncio.get_running_loop().time()
            + timeout_minutes * 60
            + TIMEOUTS["batch-grace"]
        )
        last_progress: dict | None = None

        while True:
            _, status = await run(
                ["kubectl", "get", "job", job, "-n", ns, "-o",
                 "jsonpath={range .status.conditions[*]}{.type}={.status};{.reason};{end}"],
                timeout=TIMEOUTS["kubectl"], check=False,
            )
            conditions = status.strip()

            if "Complete=True" in conditions:
                break
            if "Failed=True" in conditions:
                # DeadlineExceeded is a TIMEOUT, not a crash, and saying so is
                # the difference between "your run was too long" and "your run
                # broke". The PI's own runTimeoutMinutes caused it.
                logs = await self._driver_logs(ctx, job, tail=40)
                if "DeadlineExceeded" in conditions:
                    raise StepFailed(
                        f"the run hit its {timeout_minutes}-minute limit and was "
                        f"stopped. Whatever it had finished was still written, and "
                        f"the partial results are collected.\n"
                        f"Raise 'Give up after' or lower 'How many rows', then retry."
                        f"\n--- last of the driver's output ---\n{_tail(logs, 40)}"
                    )
                raise StepFailed(
                    f"the run failed.\n--- last of the driver's output ---\n"
                    f"{_tail(logs, 40)}"
                )

            # --- not finished: is it even running? --------------------------
            reason = await self._why_not_started(ctx, job)
            if reason:
                ctx.progress({
                    **(last_progress or {"percent": 0, "done": 0,
                                         "total": total_hint or None, "failed": 0,
                                         "rowsPerSec": 0, "etaSeconds": None}),
                    "detail": reason,
                })
            else:
                logs = await self._driver_logs(ctx, job, tail=200)
                snapshot = self._last_json_line(logs, "BATCH_PROGRESS")
                if snapshot:
                    done = snapshot.get("done") or 0
                    total = snapshot.get("total") or total_hint or 0
                    last_progress = {
                        "percent": int(100 * done / total) if total else 0,
                        "done": done,
                        "total": total,
                        "failed": snapshot.get("failed", 0),
                        "rowsPerSec": snapshot.get("rowsPerSec", 0),
                        "etaSeconds": snapshot.get("etaSeconds"),
                        "detail": None,
                    }
                    ctx.progress(last_progress)

            if asyncio.get_running_loop().time() > deadline:
                raise StepFailed(
                    f"the run was still going {TIMEOUTS['batch-grace']}s after its own "
                    f"{timeout_minutes}-minute deadline should have stopped it. "
                    f"Something is wrong with the Job rather than with the work."
                )
            await asyncio.sleep(TIMEOUTS["batch-poll"])

        # --- it completed -----------------------------------------------------
        logs = await self._driver_logs(ctx, job, tail=400)
        result = self._last_json_line(logs, "BATCH_RESULT")
        if not result:
            raise StepFailed(
                "the run finished but printed no BATCH_RESULT line, so there are no "
                "numbers to report. The driver may have been killed mid-write.\n"
                f"--- last of the driver's output ---\n{_tail(logs, 30)}"
            )

        ctx.progress({
            "percent": 100,
            "done": result.get("rows", 0) + result.get("failed", 0),
            "total": result.get("rows", 0) + result.get("failed", 0),
            "failed": result.get("failed", 0),
            "rowsPerSec": result.get("rowsPerSecond", 0),
            "etaSeconds": 0,
            "detail": None,
        })
        ctx.emit(_tail(logs, 12))

        return StepResult(
            ok=True,
            log=(
                f"{result.get('rows')} rows processed, {result.get('failed')} failed, "
                f"in {result.get('durationSeconds')}s "
                f"({result.get('rowsPerSecond')} rows/s) "
                f"using runtime {runtime['name']}"
            ),
            outputs={
                "rowsProcessed": result.get("rows"),
                "rowsFailed": result.get("failed"),
                "durationSeconds": result.get("durationSeconds"),
                "rowsPerSecond": result.get("rowsPerSecond"),
                "runtimeUsed": runtime["name"],
                "runtimeImage": runtime["image"],
            },
        )

    async def _driver_logs(self, ctx: StepContext, job: str, tail: int) -> str:
        """The DRIVER container's output, specifically.

        `-c driver` is not optional: the pod has two containers, and without it
        kubectl either picks the default or errors. The runtime's log is the
        serving runtime's own chatter and contains no BATCH_ lines at all.
        """
        _, logs = await run(
            ["kubectl", "logs", f"job/{job}", "-n", self._host_ns(ctx.project),
             "-c", "driver", f"--tail={tail}"],
            timeout=TIMEOUTS["kubectl"], check=False,
        )
        return logs

    async def collect_results(self, ctx: StepContext) -> StepResult:
        """Pull the artifacts out, then start the retention clock."""
        _raise_if_injected("collect-results")
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        # READ-ONLY. This step copies FROM the volume and has no business
        # writing to it — a bad path argument on a read-write mount could
        # overwrite the very files it exists to retrieve. The mode is declared
        # once, in render.STAGING_RENDERS, beside the name it goes with.
        pod = await self._staging_pod(ctx, "collect-results")

        artifacts_dir = repo_root() / "tenants" / ctx.project / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            ctx.emit(f"copying /mnt/scratch/output/. out to "
                     f"{artifacts_dir.relative_to(repo_root())}")
            await run(
                ["kubectl", "cp", f"{ns}/{pod}:/mnt/scratch/output/.", str(artifacts_dir)],
                timeout=TIMEOUTS["cp"], emit=ctx.emit, label="kubectl cp results",
            )
        finally:
            await self._delete_staging_pod(ctx, pod)

        files = sorted(
            [{"name": f.name, "bytes": f.stat().st_size}
             for f in artifacts_dir.iterdir()
             if f.is_file() and not f.name.startswith(".")],
            key=lambda f: f["name"],
        )
        if not files:
            raise StepFailed(
                "the run produced no files to collect. The driver writes "
                "results.jsonl, errors.jsonl and run-summary.json even when it "
                "fails, so an empty output directory means the copy did not work."
            )
        ctx.emit("collected: " + ", ".join(f"{f['name']} ({f['bytes']:,}B)" for f in files))

        # --- THE RETENTION CLOCK STARTS NOW ---------------------------------
        # namespace-pack wrote a safety net at provisioning time. This replaces
        # it with the real value: completion + artifactRetentionDays, which is
        # the promise the wizard made ("results stay downloadable for R days").
        # Studio only SETS the annotation; the host reconciler is what honours
        # it — which is why the UI says retention is displayed but not yet
        # enforced.
        retention = int(ctx.params.get("artifactRetentionDays") or 14)
        until = (
            datetime.now(timezone.utc) + timedelta(days=retention)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        await run(
            ["kubectl", "annotate", "namespace", ns, "--overwrite",
             f"hive.ksl/expiry={until}"],
            timeout=TIMEOUTS["kubectl"], emit=ctx.emit, label="set the retention clock",
        )
        ctx.emit(f"results available until {until} ({retention} days from now)")

        return StepResult(
            ok=True,
            log=(
                f"{len(files)} artifact(s) collected to "
                f"tenants/{ctx.project}/artifacts/; retention set to {until}"
            ),
            outputs={"artifacts": files, "resultsAvailableUntil": until},
        )

    # ------------------------------------------------------------------
    # TEARDOWN
    # ------------------------------------------------------------------

    async def teardown(self, ctx: StepContext, step_id: str) -> StepResult:
        # Teardown-scoped, so a provisioning injection cannot break the escape
        # hatch — see _raise_if_injected_teardown.
        _raise_if_injected_teardown(step_id)
        ns = self._host_ns(ctx.project)
        _assert_ours(ns)

        # -----------------------------------------------------------------
        # RAG STACK — all three are HOST objects, so all three are plain
        # deletes with --ignore-not-found. Absent is the desired end state, not
        # an error: teardown is the escape hatch from a FAILED run, so it must
        # cope with a project that only got three steps in. Everything here is
        # scoped by the project's own label as well as by name, so a delete can
        # never reach outside what this project created.
        # -----------------------------------------------------------------

        if step_id == "rag-api":
            # First in teardownOrder: it is the only thing holding open
            # connections to the other two, so removing it first means no
            # request is in flight against a Qdrant that is going away.
            await run(
                ["kubectl", "delete", "deployment,service", "-n", ns,
                 "-l", f"hive.ksl/project={ctx.project},hive.ksl/role=rag-api",
                 "--ignore-not-found"],
                timeout=TIMEOUTS["apply"], emit=ctx.emit, check=False,
            )
            # And close the playground's cached tunnel to it. A port-forward to
            # a Service that no longer exists is a child process retrying
            # forever, and it would otherwise outlive the project.
            await dataplane.forwards.drop(ns, f"{ctx.project}-rag", 8080)
            return StepResult(ok=True, log="RAG service removed")

        if step_id == "ingestion":
            # The Job may still be RUNNING — on a failed or retried deployment
            # this teardown is exactly what stops it. It writes to Qdrant, so it
            # must stop before Qdrant does.
            #
            # `--cascade=foreground` so the command does not return until the
            # Job's pod is actually gone. The default (background) returns
            # immediately and would let the next step delete Qdrant out from
            # under a pod still upserting into it.
            await run(
                ["kubectl", "delete", "job", f"{ctx.project}-ingest", "-n", ns,
                 "--ignore-not-found", "--cascade=foreground",
                 "--wait=true", "--timeout=120s"],
                timeout=150, emit=ctx.emit, check=False,
            )
            # The staging pod is deleted by the step that creates it, so this is
            # only for the case where that step itself failed part-way.
            await run(
                ["kubectl", "delete", "pod", f"{ctx.project}-stage", "-n", ns,
                 "--ignore-not-found"],
                timeout=TIMEOUTS["kubectl"], emit=ctx.emit, check=False,
            )
            return StepResult(ok=True, log="indexing Job removed")

        if step_id == "vector-db":
            await run(
                ["kubectl", "delete", "deployment,service", "-n", ns,
                 "-l", f"hive.ksl/project={ctx.project},hive.ksl/role=vector-db",
                 "--ignore-not-found"],
                timeout=TIMEOUTS["apply"], emit=ctx.emit, check=False,
            )
            # THE PVC IS NOT DELETED HERE. `qdrant-data` is declared in
            # namespace-pack and goes with the namespace, at the end of
            # teardownOrder — which is what keeps every claim's lifetime tied to
            # the one object that owns them all, and what makes "your documents
            # are deleted with your project" true of the index as well.
            return StepResult(ok=True, log="vector database removed (its volume goes with the namespace)")

        if step_id == "inference-service":
            # TEARDOWN IS BEST-EFFORT, BY DESIGN. It is the escape hatch from a
            # failed run, so it must cope with a project that is only partly
            # built — and the runs most likely to need deleting are exactly the
            # ones that stopped early.
            #
            # This was a real defect, found by the S7 audit: `_vc` opens a
            # kubeconfig via `vcluster connect --print`, which FAILS when no
            # vCluster was ever created. A project that failed at namespace-pack
            # therefore could not be deleted at all — teardown stopped on its
            # first step and the row stayed `failed` forever.
            #
            # Absent is not an error here. There being no isvc inside a vCluster
            # that does not exist is the desired end state, already reached.
            try:
                await self._vc(
                    ctx,
                    ["kubectl", "delete", "isvc", ctx.project, "--ignore-not-found"],
                    timeout=TIMEOUTS["kubectl"], label="delete isvc inside vCluster",
                )
            except StepFailed as exc:
                ctx.emit(
                    f"could not reach the tenant's own cluster ({exc}); "
                    f"nothing to remove inside it — continuing"
                )

            # The HOST mirror is the one that matters: deleting it cascades the
            # Knative Service, Route and pods, which is what releases the GPU.
            await run(
                ["kubectl", "delete", "isvc", "-n", ns,
                 "-l", f"hive.ksl/project={ctx.project}", "--ignore-not-found"],
                timeout=TIMEOUTS["apply"], emit=ctx.emit, check=False,
            )
            return StepResult(ok=True, log="InferenceService removed, GPU released")

        if step_id == "batch-run":
            # Deleting the Job stops the work AND releases the GPU immediately,
            # rather than letting a run nobody wants any more hold a GPU until
            # it finishes. `--cascade` so the pod goes with it; without it an
            # orphaned pod keeps the sidecar — and therefore the GPU — alive.
            await run(
                ["kubectl", "delete", "job", f"{ctx.project}-batch", "-n", ns,
                 "--ignore-not-found", "--cascade=foreground",
                 "--wait=true", "--timeout=120s"],
                timeout=150, emit=ctx.emit, check=False,
            )
            # Both staging pods are deleted by the steps that create them, but a
            # run torn down MID-STEP can leave one behind holding a
            # ReadWriteOnce volume — which would then block the namespace
            # delete below. Cheap to make sure.
            for pod in (f"{ctx.project}-stage-in", f"{ctx.project}-stage-out"):
                await run(
                    ["kubectl", "delete", "pod", pod, "-n", ns, "--ignore-not-found",
                     "--wait=false"],
                    timeout=60, check=False,
                )
            return StepResult(ok=True, log="batch Job removed, GPU released")

        if step_id == "vcluster":
            await run(
                ["vcluster", "delete", ctx.project, "--namespace", ns,
                 "--ignore-not-found",
                 # The namespace is OURS, created by namespace-pack, not by
                 # vclusterctl. Deleting it is a separate, ordered step below;
                 # letting this command take it early would remove the quota and
                 # the PVCs out of order.
                 "--auto-delete-namespace=false"],
                timeout=TIMEOUTS["vcluster-create"], emit=ctx.emit, check=False,
            )
            return StepResult(ok=True, log=f"vCluster {ctx.project} deleted")

        if step_id == "namespace-pack":
            # `--wait` plus a timeout, but NOT check=True. A namespace with
            # finalizers routinely takes longer than the timeout to finish
            # terminating, and kubectl then exits non-zero even though the
            # deletion is proceeding correctly. Failing the step on that would
            # report a successful teardown as broken.
            #
            # So the command is best-effort and the VERIFICATION is a poll: gone
            # is gone, whatever kubectl's exit code said.
            #
            # A NAMESPACE IS EMPTIED BY THE CONTROLLER-MANAGER, not by kubectl.
            # `kubectl delete namespace` only marks it Terminating; the namespace
            # controller inside kube-controller-manager then deletes everything
            # in it. If that process is down, the namespace sits Terminating with
            # its PVCs still Bound and nothing will ever move — so it is checked
            # BEFORE the long wait and said out loud, rather than discovered
            # eight minutes later by a timeout that blamed a finalizer.
            healthy, why = await asyncio.to_thread(control_plane_health)
            if not healthy:
                ctx.emit(
                    "note: the platform's own controller-manager is not running, and "
                    "it is what actually empties a namespace. Waiting anyway, in "
                    "case it recovers in time:\n" + why
                )
            await run(
                ["kubectl", "delete", "namespace", ns, "--ignore-not-found",
                 "--wait=true", f"--timeout={TIMEOUTS['delete-ns']}s"],
                timeout=TIMEOUTS["delete-ns"] + 30, emit=ctx.emit, check=False,
            )
            started = asyncio.get_running_loop().time()
            deadline = started + 180
            polls = 0
            while True:
                # GONE MEANS NotFound — NOT "kubectl failed". This used to treat
                # any non-zero exit as proof of deletion, so a teardown run while
                # the API server was refusing connections (p-abcd, 2026-09-08)
                # read "connection refused" as "the namespace is gone", recorded
                # the project as deleted, and left it running for three weeks.
                #
                # With --ignore-not-found, NotFound is exit 0 and EMPTY output.
                # A non-zero exit is now only ever an error reaching the cluster,
                # which proves nothing and is waited out like "still there".
                rc, out = await run(
                    ["kubectl", "get", "namespace", ns, "--ignore-not-found", "-o", "name"],
                    timeout=30, check=False,
                )
                if rc == 0 and not out.strip():
                    break  # NotFound — the only outcome that counts
                if rc != 0:
                    unreachable = _tail(out, 2)
                else:
                    unreachable = None
                if asyncio.get_running_loop().time() > deadline:
                    if unreachable:
                        raise StepFailed(
                            f"could not reach the cluster to confirm {ns} is gone, "
                            f"so the project is NOT recorded as removed:\n"
                            f"{unreachable}\n"
                            f"Once the cluster answers again, press Delete again."
                        )
                    raise StepFailed(await self._namespace_stuck(ns))
                # One line every ~30s, not every 5s: thirty-five identical
                # "still terminating" lines told the reader nothing that one
                # line with a running clock does not.
                if polls % 6 == 0:
                    waited = int(asyncio.get_running_loop().time() - started)
                    ctx.emit(f"{ns} still terminating… ({waited}s)")
                polls += 1
                await asyncio.sleep(5)
            # The rendered manifests stay on disk deliberately: they are the record
            # of what this project WAS, and invariant (a) is about reviewability
            # after the fact as much as before it. The kubeconfig does not — it is
            # a credential for a control plane that no longer exists.
            kc = rendered_dir(ctx.project) / "kubeconfig"
            if kc.exists():
                kc.unlink()
                ctx.emit("removed the tenant kubeconfig (its cluster is gone)")
            return StepResult(ok=True, log=f"namespace {ns} deleted")

        raise StepFailed(f"no teardown defined for step {step_id!r}")
