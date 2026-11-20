# ===========================================================================
# RAG TOOLKIT — THE RETRIEVAL SERVICE
# ===========================================================================
# The endpoint a PI actually calls. Everything else in a RAG project exists to
# make this one POST work.
#
#   GET  /healthz  -> Qdrant reachable AND the embedder loaded
#   POST /chat     -> {question, topK?} -> {answer, sources[], latencyMs, contextChars}
#
# ---------------------------------------------------------------------------
# THE ONE PROMISE THIS SERVICE MAKES
# ---------------------------------------------------------------------------
# EVERY ANSWER CARRIES ITS EVIDENCE. `sources` is not decoration: it is the
# difference between a research tool and a plausible-sounding oracle. A PI
# asking about their own documents must be able to check the answer against the
# passage it came from, and the chunk ids are stable (see chunker.py) so the
# passage can be found again.
#
# The second half of the promise is REFUSAL. When retrieval finds nothing, this
# service does not ask the model anyway and hope — it returns the honest
# "I don't find that in the indexed documents." WITHOUT CALLING THE MODEL AT
# ALL. Calling it with an empty context would produce an answer from the model's
# own training data, indistinguishable in tone from a grounded one, cited to
# nothing. That is the single worst failure a RAG system can have, and the only
# reliable prevention is not making the call.
#
# ---------------------------------------------------------------------------
# HOW IT REACHES THE CHAT MODEL
# ---------------------------------------------------------------------------
# The chat model is a Knative Service behind KServe. It has no ordinary
# ClusterIP name: Knative routes by HOST HEADER through a gateway, and the
# gateway is also what WAKES a scaled-to-zero revision. So this posts to
# `$CHAT_ENDPOINT` (Knative's cluster-local gateway) with `Host: $CHAT_HOST`.
# Both are injected by the pipeline — the host is read LIVE from the
# InferenceService's `.status.url` and never constructed, because local URLs
# carry a `-predictor` segment production will not have (DEVIATIONS.md).
#
# The 180s timeout is that wake. An on-demand project's model has no pods at all
# when idle; the first question pulls an image, loads a GGUF and starts a
# server. A timeout that looked more reasonable would turn the documented
# ~15-second cold start into an error.
#
# ---------------------------------------------------------------------------
# NO NETWORK EGRESS
# ---------------------------------------------------------------------------
# `models` is mounted read-only, the embedder loads with `local_files_only=True`,
# and `HF_HUB_OFFLINE=1` is set in the manifest as a second lock. The only
# outbound connections this process makes are to Qdrant and to the in-cluster
# gateway.
# ===========================================================================

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

# --- configuration, all from the manifest -----------------------------------
MODELS_DIR = os.environ.get("MODELS_DIR", "/mnt/models")
EMBED_SUBPATH = os.environ.get("EMBED_SUBPATH", "embed").strip()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "").strip()
EMBED_DIMS = os.environ.get("EMBED_DIMS", "").strip()
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").strip()
COLLECTION = os.environ.get("COLLECTION", "").strip()
TOP_K_DEFAULT = int(os.environ.get("TOP_K", "3"))

CHAT_ENDPOINT = os.environ.get("CHAT_ENDPOINT", "").strip().rstrip("/")
CHAT_HOST = os.environ.get("CHAT_HOST", "").strip()
CHAT_MODEL_ID = os.environ.get("CHAT_MODEL_ID", "").strip()

# See the header. This is a cold start, not a slow request.
CHAT_TIMEOUT_SECONDS = 180.0

# Bounded independently of the wizard's own 1-8 range: TOP_K comes from the
# manifest, but `topK` on a request comes from whoever is calling. Every extra
# passage is ~600 more characters of prompt, and the pinned runtime's context is
# 4096 tokens — so this is the ceiling that protects the model from silent
# truncation, enforced where the untrusted number arrives.
TOP_K_MAX = 8

# The instruction that makes the answers grounded. Written out here rather than
# assembled, so what the model is told is reviewable in one place.
#
# THE REFUSAL SENTENCE IS QUOTED EXACTLY so that the model's "I don't know" and
# this service's own zero-results answer are the SAME STRING. A caller — the
# playground, a PI's script — can then detect "not in the documents" without
# having to distinguish two spellings of it.
NOT_FOUND_ANSWER = "I don't find that in the indexed documents."

