#!/usr/bin/env python
# ===========================================================================
# RENDERER TESTS — EVERY TEMPLATE, NO CLUSTER
# ===========================================================================
#   studio/backend/.venv/bin/python tests/test-render.py
#
# WHAT THIS IS FOR. app/render.py is pure by design: hand it inputs and a clock
# and it returns text. That is what makes this file possible — every assertion
# below runs with Docker stopped, and it takes under a second.
#
# The claim being tested is narrow and worth stating: THE RENDERER PRODUCES
# VALID, CORRECT KUBERNETES YAML FOR EVERY TEMPLATE THAT PROVISIONS. Not that the cluster
# accepts it, not that the workload runs — those need a cluster and are what
# `scripts/studio-smoke.sh` and a real deployment are for.
#
# It is also the regression guard on the RAG work: `inference-endpoint` had a
# working renderer before rag-stack existed, and several shared functions
# (facts_for, values_for, render_all) changed shape to accommodate it. Half the
# assertions here exist to prove that template renders exactly as it did.
# ===========================================================================

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "studio" / "backend"))

import yaml  # noqa: E402

from app import render  # noqa: E402
from app.catalog import load_catalog  # noqa: E402

# Fixed, so expiry arithmetic is deterministic and a failure is never a
# midnight-rollover flake.
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}")
        if detail:
            print(f"        {detail}")


def docs(text: str) -> list[dict]:
    """Every YAML document in a rendered fragment, empties dropped."""
    return [d for d in yaml.safe_load_all(text) if d]



# ---------------------------------------------------------------------------
# THE PVC CO-LOCATION FENCE
# ---------------------------------------------------------------------------


def check_pvc_fence(by_name: dict[str, str]) -> None:
    """The staging pod must mount `models`, or ingestion becomes unschedulable.

    local-path is ReadWriteOnce and pins each claim to whichever node first
    mounts it. The seed Job pins `models`; if the staging pod mounted only
    `scratch` it would be free to land on the other worker and pin `scratch`
    there. The ingestion Job mounts BOTH and would then never schedule —
    Pending for ever, timing out at 900s with an EMPTY job log because nothing
    ran.

    That happened, on project `abc`, after several deployments had got away with
    it by luck. These assertions keep the fence in place.
    """
    stage = docs(by_name["corpus-staging-pod.yaml"])[0]
    mounts = {m["name"]: m for m in stage["spec"]["containers"][0]["volumeMounts"]}
    stage_claims = {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in stage["spec"]["volumes"]
        if "persistentVolumeClaim" in v
    }

    check("staging pod mounts scratch read-write", mounts["scratch"].get("readOnly") is not True)
    check("staging pod ALSO mounts models, as a scheduling fence", "models" in mounts)
    check("...and never writes to it", mounts.get("models", {}).get("readOnly") is True)
    check("...from the real models claim", stage_claims.get("models") == "models")

    # Stated as a RELATIONSHIP rather than a fixed list, so a claim added to
    # ingestion later fails here instead of failing as an unschedulable pod
    # fifteen minutes into a deployment.
    ingest = docs(by_name["ingestion-job.yaml"])[0]
    ingest_claims = {
        v["persistentVolumeClaim"]["claimName"]
        for v in ingest["spec"]["template"]["spec"]["volumes"]
        if "persistentVolumeClaim" in v
    }
    check(
        f"staging pod pins every claim ingestion needs ({sorted(ingest_claims)})",
        ingest_claims <= set(stage_claims.values()),
        f"ingestion needs {sorted(ingest_claims)}, staging pins {sorted(set(stage_claims.values()))}",
    )


