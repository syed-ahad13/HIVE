# ===========================================================================
# HIVE STUDIO — THE BATCH DRIVER IMAGE
# ===========================================================================
# The main container of a batch Job. It streams a dataset through the curated
# serving runtime that runs beside it as a native sidecar, and writes the
# results onto the project's scratch volume.
#
# ---------------------------------------------------------------------------
# ONE DEPENDENCY, ON PURPOSE
# ---------------------------------------------------------------------------
# httpx, and nothing else. No pandas, no pyarrow, no datasets library.
#
# The driver reads CSV and JSONL a line at a time with the standard library,
# because a 200MB upload must not be loaded into a container sized for streaming
# work — and because every dependency here is a thing that must publish
# linux/arm64, be pinned, be recorded in VERSIONS.md, and be re-reviewed at
# migration. `csv` and `json` cost none of that.
#
# python:3.11-slim publishes linux/arm64, so this runs natively on Apple Silicon
# (CLAUDE.md §5) with no Rosetta row in DEVIATIONS.md.
# ===========================================================================
FROM python:3.11-slim

# Pinned, like every other dependency in this repository. Same version the BFF
# uses, so there is one httpx to reason about across the whole of Studio.
RUN pip install --no-cache-dir httpx==0.28.1

WORKDIR /app
COPY driver.py /app/driver.py

# NO ENTRYPOINT, deliberately — the same convention the RAG toolkit image uses.
# The manifest sets `command: ["python", "/app/driver.py"]`, so what runs is
# visible in the rendered Job under tenants/<project>/rendered/ rather than
# buried in an image somebody has to inspect.
