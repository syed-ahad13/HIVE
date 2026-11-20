# ===========================================================================
# RAG TOOLKIT — THE INGESTION ENTRYPOINT
# ===========================================================================
# Reads the PI's corpus off the `scratch` PVC, chunks it, embeds every chunk on
# CPU, and upserts the lot into the project's Qdrant collection. Run once per
# deployment as the pipeline's `ingestion` step.
#
# ---------------------------------------------------------------------------
# IT PRINTS ONE MACHINE-READABLE LINE, AND EVERYTHING ELSE IS FOR A HUMAN
# ---------------------------------------------------------------------------
# The last line of a successful run is exactly:
#
#     INGEST_RESULT {"files":N,"chunks":M,"collection":"...","dims":D}
#
# The executor parses that line out of `kubectl logs` and turns it into the
# deployment's `indexedChunks` / `indexedFiles` outputs. It is a single line of
# JSON on a marked prefix rather than a file or an annotation because the Job's
# log is the one channel that survives the pod being garbage-collected into a
# `kubectl logs job/...` read, and because a human scrolling the same log can
# see it too.
#
# Everything above it is progress, addressed to the PI watching the step.
#
# ---------------------------------------------------------------------------
# IDEMPOTENT BY RECREATION
# ---------------------------------------------------------------------------
# The collection is DELETED IF PRESENT and then created. Upserting into an
# existing collection would be idempotent only if the chunk ids were stable
# across corpus edits — they are not, because a chunk's index shifts when
# earlier text changes — so a re-run would leave orphaned passages from the
# previous corpus that no document supports any more. Those are the worst kind
# of retrieval result: plausible, cited, and gone from the source.
#
# `recreate_collection()` exists in qdrant-client and would be one call, but it
# is DEPRECATED in 1.19.0 ("will be removed in the future"). Delete-then-create
# is the supported spelling of the same thing.
#
# ---------------------------------------------------------------------------
# NO NETWORK. THE EMBEDDING MODEL IS ALREADY ON DISK.
# ---------------------------------------------------------------------------
# `models` is mounted read-only and the embedder is constructed with
# `local_files_only=True`, so a missing cache is an immediate, explicit failure
# naming the seed step — never a silent download onto a read-only mount at the
# moment a PI is watching.
# ===========================================================================

from __future__ import annotations

import json
import os
import sys
import time

from chunker import chunk_corpus

CORPUS_DIR = os.environ.get("CORPUS_DIR", "/mnt/scratch/corpus")
MODELS_DIR = os.environ.get("MODELS_DIR", "/mnt/models")
EMBED_SUBPATH = os.environ.get("EMBED_SUBPATH", "embed").strip()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "").strip()
EMBED_DISTANCE = os.environ.get("EMBED_DISTANCE", "Cosine").strip()
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").strip()
COLLECTION = os.environ.get("COLLECTION", "").strip()

# How many chunks are embedded and upserted at a time. Small enough that the
# 2Gi container limit is never in question, large enough that a demo corpus is
# one or two batches. Also the granularity of the progress lines.
BATCH = 64