SYSTEM_PROMPT = (
    "You are a careful research assistant. Answer the user's question using ONLY "
    "the numbered context passages provided. Do not use any other knowledge. "
    "Cite the passage numbers you used, like [1] or [2]. "
    f'If the context does not contain the answer, reply exactly: "{NOT_FOUND_ANSWER}" '
    "Do not guess, and do not fill gaps from memory."
)


app = FastAPI(
    title="HIVE RAG service",
    summary="Retrieval-augmented answers over one project's own documents.",
    version="0.1.0",
)

# Loaded once, at first use, and held for the process lifetime. Module-level
# rather than per-request: constructing a TextEmbedding opens an ONNX session,
# which takes seconds and hundreds of megabytes.
_embedder: Any = None
_embedder_error: str | None = None


def embedder() -> Any:
    """The embedding model, loaded lazily and remembered — including a failure.

    LAZY, NOT AT IMPORT: uvicorn imports this module before it binds a port, and
    a model that fails to load at import time produces a container that exits
    with a traceback and no HTTP surface at all. Loading on first use means
    /healthz can answer 503 with the actual reason, which is what the readiness
    probe and the PI both need.

    The failure is CACHED. A missing cache directory will not fix itself, and
    retrying an ONNX load on every health check every five seconds would turn
    one clear error into a CPU-burning loop.
    """
    global _embedder, _embedder_error
    if _embedder is not None:
        return _embedder
    if _embedder_error is not None:
        raise RuntimeError(_embedder_error)

    cache_dir = os.path.normpath(os.path.join(MODELS_DIR, EMBED_SUBPATH))
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=EMBED_MODEL,
            cache_dir=cache_dir,
            # The query path must never download. See the module header.
            local_files_only=True,
        )
        # THE SAME CHECK THE INGESTION JOB MAKES, for the same reason and with
        # more at stake: if this process embedded questions with a different
        # width than the index was built with, Qdrant would reject the search —
        # which is the good case. A model with the same width but different
        # weights would return confident nonsense with no error anywhere.
        if EMBED_DIMS and int(EMBED_DIMS) != int(model.embedding_size):
            raise RuntimeError(
                f"embedding model {EMBED_MODEL} has {model.embedding_size} "
                f"dimensions, but this project's index was built for {EMBED_DIMS}"
            )
    except Exception as exc:  # noqa: BLE001
        _embedder_error = (
            f"could not load embedding model {EMBED_MODEL!r} from {cache_dir}: {exc}"
        )
        raise RuntimeError(_embedder_error) from exc

    _embedder = model
    return _embedder


def embed_one(text: str) -> list[float]:
    """One vector for one string.

    `query_embed`, not `embed`. fastembed distinguishes the two so a model that
    wants a query prefix gets one; bge-small-en-v1.5 does not need one today,
    and using the query API anyway means adding a model that does need one is a
    catalogue change rather than a code change.
    """
    return list(next(iter(embedder().query_embed(text))))


