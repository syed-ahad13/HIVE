#!/usr/bin/env python
# ===========================================================================
# ONE ASSERTION OVER A rag-stack DRY RUN
# ===========================================================================
#   studio/backend/.venv/bin/python tests/assert-rag-dryrun.py <body.json> <project>
#
# Called by scripts/studio-smoke.sh against the body of
# POST /api/deployments?dry_run=1. A separate file rather than a heredoc inside
# the shell script for one practical reason: the assertions are about YAML that
# contains `$`, quotes and backticks, and a heredoc three levels deep is where
# those go wrong silently.
#
# WHAT IT PROVES. The review screen a PI approves shows exactly the manifests
# that will be applied — so these are the claims that matter about the preview:
# the right FILES, under the names the executor writes; the right STEPS, in the
# order that makes rag-api possible; and the right CONTENT in the places where
# getting it wrong would produce a project that provisions and then does not
# work.
#
# It prints every failure rather than stopping at the first, so one run tells
# you everything that is wrong with the render.
# ===========================================================================

from __future__ import annotations

import json
import re
import sys

EXPECTED_FILES = [
    "corpus-staging-pod.yaml",
    "inference-service.yaml",
    "ingestion-job.yaml",
    "model-seed.yaml",
    "namespace-pack.yaml",
    "qdrant.yaml",
    "rag-api.yaml",
]

# ready-check sits BETWEEN inference-service and rag-api, not at the end. That
# is the whole reason pipeline.STEP_GATES exists: rag-api needs the chat model's
# live hostname, and only ready-check can read it.
EXPECTED_STEPS = [
    "approve", "namespace-pack", "model-seed", "vcluster", "vector-db",
    "corpus-upload", "ingestion", "inference-service", "ready-check",
    "rag-api", "rag-ready-check",
]


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: assert-rag-dryrun.py <body.json> <project>", file=sys.stderr)
        return 2
    body = json.load(open(sys.argv[1]))
    project = sys.argv[2]

    failures: list[str] = []

    def want(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            failures.append(f"{label}{': ' + detail if detail else ''}")

    want("dryRun flag", body.get("dryRun") is True)
    want("no registry id was minted for a preview", body.get("id") == project)

    names = sorted(f["name"] for f in body.get("files", []))
    want("manifest set", names == EXPECTED_FILES, f"got {names}")
    want("step plan", body.get("steps") == EXPECTED_STEPS, f"got {body.get('steps')}")

    by = {f["name"]: f["content"] for f in body.get("files", [])}
    if names != EXPECTED_FILES:
        # Content assertions would all fail confusingly on a missing file.
        for line in failures:
            print(f"FAIL: {line}")
        return 1

    ns, qd = by["namespace-pack.yaml"], by["qdrant.yaml"]
    ing, isvc, rag = by["ingestion-job.yaml"], by["inference-service.yaml"], by["rag-api.yaml"]
    seed = by["model-seed.yaml"]

    # --- the host-side ground ---------------------------------------------
    want("expiry stamped RFC3339 UTC",
         bool(re.search(r'hive\.ksl/expiry: "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"', ns)))
    want("ownership label is studio-rag — this is what teardown checks",
         "hive.ksl/template: studio-rag" in ns)
    want("exactly one tier's ResourceQuota survived pass 1",
         ns.count("kind: ResourceQuota") == 1, f"found {ns.count('kind: ResourceQuota')}")
    want("rag-small quota profile", "requests.storage: 10Gi" in ns and 'persistentvolumeclaims: "6"' in ns)
    want("the third PVC exists", "name: qdrant-data" in ns)
    want("GPU ceiling is still tier+1 for Knative's dry run",
         'requests.nvidia.com/gpu: "2"' in ns)

    # --- the vector database -----------------------------------------------
    want("Qdrant is a Deployment, not a StatefulSet", "kind: StatefulSet" not in qd)
    want("Recreate strategy — an RWO volume cannot be reattached mid-rollout",
         "type: Recreate" in qd)
    want("readiness probes /readyz, verified against v1.19.0", "path: /readyz" in qd)
    want("the index volume is mounted where Qdrant looks for it",
         "mountPath: /qdrant/storage" in qd)

    # --- the chat model -----------------------------------------------------
    want("the >=4096-context runtime is PINNED by the template",
         "runtime: hive-llama-cpp-rag" in isvc)
    want("one GPU, on the one request surface", 'nvidia.com/gpu: "1"' in isvc)
    want("on-demand renders BOTH the annotation and minReplicas",
         'min-scale: "0"' in isvc and "minReplicas: 0" in isvc)
    want("the chat model is mounted from the models PVC subpath",
         'storageUri: "pvc://models/llm"' in isvc)

    # --- the toolkit --------------------------------------------------------
    for name, text in (("ingestion", ing), ("rag-api", rag), ("model-seed", seed)):
        want(f"{name} sets imagePullPolicy: Never (the image is in no registry)",
             "imagePullPolicy: Never" in text)

    # THE CORRECTNESS CONDITION OF RETRIEVAL, asserted on the rendered bytes:
    # index and query must use the same embedding model and the same collection,
    # or every search returns confident nonsense with no error anywhere.
    want("ingestion and rag-api agree on the embedding model",
         'value: "BAAI/bge-small-en-v1.5"' in ing and 'value: "BAAI/bge-small-en-v1.5"' in rag)
    want("ingestion and rag-api agree on the collection",
         f'value: "{project}-corpus"' in ing and f'value: "{project}-corpus"' in rag)
    want("the query path is pinned offline", 'name: HF_HUB_OFFLINE' in rag)
    want("the models volume is read-only wherever it is not being written",
         ing.count("readOnly: true") >= 1 and rag.count("readOnly: true") >= 1)

    # The preview cannot know the chat model's hostname — it is read live from
    # .status.url at deploy time — so it must show the documented sentinel, not
    # an empty string that would render a Host header of "".
    want("the preview shows the documented pending-host sentinel",
         "resolved-at-deploy-time.invalid" in rag)

    for line in failures:
        print(f"FAIL: {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