# The marker the executor greps for. Defined as a constant so the producer and
# tests agree on the spelling; the consumer is RealExecutor.ingestion.
RESULT_PREFIX = "INGEST_RESULT"


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def main() -> None:
    started = time.monotonic()
    log("=== rag-toolkit ingestion ===")

    if not COLLECTION:
        fail("COLLECTION is empty")
    if not EMBED_MODEL:
        fail("EMBED_MODEL is empty")

    # --- 1. read and chunk, BEFORE touching anything expensive ------------
    # Deliberately first. Chunking is pure, fast and offline, so a corpus
    # problem — an empty folder, a folder of PDFs — is reported in a second
    # rather than after the ONNX session has loaded and the collection has been
    # dropped. Dropping the old collection and THEN discovering there is nothing
    # to put in it would destroy a working index for no reason.
    if not os.path.isdir(CORPUS_DIR):
        fail(
            f"corpus directory {CORPUS_DIR} does not exist. The corpus-upload "
            f"step is what creates and fills it."
        )

    log(f"reading corpus from {CORPUS_DIR}")
    chunks, sources = chunk_corpus(CORPUS_DIR)

    present = sorted(
        name for name in os.listdir(CORPUS_DIR)
        if os.path.isfile(os.path.join(CORPUS_DIR, name)) and not name.startswith(".")
    )
    skipped = [name for name in present if name not in sources]
    if skipped:
        # Named rather than silently ignored. A PI who uploaded ten PDFs and got
        # "0 chunks" deserves to be told it was the file type.
        log(f"skipped {len(skipped)} unsupported file(s): {', '.join(skipped[:10])}")

    if not chunks:
        fail(
            f"no indexable content in {CORPUS_DIR}. Only .md and .txt files are "
            f"read; found {len(present)} file(s), of which {len(sources)} were "
            f"readable."
        )

    log(f"{len(sources)} file(s) -> {len(chunks)} chunk(s)")

    # --- 2. the embedder ---------------------------------------------------
    cache_dir = os.path.normpath(os.path.join(MODELS_DIR, EMBED_SUBPATH))
    log(f"loading embedding model {EMBED_MODEL} from {cache_dir} (offline)")
    try:
        from fastembed import TextEmbedding

        embedder = TextEmbedding(
            model_name=EMBED_MODEL,
            cache_dir=cache_dir,
            # THE POINT OF THE READ-ONLY MOUNT. Without this, a cache miss is a
            # silent download at ingestion time, which would mean the query path
            # depends on the internet and the closed-list guarantee in
            # models.yaml is decorative.
            local_files_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        fail(
            f"could not load the embedding model from {cache_dir}: {exc}\n"
            f"This model is fetched by the model-seed step; if that step was "
            f"skipped or failed there is nothing here to load."
        )

    # THE DIMENSION IS READ FROM THE MODEL, NOT FROM THE ENVIRONMENT. EMBED_DIMS
    # is rendered from models.yaml and is what the catalogue CLAIMS; this is
    # what is true. They are compared rather than one being trusted, because a
    # mismatch means the collection is created with the wrong vector width and
    # the failure surfaces at the first upsert — after the whole corpus has been
    # read and embedded.
    dims = int(embedder.embedding_size)
    declared = os.environ.get("EMBED_DIMS", "").strip()
    if declared and int(declared) != dims:
        fail(
            f"catalogue says {EMBED_MODEL} has {declared} dimensions, the loaded "
            f"model has {dims}. Fix `dims` in templates/catalog/models.yaml — the "
            f"collection would be created with the wrong vector width."
        )
    log(f"embedding model ready: {dims} dimensions, {EMBED_DISTANCE} distance")

    # --- 3. the collection -------------------------------------------------
    try:
        from qdrant_client import QdrantClient, models as qmodels
    except Exception as exc:  # noqa: BLE001
        fail(f"could not import qdrant_client: {exc}")

    log(f"connecting to Qdrant at {QDRANT_URL}")
    client = QdrantClient(url=QDRANT_URL, timeout=60)

    try:
        if client.collection_exists(COLLECTION):
            log(f"collection '{COLLECTION}' exists — deleting it so this run is clean")
            client.delete_collection(COLLECTION)
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=qmodels.VectorParams(
                size=dims,
                # `Distance` is a str-Enum whose members are spelled exactly as
                # models.yaml spells them ("Cosine"/"Dot"/"Euclid"), so this
                # both converts and validates: an unknown value raises here,
                # naming the field, rather than being sent to Qdrant.
                distance=qmodels.Distance(EMBED_DISTANCE),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        fail(f"could not prepare collection '{COLLECTION}': {exc}")
    log(f"collection '{COLLECTION}' created")

    # --- 4. embed and upsert, in batches ----------------------------------
    indexed = 0
    for start in range(0, len(chunks), BATCH):
        batch = chunks[start : start + BATCH]
        texts = [c["text"] for c in batch]
        try:
            vectors = [v.tolist() for v in embedder.embed(texts)]
        except Exception as exc:  # noqa: BLE001
            fail(f"embedding failed at chunk {start}: {exc}")

        points = [
            qmodels.PointStruct(
                # A DENSE INTEGER ID, not a hash of the chunk id. Qdrant accepts
                # unsigned integers or UUIDs and nothing else, so the string
                # "notes.md#3" cannot be the id — it lives in the payload, which
                # is where the RAG service reads it from to build a citation.
                id=start + offset,
                vector=vector,
                payload={
                    "text": chunk["text"],
                    "source": chunk["source"],
                    "chunk_id": chunk["chunk_id"],
                },
            )
            for offset, (chunk, vector) in enumerate(zip(batch, vectors))
        ]

        try:
            # `wait=True` so the count reported at the end is a count of points
            # Qdrant has actually committed, not of requests accepted.
            client.upsert(collection_name=COLLECTION, points=points, wait=True)
        except Exception as exc:  # noqa: BLE001
            fail(f"upsert failed at chunk {start}: {exc}")

        indexed += len(points)
        log(f"indexed {indexed}/{len(chunks)} chunks")

    # --- 5. verify, then report -------------------------------------------
    # Read the count back rather than trusting the loop. This is the same field
    # the project page reads live (`.result.points_count` over REST), so a
    # disagreement between what this Job reports and what the page shows later
    # would be caught here, once, rather than looking like a UI bug.
    try:
        info = client.get_collection(COLLECTION)
        committed = info.points_count
    except Exception as exc:  # noqa: BLE001
        fail(f"indexed {indexed} chunks but could not read the collection back: {exc}")

    if committed != indexed:
        fail(
            f"upserted {indexed} chunks but the collection reports {committed}. "
            f"The index is not what this run intended."
        )

    elapsed = time.monotonic() - started
    log(f"done in {elapsed:.1f}s")

    # THE ONE MACHINE-READABLE LINE. Compact separators and a fixed key order,
    # so it is byte-stable — which matters because the mock executor prints a
    # canned line of this exact shape and both must survive one parser.
    print(
        RESULT_PREFIX
        + " "
        + json.dumps(
            {
                "files": len(sources),
                "chunks": indexed,
                "collection": COLLECTION,
                "dims": dims,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