# ---------------------------------------------------------------------------
# HEALTH
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Both dependencies, actually exercised.

    NOT A 200 THAT MEANS "the process is running". The readiness probe in
    rag-api.yaml.tmpl points here, so what this returns decides whether the pod
    joins the Service — and a pod that joined before Qdrant was up, or with no
    embedding cache, would accept a question and fail it.

    The chat model is DELIBERATELY NOT CHECKED. On an on-demand project it is
    asleep by design, and waking it on every health check would defeat the
    entire archetype: the pod would never scale to zero, and the GPU would never
    return to the pool. A sleeping model is a healthy project.
    """
    problems: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{QDRANT_URL}/collections/{COLLECTION}")
        if response.status_code != 200:
            problems.append(
                f"qdrant: collection '{COLLECTION}' -> HTTP {response.status_code}"
            )
    except httpx.HTTPError as exc:
        problems.append(f"qdrant: unreachable at {QDRANT_URL} ({exc})")

    try:
        embedder()
    except Exception as exc:  # noqa: BLE001
        problems.append(str(exc))

    if problems:
        raise HTTPException(status_code=503, detail={"ok": False, "problems": problems})
    return {
        "ok": True,
        "collection": COLLECTION,
        "embedModel": EMBED_MODEL,
        "chatHost": CHAT_HOST,
    }


# ---------------------------------------------------------------------------
# CHAT
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    # Optional: the manifest's TOP_K is the project's default, and a caller may
    # override it per question. Bounded here because this number arrives from
    # outside — see TOP_K_MAX.
    topK: int | None = Field(default=None, ge=1, le=TOP_K_MAX)


def build_messages(question: str, passages: list[dict]) -> list[dict[str, str]]:
    """The prompt: one system message, one user message with numbered context.

    NUMBERED, because the system prompt asks the model to cite passage numbers
    and a model cannot cite what it cannot name. The numbers are 1-based and
    match the order of `sources` in the response, so "[2]" in an answer points
    at `sources[1]` — which is what makes a citation checkable.
    """
    blocks = []
    for index, passage in enumerate(passages, start=1):
        blocks.append(
            f"[{index}] (from {passage['source']})\n{passage['text']}"
        )
    context = "\n\n".join(blocks)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context passages:\n\n{context}\n\nQuestion: {question}",
        },
    ]


@app.post("/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    started = time.monotonic()
    top_k = req.topK or TOP_K_DEFAULT

    # --- retrieve ---------------------------------------------------------
    try:
        from qdrant_client import QdrantClient

        vector = embed_one(req.question)
        client = QdrantClient(url=QDRANT_URL, timeout=30)
        # `query_points`, not `search`: `search` was REMOVED from qdrant-client
        # by 1.19.0. Verified against the installed package, not recalled.
        found = client.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=top_k,
            with_payload=True,
        ).points
    except RuntimeError as exc:
        # The embedder failed to load. 503, not 500: it is a dependency that is
        # missing, and the readiness probe is already reporting it.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"could not search the index: {exc}"
        ) from exc

    passages = [
        {
            "chunk_id": (point.payload or {}).get("chunk_id", ""),
            "source": (point.payload or {}).get("source", ""),
            "score": round(float(point.score), 4),
            "text": (point.payload or {}).get("text", ""),
        }
        for point in found
    ]

    # --- the honest refusal, WITHOUT calling the model --------------------
    # See the module header: an empty context plus a chat model is an answer
    # from the model's training data wearing the costume of a grounded one.
    if not passages:
        return {
            "answer": NOT_FOUND_ANSWER,
            "sources": [],
            "latencyMs": int((time.monotonic() - started) * 1000),
            "contextChars": 0,
            # Flagged explicitly so a caller can distinguish "the index has
            # nothing about this" from "the model declined". Both produce the
            # same sentence, on purpose, and only this field separates them.
            "modelCalled": False,
        }

    messages = build_messages(req.question, passages)
    context_chars = sum(len(p["text"]) for p in passages)

    # --- ask the model ----------------------------------------------------
    if not CHAT_ENDPOINT or not CHAT_HOST:
        raise HTTPException(
            status_code=503,
            detail=(
                "this service was deployed without a chat endpoint; CHAT_ENDPOINT "
                "and CHAT_HOST are set by the pipeline's rag-api step from the "
                "InferenceService's live status"
            ),
        )

    payload = {
        "model": CHAT_MODEL_ID,
        "messages": messages,
        # Low, not zero. Retrieval-grounded answering wants the model to follow
        # its instructions rather than be creative; 0.2 leaves enough room to
        # paraphrase a passage instead of copying it verbatim.
        "temperature": 0.2,
        # No streaming. A streamed reply would need an SSE relay through this
        # service and then through the BFF, and the first thing worth proving is
        # that the grounded round trip works at all.
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{CHAT_ENDPOINT}/v1/chat/completions",
                json=payload,
                # THE Host HEADER IS THE ROUTING. Knative's gateway has one
                # address and decides which service a request belongs to
                # entirely from this header.
                headers={"Host": CHAT_HOST, "Content-Type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"the chat model did not answer within {int(CHAT_TIMEOUT_SECONDS)}s. "
                f"If the project is on demand, the first question after an idle "
                f"period also has to start the model."
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"could not reach the chat model: {exc}"
        ) from exc

    if response.status_code != 200:
        # The upstream body is passed through: a gateway 404 (wrong Host header)
        # and a runtime error look nothing alike, and the person debugging needs
        # to see which one happened.
        raise HTTPException(
            status_code=502,
            detail=f"chat model returned HTTP {response.status_code}: {response.text[:500]}",
        )

    try:
        body = response.json()
        answer = body["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"chat model returned an unexpected body: {response.text[:500]}",
        ) from exc

    return {
        "answer": answer,
        "sources": passages,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "contextChars": context_chars,
        "modelCalled": True,
    }