def main() -> int:
    catalog = load_catalog()

    # =====================================================================
    # inference-endpoint — THE REGRESSION GUARD
    # =====================================================================
    print("\n==> inference-endpoint (unchanged by the RAG work)\n")

    template = catalog.resolved_template_model("inference-endpoint")
    params = {
        "projectName": "regress",
        "model": "sklearn-iris",
        "availability": "on-demand",
        "tier": "small",
        "durationDays": 14,
    }
    files = render.render_all(template, params, catalog.model("sklearn-iris"), NOW)
    by_name = {f["name"]: f["content"] for f in files}

    check(
        "three manifests, under the names the executor writes",
        sorted(by_name) == ["inference-service.yaml", "model-seed-job.yaml", "namespace-pack.yaml"],
        f"got {sorted(by_name)}",
    )

    for name, text in by_name.items():
        try:
            parsed = docs(text)
            check(f"{name} is valid YAML ({len(parsed)} document(s))", bool(parsed))
        except yaml.YAMLError as exc:
            check(f"{name} is valid YAML", False, str(exc))

    ns_docs = docs(by_name["namespace-pack.yaml"])
    quotas = [d for d in ns_docs if d["kind"] == "ResourceQuota"]
    check("exactly one tier's ResourceQuota survives pass 1", len(quotas) == 1,
          f"got {len(quotas)}")
    check("expiry is RFC3339 UTC, provisioning time + durationDays",
          ns_docs[0]["metadata"]["annotations"]["hive.ksl/expiry"] == "2026-09-14T12:00:00Z",
          ns_docs[0]["metadata"]["annotations"]["hive.ksl/expiry"])

    isvc = docs(by_name["inference-service.yaml"])[0]
    predictor = isvc["spec"]["predictor"]
    check("on-demand renders minReplicas 0", predictor["minReplicas"] == 0)
    check("on-demand keeps the T-SZ annotations",
          isvc["metadata"]["annotations"].get("autoscaling.knative.dev/scale-down-delay") == "60s")
    check("an empty catalogue runtime omits the key ENTIRELY, not blank",
          "runtime" not in predictor["model"],
          str(predictor["model"].get("runtime")))
    check('storageUri is "pvc://models" with no trailing slash',
          predictor["model"]["storageUri"] == "pvc://models",
          predictor["model"]["storageUri"])

    always = render.render_all(
        template, {**params, "availability": "always-on"}, catalog.model("sklearn-iris"), NOW
    )
    always_isvc = docs([f for f in always if f["name"] == "inference-service.yaml"][0]["content"])[0]
    check("always-on renders minReplicas 1", always_isvc["spec"]["predictor"]["minReplicas"] == 1)
    check("always-on has NO scale-down annotations",
          "autoscaling.knative.dev/scale-down-delay" not in always_isvc["metadata"]["annotations"])

    # =====================================================================
    # rag-stack
    # =====================================================================
    print("\n==> rag-stack\n")

    template = catalog.resolved_template_model("rag-stack")
    chat = catalog.model("qwen2.5-0.5b-instruct-q4")
    embed = catalog.model("bge-small-en-v1.5")
    params = {
        "projectName": "ragdemo",
        "baseModel": "qwen2.5-0.5b-instruct-q4",
        "embeddingModel": "bge-small-en-v1.5",
        "vectorDb": "qdrant",
        "corpusUploadId": "",
        "topK": 3,
        "availability": "on-demand",
        "tier": "small",
        "durationDays": 30,
    }
    files = render.render_all(template, params, chat, NOW, embed_model=embed)
    by_name = {f["name"]: f["content"] for f in files}

    check(
        "seven manifests, named for their FRAGMENTS not their resources",
        sorted(by_name) == [
            "corpus-staging-pod.yaml", "inference-service.yaml", "ingestion-job.yaml",
            "model-seed.yaml", "namespace-pack.yaml", "qdrant.yaml", "rag-api.yaml",
        ],
        f"got {sorted(by_name)}",
    )

    check_pvc_fence(by_name)

    kinds: dict[str, list[str]] = {}
    for name, text in by_name.items():
        try:
            parsed = docs(text)
            kinds[name] = [d["kind"] for d in parsed]
            check(f"{name} is valid YAML ({len(parsed)} document(s))", bool(parsed))
        except yaml.YAMLError as exc:
            check(f"{name} is valid YAML", False, str(exc))

    # Every rendered object must carry apiVersion/kind/metadata.name, which is
    # the minimum kubectl will accept. A fragment that lost one would still be
    # valid YAML and would fail at apply time, three steps into a deployment.
    incomplete = []
    for name, text in by_name.items():
        for doc in docs(text):
            if not (doc.get("apiVersion") and doc.get("kind") and doc.get("metadata", {}).get("name")):
                incomplete.append(f"{name}:{doc.get('kind')}")
    check("every object has apiVersion, kind and metadata.name", not incomplete, str(incomplete))

    ns_docs = docs(by_name["namespace-pack.yaml"])
    namespace = ns_docs[0]
    check("namespace is p-<project>", namespace["metadata"]["name"] == "p-ragdemo")
    check("labelled hive.ksl/template=studio-rag — the ownership check",
          namespace["metadata"]["labels"]["hive.ksl/template"] == "studio-rag")
    quotas = [d for d in ns_docs if d["kind"] == "ResourceQuota"]
    check("exactly one tier's ResourceQuota survives pass 1", len(quotas) == 1)
    hard = quotas[0]["spec"]["hard"]
    check("rag-small profile: requests.storage 10Gi", hard["requests.storage"] == "10Gi", str(hard))
    check("rag-small profile: persistentvolumeclaims 6", hard["persistentvolumeclaims"] == "6")
    check("GPU ceiling unchanged from the tier (tier+1 for Knative's dry run)",
          hard["requests.nvidia.com/gpu"] == "2")

    claims = [d["metadata"]["name"] for d in ns_docs if d["kind"] == "PersistentVolumeClaim"]
    check("three PVCs: models, scratch, qdrant-data",
          sorted(claims) == ["models", "qdrant-data", "scratch"], str(claims))
    # The four claims the quota budgets are these three plus the vCluster's own
    # 5Gi etcd volume: 1 + 1 + 2 + 5 = 9Gi against a 10Gi ceiling.
    total = sum(
        int(d["spec"]["resources"]["requests"]["storage"].rstrip("Gi"))
        for d in ns_docs if d["kind"] == "PersistentVolumeClaim"
    )
    check("declared claims + the vCluster's 5Gi fit inside requests.storage",
          total + 5 <= int(hard["requests.storage"].rstrip("Gi")),
          f"{total}Gi declared + 5Gi vCluster vs {hard['requests.storage']}")

    qdrant = docs(by_name["qdrant.yaml"])
    check("qdrant.yaml is a Deployment and a Service, NOT a StatefulSet",
          [d["kind"] for d in qdrant] == ["Deployment", "Service"], str(kinds["qdrant.yaml"]))
    check("Qdrant is single-replica", qdrant[0]["spec"]["replicas"] == 1)
    check("Recreate strategy — a rolling update cannot reattach an RWO volume",
          qdrant[0]["spec"]["strategy"]["type"] == "Recreate")
    qc = qdrant[0]["spec"]["template"]["spec"]["containers"][0]
    check("readiness probes /readyz (verified against v1.19.0; /health is a 404)",
          qc["readinessProbe"]["httpGet"]["path"] == "/readyz",
          qc["readinessProbe"]["httpGet"]["path"])
    check("the PVC is mounted at /qdrant/storage",
          qc["volumeMounts"][0]["mountPath"] == "/qdrant/storage")
    check("Qdrant Service publishes both 6333 and 6334",
          sorted(p["port"] for p in qdrant[1]["spec"]["ports"]) == [6333, 6334])

    isvc = docs(by_name["inference-service.yaml"])[0]
    predictor = isvc["spec"]["predictor"]
    check("the RAG isvc PINS the >=4096-context runtime, not the model's own",
          predictor["model"]["runtime"] == render.RAG_CHAT_RUNTIME,
          predictor["model"].get("runtime"))
    # THIS CHECK USED TO ASSERT THE TWO DIFFERED, and it was a weak test that
    # passed for an accidental reason. models.yaml pinned `hive-llama-cpp`, a
    # runtime that was never authored and has never existed on this cluster, so
    # "the fragment pins something else" was guaranteed by the catalogue being
    # broken rather than by the fragment doing its job.
    #
    # The catalogue entry now names the gguf runtime that actually exists
    # (studio B1), so the two agree — and the real property is unchanged and is
    # asserted above: the RAG fragment pins the runtime ITSELF, from
    # render.RAG_CHAT_RUNTIME, rather than reading it from the model. What
    # matters is that the rendered value does not vary with the catalogue, which
    # is what this now checks.
    check("the RAG pin comes from the fragment, not from models.yaml",
          predictor["model"]["runtime"] == render.RAG_CHAT_RUNTIME
          and render.required_runtime("rag-stack", chat) == render.RAG_CHAT_RUNTIME,
          f"rendered {predictor['model'].get('runtime')}, models.yaml says {chat.runtime}")
    check("min-scale follows availability", predictor["minReplicas"] == 0)
    check("still asks for exactly one GPU on the one request surface",
          predictor["model"]["resources"]["limits"]["nvidia.com/gpu"] == "1")
    check("chat storageUri is the joined pvc:// form",
          predictor["model"]["storageUri"] == "pvc://models/llm",
          predictor["model"]["storageUri"])

    ingest_env = {
        e["name"]: e["value"]
        for e in docs(by_name["ingestion-job.yaml"])[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    rag_env = {
        e["name"]: e["value"]
        for e in docs(by_name["rag-api.yaml"])[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    # THE CORRECTNESS CONDITION OF RETRIEVAL. If the index and the query used
    # different embeddings, every search would return confident nonsense with no
    # error anywhere in the system. Both are rendered from the ONE catalogue
    # entry the PI chose, and this is the assertion that says so.
    check("ingestion and the RAG service use the SAME embedding model",
          ingest_env["EMBED_MODEL"] == rag_env["EMBED_MODEL"] == embed.fastembedName,
          f"{ingest_env['EMBED_MODEL']} vs {rag_env['EMBED_MODEL']}")
    check("...the same dimensions", ingest_env["EMBED_DIMS"] == rag_env["EMBED_DIMS"] == "384")
    check("...the same cache directory", ingest_env["EMBED_SUBPATH"] == rag_env["EMBED_SUBPATH"])
    check("...and the same collection",
          ingest_env["COLLECTION"] == rag_env["COLLECTION"] == "ragdemo-corpus")
    check("the collection name is derived from the project",
          render.collection_name("ragdemo") == "ragdemo-corpus")
    check("topK reaches the service", rag_env["TOP_K"] == "3")
    check("the query path is pinned offline", rag_env["HF_HUB_OFFLINE"] == "1")

    # The preview is rendered before the chat model exists, so its hostname
    # cannot be known. It must be the documented sentinel and NOT an empty
    # string, which would render a Host header of "" and route nowhere.
    check("dry-run renders the documented pending-host sentinel",
          rag_env["CHAT_HOST"] == render.PENDING_CHAT_HOST, rag_env["CHAT_HOST"])
    check("the sentinel uses the reserved .invalid TLD, so it can never resolve",
          render.PENDING_CHAT_HOST.endswith(".invalid"))

    deployed = render.render_all(
        template, params, chat, NOW, embed_model=embed,
        extras={"chat_host": "ragdemo-predictor.p-ragdemo.hive.local",
                "chat_endpoint": "http://kourier-internal.kourier-system.svc.cluster.local"},
    )
    deployed_env = {
        e["name"]: e["value"]
        for e in docs([f for f in deployed if f["name"] == "rag-api.yaml"][0]["content"])[0]
        ["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    check("a real deploy substitutes the live host read from .status.url",
          deployed_env["CHAT_HOST"] == "ragdemo-predictor.p-ragdemo.hive.local")
    check("...and the discovered cluster-local gateway",
          deployed_env["CHAT_ENDPOINT"].startswith("http://kourier-internal."))

    # Everything built locally must say Never, or the kubelet tries Docker Hub
    # for an image that exists in no registry and the pod sits in
    # ImagePullBackOff — a failure that looks like a network problem.
    local_image_pods = []
    for name, text in by_name.items():
        for doc in docs(text):
            spec = doc.get("spec", {})
            pod = spec.get("template", {}).get("spec") or (spec if doc["kind"] == "Pod" else {})
            for container in pod.get("containers", []) or []:
                if container["image"] == render.RAG_TOOLKIT_IMAGE:
                    local_image_pods.append((name, container.get("imagePullPolicy")))
    check("every pod using the locally-built image sets imagePullPolicy: Never",
          local_image_pods and all(p == "Never" for _, p in local_image_pods),
          str(local_image_pods))

    # =====================================================================
    # RENDER ERRORS ARE ERRORS, NOT BLANK MANIFESTS
    # =====================================================================
    # batch-inference
    # =====================================================================
    # THE WHOLE TEMPLATE COULD NOT RENDER, and nothing here noticed because
    # nothing here asked. `namespace-pack` computed its expiry from
    # `durationDays`, which a RUN does not declare; and the template asked for
    # the resource `model-seed`, which is rag-stack's TWO-model seed Job and
    # demanded an embedding model this template never offers. Both failures were
    # invisible in mock mode, which renders nothing, so a batch deployment
    # looked fine right up to the moment it touched a cluster.
    print("\n==> batch-inference\n")

    template = catalog.resolved_template_model("batch-inference")
    iris = catalog.model("sklearn-iris")
    params = {
        "projectName": "batchdemo",
        "model": "sklearn-iris",
        "datasetUploadId": "",
        "inputFormat": "csv",
        "inputFields": ["sepal_length", "sepal_width", "petal_length", "petal_width"],
        "promptTemplate": "",
        "maxRows": 200,
        "batchSize": 64,
        "concurrency": 2,
        "maxFailureRatePct": 10,
        "tier": "small",
        "runTimeoutMinutes": 60,
        "artifactRetentionDays": 14,
    }
    # The runtime block is the executor's one cluster read, handed to a pure
    # renderer. Shaped exactly as render.runtime_container returns it, so this
    # exercises the fragment without inventing a different contract.
    runtime = render.runtime_container(
        {
            "metadata": {"name": "kserve-sklearnserver"},
            "spec": {
                "containers": [
                    {
                        "name": "kserve-container",
                        "image": "kserve/sklearnserver:v0.20.0",
                        "args": ["--model_name={{.Name}}", "--model_dir=/mnt/models",
                                 "--http_port=8080"],
                        "resources": {
                            "requests": {"cpu": "500m", "memory": "1Gi"},
                            "limits": {"cpu": "2", "memory": "3Gi"},
                        },
                    }
                ]
            },
        },
        # What the executor passes: the project, standing in for the
        # InferenceService name KServe would have supplied.
        "batchdemo",
    )
    files = render.render_all(template, params, iris, NOW, extras={"runtime": runtime})
    by_name = {f["name"]: f["content"] for f in files}

    check(
        "five manifests, each under a DISTINCT name the executor also writes",
        sorted(by_name) == [
            "batch-job.yaml", "model-seed-job.yaml", "namespace-pack.yaml",
            "staging-pod-ro.yaml", "staging-pod-rw.yaml",
        ],
        f"got {sorted(by_name)}",
    )
    check(
        "the staging pod is rendered TWICE and neither render overwrites the other",
        len(files) == len(by_name),
        f"{len(files)} rendered, {len(by_name)} distinct names",
    )

    for name, text in by_name.items():
        try:
            parsed = docs(text)
            check(f"{name} is valid YAML ({len(parsed)} document(s))", bool(parsed))
        except yaml.YAMLError as exc:
            check(f"{name} is valid YAML", False, str(exc))

    ns_docs = docs(by_name["namespace-pack.yaml"])
    check(
        "a RUN takes its provisioning expiry from artifactRetentionDays, not durationDays",
        ns_docs[0]["metadata"]["annotations"]["hive.ksl/expiry"] == "2026-09-14T12:00:00Z",
        ns_docs[0]["metadata"]["annotations"]["hive.ksl/expiry"],
    )
    check(
        "the namespace is labelled studio-batch, which the delete guard allows",
        ns_docs[0]["metadata"]["labels"]["hive.ksl/template"] == "studio-batch",
        ns_docs[0]["metadata"]["labels"]["hive.ksl/template"],
    )
    check("exactly one tier's ResourceQuota survives pass 1",
          len([d for d in ns_docs if d["kind"] == "ResourceQuota"]) == 1)

    seed = docs(by_name["model-seed-job.yaml"])[0]
    check(
        "the seed Job fetches the catalogue's URL and nothing else",
        iris.seedUrl in by_name["model-seed-job.yaml"],
        iris.seedUrl,
    )
    check("the seed Job is a Job", seed["kind"] == "Job")

    job = docs(by_name["batch-job.yaml"])[0]
    sidecar = job["spec"]["template"]["spec"]["initContainers"][0]
    driver = job["spec"]["template"]["spec"]["containers"][0]
    check(
        "the runtime runs as a NATIVE SIDECAR — restartPolicy Always on an initContainer",
        sidecar["restartPolicy"] == "Always",
        str(sidecar.get("restartPolicy")),
    )
    check(
        "the sidecar is the CURATED image, carried through from the live runtime",
        sidecar["image"] == runtime["image"],
        sidecar["image"],
    )
    check(
        "the GPU is requested only as limits['nvidia.com/gpu'] (CLAUDE.md §2.3)",
        sidecar["resources"]["limits"]["nvidia.com/gpu"] == "1"
        and "nvidia.com/gpu" not in sidecar["resources"].get("requests", {}),
        str(sidecar["resources"]),
    )
    check(
        "the Job stops itself at the PI's own runTimeoutMinutes",
        job["spec"]["activeDeadlineSeconds"] == 60 * 60,
        str(job["spec"]["activeDeadlineSeconds"]),
    )
    env = {e["name"]: e["value"] for e in driver["env"]}
    check(
        "the chosen columns reach the driver IN ORDER",
        env["INPUT_FIELDS"] == "sepal_length,sepal_width,petal_length,petal_width",
        env["INPUT_FIELDS"],
    )
    check(
        "the driver talks to the sidecar over localhost — no gateway, no Host header",
        env["RUNTIME_URL"] == "http://127.0.0.1:8080",
        env["RUNTIME_URL"],
    )
    check(
        "the locally-built driver image sets imagePullPolicy: Never",
        driver["imagePullPolicy"] == "Never",
        str(driver.get("imagePullPolicy")),
    )

    # THE NAME THE RUNTIME ANSWERS TO, AND THE NAME THE DRIVER ASKS FOR, ARE
    # THE SAME STRING. `--model_name={{.Name}}` is rendered with the project,
    # and KServe V1 puts that same name in the path — so a driver told the
    # CATALOGUE id instead posts to /v1/models/sklearn-iris:predict against a
    # runtime serving /v1/models/batchdemo:predict, and every row 404s while
    # the run reports "your dataset is mostly failing".
    check(
        "the sidecar is started under the project name",
        "--model_name=batchdemo" in sidecar["args"],
        str(sidecar["args"]),
    )
    check(
        "...and the driver is told that same name, not the catalogue's id",
        env["MODEL_SERVING_NAME"] == "batchdemo" and env["MODEL_ID"] == "sklearn-iris",
        f"serving={env.get('MODEL_SERVING_NAME')} id={env.get('MODEL_ID')}",
    )
    check(
        "the V1 predict path the driver builds is the one the runtime serves",
        env["MODEL_INVOKE_PATH"].replace("{name}", env["MODEL_SERVING_NAME"])
        == "/v1/models/batchdemo:predict",
        env["MODEL_INVOKE_PATH"].replace("{name}", env["MODEL_SERVING_NAME"]),
    )
    check(
        "the manifest names the driver tag the build script builds",
        driver["image"] == render.BATCH_DRIVER_IMAGE,
        driver["image"],
    )

    stage_in = docs(by_name["staging-pod-rw.yaml"])[0]
    stage_out = docs(by_name["staging-pod-ro.yaml"])[0]
    check(
        "the two staging pods are named for their DIRECTION, not for the fragment",
        (stage_in["metadata"]["name"], stage_out["metadata"]["name"])
        == ("batchdemo-stage-in", "batchdemo-stage-out"),
        f"{stage_in['metadata']['name']} / {stage_out['metadata']['name']}",
    )

    def scratch_mount(pod):
        container = pod["spec"]["containers"][0]
        return next(m for m in container["volumeMounts"] if m["name"] == "scratch")

    check(
        "the dataset goes in on a WRITABLE scratch mount",
        not scratch_mount(stage_in).get("readOnly"),
        str(scratch_mount(stage_in)),
    )
    check(
        "the results come out on a READ-ONLY one, so collection cannot overwrite them",
        scratch_mount(stage_out).get("readOnly") is True,
        str(scratch_mount(stage_out)),
    )

    # A run has no endpoint, so nothing here may render an InferenceService.
    check(
        "a run creates no InferenceService — it has no endpoint to serve",
        not any(d["kind"] == "InferenceService"
                for text in by_name.values() for d in docs(text)),
    )

    # =====================================================================
    print("\n==> failure modes\n")

    rag = catalog.resolved_template_model("rag-stack")
    rag_params = {
        "projectName": "ragdemo", "baseModel": "qwen2.5-0.5b-instruct-q4",
        "embeddingModel": "bge-small-en-v1.5", "vectorDb": "qdrant",
        "corpusUploadId": "", "topK": 3, "availability": "on-demand",
        "tier": "small", "durationDays": 30,
    }
    try:
        render.render_all(rag, rag_params, chat, NOW, embed_model=None)
        check("a missing embedding model is refused, not rendered blank", False,
              "render_all succeeded with embed_model=None")
    except render.RenderError as exc:
        check("a missing embedding model is refused, not rendered blank",
              "embedding model" in str(exc), str(exc))

    # EVERY PROVISIONED NAMESPACE GETS AN EXPIRY. A namespace with none is the
    # failure worth designing against: the host reconciler would never collect
    # it, and the project would outlive its purpose holding a GPU quota.
    try:
        render.render_fragment(
            "namespace-pack", "inference-endpoint", "noclock",
            {"tier": "small"}, iris, NOW,
        )
        check("a submission with no expiry clock is refused, not rendered blank", False)
    except render.RenderError as exc:
        check("a submission with no expiry clock is refused, not rendered blank",
              "expiry clock" in str(exc), str(exc))

    # The batch Job's serving container is not described in this repository. A
    # render that quietly produced one would be inventing the thing the whole
    # template exists to avoid inventing.
    try:
        render.render_fragment("batch-job", "batch-inference", "batchdemo",
                               params, iris, NOW, extras={})
        check("a batch Job will not render without the live runtime", False)
    except render.RenderError as exc:
        check("a batch Job will not render without the live runtime",
              "ClusterServingRuntime" in str(exc), str(exc))

    try:
        render.substitute("$nope", {}, "<test>")
        check("an unresolved placeholder raises rather than reaching the cluster", False)
    except render.RenderError:
        check("an unresolved placeholder raises rather than reaching the cluster", True)

    try:
        render.apply_conditionals("#@if bogus=1\nx\n#@end\n", {"tier": "small"}, "<test>")
        check("an #@if on an unknown fact raises rather than silently dropping", False)
    except render.RenderError:
        check("an #@if on an unknown fact raises rather than silently dropping", True)

    print(f"\n==> {PASS} passed, {FAIL} failed\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
