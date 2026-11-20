# ===========================================================================
# HIVE STUDIO BFF — LIVE STATUS, READ FROM THE CLUSTER
# ===========================================================================
# INVARIANT (b) LIVES HERE. The registry records what was asked for and how the
# pipeline went; it never caches readiness, replica counts, URLs or expiry. Every
# one of those is read here, on demand, from the cluster — so a project that has
# scaled to zero, been evicted, expired or been deleted behind Studio's back tells
# the truth the moment someone looks.
#
# Nothing in this module writes anything, to the cluster or to SQLite.
#
# ---------------------------------------------------------------------------
# THE DERIVED STATUS, AND WHY `sleeping` IS NOT A PROBLEM
# ---------------------------------------------------------------------------
#   provisioning  the pipeline is still running
#   ready         Ready=True on the host, and at least one predictor pod
#   sleeping      Ready=True, on-demand, and ZERO pods. This is the archetype
#                 working exactly as designed — the GPU is back in the pool. It is
#                 grey in the UI, never amber and never red.
#   waking        zero pods, but an invoke is in flight. Held in memory only: it
#                 is a property of a request happening right now, not a fact about
#                 the project, and it must not survive a restart.
#   failed        the run failed
#   deleted       torn down
#
# An always-on project with zero pods is NOT sleeping — it is broken, and reports
# `ready` only when a pod is actually there. Conflating the two would hide the one
# failure T-AO exists to prevent.
# ===========================================================================

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from typing import Any

import httpx

# Projects with an invoke in flight while scaled to zero. In memory on purpose:
# a "waking" that outlived the request that caused it would be a lie, and a
# "waking" restored after a restart would be a lie about a request that no longer
# exists.
_waking: set[str] = set()


def mark_waking(project: str) -> None:
    _waking.add(project)


def clear_waking(project: str) -> None:
    _waking.discard(project)


def is_waking(project: str) -> bool:
    return project in _waking


async def _kubectl(args: list[str], timeout: int = 15) -> tuple[int, str]:
    """Read-only kubectl. Never raises — a live read that fails degrades the
    answer, it does not break the page."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, (out or b"").decode("utf-8", "replace")
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return 1, ""


async def isvc_facts(project: str) -> dict[str, Any]:
    """Readiness, URL and synced name for a project's host-side isvc.

    Discovered BY LABEL, not by assembling the name. The `-x-` translation is a
    convention this build follows, not a guarantee, and the S4 `-predictor` finding
    is the standing reminder that constructed identifiers drift.
    """
    ns = f"p-{project}"
    rc, out = await _kubectl(
        ["get", "isvc", "-n", ns, "-l", f"hive.ksl/project={project}", "-o", "json"]
    )
    if rc != 0 or not out.strip():
        return {"exists": False, "ready": False, "url": None, "syncedName": None}

    try:
        items = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return {"exists": False, "ready": False, "url": None, "syncedName": None}
    if not items:
        return {"exists": False, "ready": False, "url": None, "syncedName": None}

    isvc = items[0]
    conditions = isvc.get("status", {}).get("conditions", [])
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    return {
        "exists": True,
        "ready": ready,
        # READ LIVE, never constructed — local URLs carry a `-predictor` segment
        # production will not have (DEVIATIONS.md).
        "url": isvc.get("status", {}).get("url"),
        "syncedName": isvc["metadata"]["name"],
    }


async def predictor_pods(project: str, synced_name: str | None) -> int:
    """How many predictor pods are actually Running right now.

    THE NUMBER THAT DECIDES `sleeping`. Counted from pods rather than from the
    Knative Revision, because the Revision keeps reporting a desired scale after
    the pods are gone and the question here is what is running, not what is wanted.
    """
    if not synced_name:
        return 0
    rc, out = await _kubectl(
        ["get", "pods", "-n", f"p-{project}", "--field-selector=status.phase=Running", "-o", "json"]
    )
    if rc != 0 or not out.strip():
        return 0
    try:
        items = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return 0
    # Knative names the pod after the Revision, which is prefixed with the isvc
    # name; the seed Job and the vCluster control plane must not be counted.
    return sum(1 for p in items if p["metadata"]["name"].startswith(f"{synced_name}-predictor"))


async def quota(project: str) -> dict[str, Any] | None:
    """The tenant's ceiling and what is against it, from the ResourceQuota."""
    rc, out = await _kubectl(
        ["get", "resourcequota", "tier-quota", "-n", f"p-{project}", "-o", "json"]
    )
    if rc != 0 or not out.strip():
        return None
    try:
        status = json.loads(out).get("status", {})
    except json.JSONDecodeError:
        return None
    return {"used": status.get("used", {}), "hard": status.get("hard", {})}


