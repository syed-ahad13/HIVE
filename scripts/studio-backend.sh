#!/usr/bin/env bash
#
# studio-backend.sh — run the HIVE Studio BFF.
#
#   Usage:
#     scripts/studio-backend.sh                    # mock executor (the default)
#     STUDIO_EXECUTOR=real scripts/studio-backend.sh
#     STUDIO_FAIL_STEP=model-seed scripts/studio-backend.sh
#     PORT=8001 scripts/studio-backend.sh
#
# ---------------------------------------------------------------------------
# WHY A SCRIPT AND NOT "just run uvicorn"
# ---------------------------------------------------------------------------
# Three things have to be right at once and none of them are obvious:
#
#   1. THE INTERPRETER. This machine has three python3s on PATH — Homebrew's
#      (which wins), Apple's, and conda's — and the dependencies are in none of
#      them. They are in studio/backend/.venv, which this script addresses by its
#      explicit path so nothing depends on the caller having activated anything.
#   2. THE WORKING DIRECTORY. `app.main:app` only resolves from studio/backend/,
#      and --reload re-imports from the same place.
#   3. THE PORT. 8000 is fixed by CLAUDE.md §6 because the Vite dev server proxies
#      /api there. Changing it means changing the frontend proxy too.
#
# This is the scripts/ carve-out from the CLAUDE.md §5 working agreements: the
# imperative command lives in a committed file rather than in someone's shell
# history.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/studio/backend"
VENV="${BACKEND_DIR}/.venv"
PORT="${PORT:-8000}"

# The default is `mock` and it is set here as well as in the app, so that what the
# script prints matches what the app will do (CLAUDE.md §6: the safe mode is the
# one you get by forgetting to set anything).
export STUDIO_EXECUTOR="${STUDIO_EXECUTOR:-mock}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  cat >&2 <<EOF
error: no virtualenv at ${VENV}

  Create it and install the pinned dependencies:

    python3 -m venv studio/backend/.venv
    studio/backend/.venv/bin/pip install -r studio/backend/requirements.txt
EOF
  exit 1
fi

if ! "${VENV}/bin/python" -c "import uvicorn" 2>/dev/null; then
  echo "error: dependencies missing from ${VENV}" >&2
  echo "  run: ${VENV}/bin/pip install -r studio/backend/requirements.txt" >&2
  exit 1
fi

echo "==> HIVE Studio BFF"
echo "    executor : ${STUDIO_EXECUTOR}$([[ "${STUDIO_EXECUTOR}" == "mock" ]] && echo '  (nothing is created; set STUDIO_EXECUTOR=real to provision)')"
[[ -n "${STUDIO_FAIL_STEP:-}" ]] && echo "    fail-step: ${STUDIO_FAIL_STEP}  (failure injection is ON)"
echo "    url      : http://127.0.0.1:${PORT}/api/health"
echo "    docs     : http://127.0.0.1:${PORT}/docs"
echo

cd "${BACKEND_DIR}"

# TWO reload flags beyond --reload, and both are needed to watch the catalogue:
#
#   --reload-dir      uvicorn otherwise watches only the directory it started in,
#                     so templates/catalog/ would be invisible.
#   --reload-include  uvicorn's watcher matches *.py ONLY. Adding the directory
#                     without this changes nothing at all — verified: touching
#                     models.yaml with just --reload-dir produced no restart.
#
# Why bother watching data at all: the catalogue is loaded ONCE at startup, so
# without this you must restart by hand after every template edit — and template
# edits are the main thing anyone does while working on Studio.
#
# WHAT THIS DOES NOT DO — it is not crash recovery. If a template is malformed the
# app exits during startup, and uvicorn's reloader does NOT respawn a child that
# has already exited; it sits watching with nothing running. Fixing the YAML at
# that point does nothing and you must Ctrl+C and start again. Watching the
# catalogue helps while the server is HEALTHY, which is the common case.
exec "${VENV}/bin/uvicorn" app.main:app \
  --port "${PORT}" \
  --reload \
  --reload-dir "${BACKEND_DIR}/app" \
  --reload-dir "${REPO_ROOT}/templates/catalog" \
  --reload-include "*.yaml"
