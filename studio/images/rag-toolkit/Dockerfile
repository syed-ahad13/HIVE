# ===========================================================================
# HIVE RAG TOOLKIT — ONE IMAGE, THREE ENTRYPOINTS
# ===========================================================================
# Built locally by scripts/build-rag-toolkit.sh and loaded into the kind nodes
# with `kind load docker-image`. IT EXISTS IN NO REGISTRY, which is why every
# manifest that uses it sets `imagePullPolicy: Never` — without that the kubelet
# tries Docker Hub and the pod sits in ImagePullBackOff, a failure that looks
# like a network problem and is not one.
#
# ---------------------------------------------------------------------------
# WHY ONE IMAGE AND NOT THREE
# ---------------------------------------------------------------------------
#   python /app/seed.py     the model-seed Job   — fetch both models
#   python /app/ingest.py   the ingestion Job    — chunk, embed, upsert
#   uvicorn api:app         the RAG service      — retrieve and answer
#
# THE INGESTER AND THE SERVICE MUST AGREE ABOUT CHUNKING AND EMBEDDING, exactly,
# or the question and the documents end up in different vector spaces and every
# search returns confident nonsense with no error anywhere. Shipping them as one
# image makes that agreement a property of the build rather than of two version
# numbers somebody has to keep in step. `chunker.py` is imported by the ingester
# and its rules are what the service's citations refer to.
#
# The seed entrypoint joins them because it must construct the SAME fastembed
# model, with the same id, into the cache the other two read.
#
# The cost is a service image carrying an ingester it never runs. That is a few
# kilobytes of Python on top of an onnxruntime both genuinely need.
#
# NO ENTRYPOINT, DELIBERATELY. Each manifest sets its own `command`, so what a
# container does is visible in the manifest a reviewer reads rather than baked
# into a layer they would have to inspect the image to discover.
#
# ---------------------------------------------------------------------------
# linux/arm64, VERIFIED PER DEPENDENCY (2026-08-31) — CLAUDE.md §5
# ---------------------------------------------------------------------------
# This is macOS on Apple Silicon, so every wheel must exist for
# manylinux aarch64 or pip falls back to a source build that a `-slim` image has
# no compiler for. Checked against the PyPI JSON API rather than assumed, and
# `onnxruntime` — the one with real native code and the usual culprit — is
# listed first because it is the one worth re-checking on any upgrade:
#
#   onnxruntime 1.29.0        cp311-cp311-manylinux_2_28_aarch64      OK
#   tokenizers 0.23.1         cp310-abi3-manylinux_2_17_aarch64       OK
#   py-rust-stemmers 0.1.8    cp311-cp311-manylinux_2_17_aarch64      OK
#   mmh3 5.3.0                cp311-cp311-manylinux_2_28_aarch64      OK
#   pillow 12.3.0             cp311-cp311-manylinux_2_28_aarch64      OK
#   numpy 2.4.6               cp311-cp311-manylinux_2_28_aarch64      OK
#   grpcio 1.83.1             cp311-cp311-manylinux_2_17_aarch64      OK
#   protobuf 7.36.0           cp310-abi3-manylinux2014_aarch64        OK
#   fastembed / qdrant-client / portalocker    pure Python            OK
#
# NOTHING RUNS UNDER ROSETTA, so there is no DEVIATIONS.md row for this image.
# The sentence-transformers fallback the plan allowed for was not needed.
#
# python:3.11-slim is Debian-based with glibc well past 2.28, so the
# manylinux_2_28 wheels above install cleanly. 3.11 rather than 3.12+ because
# fastembed's numpy pin for 3.11 resolves to 2.4.x, which is where the aarch64
# wheels are — 2.5 dropped 3.11 entirely.
# ===========================================================================
FROM python:3.11-slim

# /app is on sys.path for `uvicorn api:app` and is where the three entrypoints
# live. Set before the install so the layer cache is not invalidated by it.
WORKDIR /app

# ---------------------------------------------------------------------------
# PINNED TO EXACT VERSIONS (==), not ranges — the same rule as
# studio/backend/requirements.txt and for the same reason: this repository is a
# rehearsal that gets migrated, and "it worked on the laptop" is only useful if
# the thing that worked can be reconstructed. Recorded in VERSIONS.md.
#
# fastapi/uvicorn/httpx are pinned to the SAME versions the BFF uses, so there
# is one set of numbers to reason about across the two Python components.
#
# --no-cache-dir keeps the wheel cache out of the layer; nothing in this image
# ever installs anything again.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir \
      fastapi==0.141.1 \
      "uvicorn[standard]==0.52.4" \
      httpx==0.28.1 \
      qdrant-client==1.19.0 \
      fastembed==0.8.0

# Copied last, so editing a script rebuilds one small layer rather than
# reinstalling ~200MB of onnxruntime.
#
#   chunker.py  pure, deterministic, stdlib-only — imported by ingest.py and
#               unit-tested outside this image by tests/test-chunker.py
#   seed.py     model-seed Job entrypoint
#   ingest.py   ingestion Job entrypoint
#   api.py      the service (`uvicorn api:app`)
COPY chunker.py seed.py ingest.py api.py /app/

# Unbuffered stdout. Every one of these entrypoints is read through
# `kubectl logs` by a human watching a step, and a buffered stream means a
# container that is killed shows nothing at all — the case where the log matters
# most.
ENV PYTHONUNBUFFERED=1

# No ENTRYPOINT and no CMD. See the header: the manifest says what runs.