async def expiry(project: str) -> dict[str, Any] | None:
    """The lifetime clock, read from the namespace annotation.

    THE AUTHORITATIVE SOURCE, replacing the arithmetic-on-intent that S2 used.
    Studio writes `hive.ksl/expiry` at provisioning; this reads it back. Two
    countdowns that can disagree would be worse than one, so the derived one is
    gone from this path entirely.
    """
    rc, out = await _kubectl(
        ["get", "namespace", f"p-{project}", "-o",
         "jsonpath={.metadata.annotations.hive\\.ksl/expiry}"]
    )
    stamp = out.strip()
    if rc != 0 or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    remaining = (when - datetime.now(timezone.utc)).total_seconds()
    return {
        "expiresAt": stamp,
        "daysLeft": max(0, int(remaining // 86400)),
        "expired": remaining <= 0,
    }


async def events(project: str, limit: int = 30) -> list[str]:
    """Recent cluster events for the tenant namespace, oldest first."""
    rc, out = await _kubectl(
        ["get", "events", "-n", f"p-{project}", "--sort-by=.lastTimestamp",
         "--no-headers"],
        timeout=20,
    )
    if rc != 0:
        return []
    lines = [l.rstrip() for l in out.splitlines() if l.strip()]
    return lines[-limit:]


def derive_status(
    run_state: str,
    deleted: bool,
    ready: bool,
    pods: int,
    availability: str,
    project: str,
) -> str:
    """One word for what this project is doing right now.

    Order matters: a deleted project is deleted whatever the cluster says, and a
    run still in flight is `provisioning` before any of the pod arithmetic applies.
    """
    if deleted or run_state == "deleted":
        return "deleted"
    if run_state == "queued":
        return "queued"
    if run_state in ("requested", "provisioning"):
        return "provisioning"
    if run_state == "failed":
        return "failed"
    if not ready:
        # The run finished but the cluster does not say Ready — expired, evicted,
        # or deleted behind Studio's back. Exactly the case invariant (b) exists
        # for, and exactly why this is not read from the registry.
        return "failed" if run_state == "ready" else run_state
    if pods == 0:
        if is_waking(project):
            return "waking"
        if availability == "on-demand":
            return "sleeping"
        # Always-on with no pods is NOT sleeping. It is supposed to hold a replica.
        return "failed"
    return "ready"


# ===========================================================================
# RAG STACK — THE EXTRA THINGS A RAG PROJECT HAS
# ===========================================================================
# INVARIANT (b) APPLIES HERE EXACTLY AS ABOVE, and a RAG project is where it
# earns its keep. The registry records that the ingestion Job reported 37
# chunks; it must never be the answer to "how big is the index?" Between then
# and now the collection can have been dropped, the volume can have filled, or
# the whole project can have expired. The number on the page is read from
# Qdrant, every time somebody looks.
#
# `indexedChunks` from the pipeline is kept alongside it, and the two are shown
# as what they are: what this run PRODUCED, and what is there NOW. A
# disagreement between them is information, not an error to hide.
# ===========================================================================


async def deployment_ready(project: str, role: str) -> dict[str, Any]:
    """Ready/desired replicas for one of the project's own Deployments.

    Found BY LABEL rather than by name, for the same reason isvc_facts is:
    a constructed identifier drifts, and the label is already in the manifest
    the pipeline applied.
    """
    rc, out = await _kubectl(
        ["get", "deployments", "-n", f"p-{project}",
         "-l", f"hive.ksl/project={project},hive.ksl/role={role}", "-o", "json"]
    )
    absent = {"exists": False, "ready": 0, "desired": 0}
    if rc != 0 or not out.strip():
        return absent
    try:
        items = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return absent
    if not items:
        return absent
    status = items[0].get("status", {})
    spec = items[0].get("spec", {})
    return {
        "exists": True,
        "ready": int(status.get("readyReplicas") or 0),
        "desired": int(spec.get("replicas") or 0),
        "name": items[0]["metadata"]["name"],
    }


async def qdrant_points(project: str, collection: str, forward_port: int) -> int | None:
    """The LIVE point count, straight from Qdrant.

    `GET /collections/{name}` -> `.result.points_count`. THE PATH IS VERIFIED,
    not assumed: probed against a running qdrant/qdrant:v1.19.0, where the count
    is nested under `result` and the sibling `indexed_vectors_count` is a
    DIFFERENT number — it stays 0 until the collection passes Qdrant's HNSW
    indexing threshold, so a small corpus shows 0 there while holding every
    point. Reading that field instead would report a healthy index as empty.

    None means "could not be read" — a collection that is genuinely gone, or a
    Qdrant that is not answering. The caller distinguishes that from 0, which
    means "the collection is there and empty".
    """
    if not collection:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"http://127.0.0.1:{forward_port}/collections/{collection}"
            )
        if response.status_code != 200:
            return None
        return int(response.json()["result"]["points_count"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


async def rag_facts(project: str, collection: str) -> dict[str, Any]:
    """Everything a RAG project has that an inference project does not.

    Assembled with ONE forward to Qdrant, opened on demand and cached by the
    pool, because the project page polls: a fresh port-forward per poll would
    spawn a child process every few seconds for as long as somebody has the page
    open.

    NOTHING HERE RAISES. A live read that fails degrades the answer, it does not
    break the page — a project whose namespace has gone should render as gone,
    not as a 500.
    """
    from . import dataplane

    ns = f"p-{project}"
    qdrant = await deployment_ready(project, "vector-db")
    rag = await deployment_ready(project, "rag-api")

    points: int | None = None
    if qdrant["exists"] and qdrant["ready"] > 0:
        forward = dataplane.forwards.get(ns, f"{project}-qdrant", 6333, "/readyz")
        try:
            port = await forward.ensure()
            points = await qdrant_points(project, collection, port)
        except RuntimeError:
            # The forward could not be established. Reported as an unknown count
            # rather than as zero: "we could not ask" and "there is nothing
            # there" are different facts and the page says which.
            points = None

    return {
        "collection": collection or None,
        "vectorDb": qdrant,
        "ragApi": rag,
        # None = could not be read. 0 = the collection exists and is empty.
        "livePoints": points,
    }


# ---------------------------------------------------------------------------
# BATCH RUNS
# ---------------------------------------------------------------------------


async def job_facts(project: str, job: str) -> dict[str, Any]:
    """The live state of a batch Job and its pod.

    READ FROM THE CLUSTER ON EVERY REQUEST, like everything else here. A run
    that was killed by its deadline, evicted, or deleted behind Studio's back
    must say so the moment someone looks — the registry's memory of a finished
    pipeline is not evidence that anything still exists.
    """
    ns = f"p-{project}"
    rc, out = await _kubectl(["get", "job", job, "-n", ns, "-o", "json"])
    if rc != 0 or not out.strip():
        return {"exists": False, "state": None, "podPhase": None, "podReason": None}

    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        return {"exists": False, "state": None, "podPhase": None, "podReason": None}

    conditions = {
        c.get("type"): c for c in doc.get("status", {}).get("conditions", []) or []
    }
    if conditions.get("Complete", {}).get("status") == "True":
        state = "complete"
    elif conditions.get("Failed", {}).get("status") == "True":
        state = "failed"
    else:
        state = "running"

    # THE POD'S OWN VIEW, because a Job with no pod and a Job with a Pending pod
    # are the two different "waiting for GPU" cases (D10) and the Job object
    # alone cannot tell them apart.
    phase = reason = None
    rc, pods = await _kubectl(
        ["get", "pods", "-n", ns, "-l", f"job-name={job}", "-o", "json"]
    )
    if rc == 0 and pods.strip():
        try:
            items = json.loads(pods).get("items", [])
        except json.JSONDecodeError:
            items = []
        if items:
            status = items[0].get("status", {})
            phase = status.get("phase")
            for condition in status.get("conditions", []) or []:
                if condition.get("status") == "False" and condition.get("reason"):
                    reason = condition["reason"]
                    break

    return {
        "exists": True,
        "state": state,
        "reason": (conditions.get("Failed", {}) or {}).get("reason"),
        "podPhase": phase,
        "podReason": reason,
        "startTime": doc.get("status", {}).get("startTime"),
        "completionTime": doc.get("status", {}).get("completionTime"),
    }

