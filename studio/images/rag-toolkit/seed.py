# ===========================================================================
# RAG TOOLKIT — THE SEED ENTRYPOINT
# ===========================================================================
# Fetches BOTH models a RAG project needs into the project's `models` PVC, and
# is the ONLY thing in the whole project that is allowed to reach the network.
#
#   $MODELS_DIR/$CHAT_SUBPATH/<name>.gguf   the chat model, by HTTPS GET
#   $MODELS_DIR/$EMBED_SUBPATH/models--...  the embedding model's ONNX cache
#
# Run by templates/catalog/rag-stack/resources/model-seed.yaml.tmpl as the
# pipeline's `model-seed` step.
#
# ---------------------------------------------------------------------------
# WHY THE EGRESS IS STILL CLOSED
# ---------------------------------------------------------------------------
# Both URLs come from templates/catalog/models.yaml, which is a closed list a PI
# cannot add to: `CHAT_SEED_URL` is a `seedUrl` verbatim, and `EMBED_MODEL` is a
# `fastembedName` that fastembed resolves to a Hugging Face repo it owns the
# mapping for. Nothing here constructs a URL, and nothing here takes one from a
# request.
#
# Everything downstream — the ingestion Job, the RAG service — mounts `models`
# READ-ONLY and loads with `local_files_only=True`. So if this program does not
# put the bytes here, they fail at startup with a clear message rather than
# quietly downloading at query time.
#
# ---------------------------------------------------------------------------
# IDEMPOTENT, BECAUSE IT WILL BE RE-RUN
# ---------------------------------------------------------------------------
# A retried deployment, a re-applied fragment and a backoffLimit retry all land
# here again. Both halves skip what is already complete, and the chat download
# writes to a `.part` file and renames into place so an interrupted run can
# never leave a truncated file that the skip check would mistake for a good one.
# ===========================================================================

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

MODELS_DIR = os.environ.get("MODELS_DIR", "/mnt/models")
CHAT_SEED_URL = os.environ.get("CHAT_SEED_URL", "").strip()
CHAT_SUBPATH = os.environ.get("CHAT_SUBPATH", "").strip()
EMBED_SUBPATH = os.environ.get("EMBED_SUBPATH", "embed").strip()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "").strip()

# Generous: a first pull of a few hundred megabytes on a domestic line. The Job
# itself has a 900s ceiling in the executor, which is the real limit.
HTTP_TIMEOUT = 600


def log(message: str) -> None:
    """stdout, unbuffered.

    The pipeline reads this Job's log to explain a failure to a PI, and a
    buffered stream means a Job that is killed shows nothing at all — the case
    where the log matters most.
    """
    print(message, flush=True)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# THE CHAT MODEL
# ---------------------------------------------------------------------------


def seed_chat() -> None:
    if not CHAT_SEED_URL:
        fail("CHAT_SEED_URL is empty; the catalogue entry has no seedUrl")

    # normpath collapses "/mnt/models/" + "" into "/mnt/models" rather than
    # leaving "/mnt/models//". The same join produces the storageUri the
    # InferenceService mounts, where a trailing slash is NOT the proven-working
    # form (see templates/catalog/models.yaml), so the two are kept consistent.
    dest_dir = os.path.normpath(os.path.join(MODELS_DIR, CHAT_SUBPATH))

    # The filename comes from the URL, so the artefact on disk keeps the name
    # the runtime expects. hive-llama-cpp-rag globs /mnt/models for exactly one
    # *.gguf, so the extension has to survive.
    filename = os.path.basename(urllib.parse.urlparse(CHAT_SEED_URL).path)
    if not filename:
        fail(f"could not derive a filename from {CHAT_SEED_URL}")
    destination = os.path.join(dest_dir, filename)

    os.makedirs(dest_dir, exist_ok=True)

    if os.path.exists(destination) and os.path.getsize(destination) > 0:
        log(f"chat model already seeded: {destination} ({os.path.getsize(destination)} bytes)")
        return

    log(f"downloading {CHAT_SEED_URL}")
    log(f"       into {destination}")

    partial = destination + ".part"
    with urllib.request.urlopen(CHAT_SEED_URL, timeout=HTTP_TIMEOUT) as response:
        if response.status != 200:
            fail(f"unexpected HTTP status {response.status} for {CHAT_SEED_URL}")
        with open(partial, "wb") as out:
            # Streamed in 1MB chunks rather than read() in one go: model weights
            # are routinely larger than the container's memory limit.
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                out.write(block)

    size = os.path.getsize(partial)
    if size == 0:
        os.remove(partial)
        fail("downloaded 0 bytes")

    # Rename only after a complete download, so the skip check above can trust
    # the presence of the file.
    os.rename(partial, destination)
    log(f"seeded {destination} ({size} bytes)")


# ---------------------------------------------------------------------------
# THE EMBEDDING MODEL
# ---------------------------------------------------------------------------


def seed_embeddings() -> None:
    """Warm fastembed's ONNX cache under $MODELS_DIR/$EMBED_SUBPATH.

    THE CACHE DIRECTORY IS A CONSTRUCTOR ARGUMENT, NOT AN ENVIRONMENT VARIABLE,
    and that is worth stating because fastembed supports both and they behave
    differently. `FASTEMBED_CACHE_PATH` is consulted only when `cache_dir` is
    None (fastembed 0.8.0, `common/utils.py:define_cache_dir`); passing
    `cache_dir=` explicitly is unconditional, so it cannot be defeated by an
    environment this container did not set.

    Constructing TextEmbedding is what triggers the download — there is no
    separate `download()` API — so this both fetches and PROVES the model loads.
    A cache that downloaded but cannot be opened would otherwise be discovered
    by the ingestion Job, one step later, with the corpus already read.
    """
    if not EMBED_MODEL:
        fail("EMBED_MODEL is empty; the catalogue entry has no fastembedName")

    cache_dir = os.path.normpath(os.path.join(MODELS_DIR, EMBED_SUBPATH))
    os.makedirs(cache_dir, exist_ok=True)

    log(f"fetching embedding model {EMBED_MODEL} into {cache_dir}")

    # Imported HERE, not at module scope. The import costs onnxruntime — several
    # hundred milliseconds and a few hundred megabytes of RSS — and the chat
    # download above should not pay for it, nor should a failure in the import
    # be reported as a failure of a download that already succeeded.
    try:
        from fastembed import TextEmbedding
    except Exception as exc:  # noqa: BLE001
        fail(f"could not import fastembed: {exc}")

    try:
        embedder = TextEmbedding(model_name=EMBED_MODEL, cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001
        fail(
            f"could not load embedding model {EMBED_MODEL!r}: {exc}\n"
            f"If this is a 'not supported' error the catalogue's fastembedName is "
            f"wrong: it must be an id from fastembed's own supported-models list."
        )

    # One real embedding, so the exit status means "this model works" rather
    # than "some files were written". Cheap: a single short string on CPU.
    try:
        vector = next(iter(embedder.embed(["hive local rag seed check"])))
    except Exception as exc:  # noqa: BLE001
        fail(f"embedding model {EMBED_MODEL!r} loaded but could not embed: {exc}")

    log(f"embedding model ready: {EMBED_MODEL}, {len(vector)} dimensions")
    log(f"cache contents: {sorted(os.listdir(cache_dir))}")


def main() -> None:
    log("=== rag-toolkit seed ===")
    log(f"models dir : {MODELS_DIR}")
    seed_chat()
    seed_embeddings()
    log("=== seed complete: both models are on the models PVC ===")


if __name__ == "__main__":
    main()
