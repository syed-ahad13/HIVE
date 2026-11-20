#!/usr/bin/env bash
#
# studio-up.sh — bring the whole of HIVE Studio up: BFF + console.
#
#   Usage:
#     scripts/studio-up.sh                     # mock executor (the default)
#     STUDIO_EXECUTOR=real scripts/studio-up.sh
#
#   Ctrl+C stops both.
#
# ---------------------------------------------------------------------------
# WHAT IT DOES ABOUT AN ALREADY-RUNNING BACKEND
# ---------------------------------------------------------------------------
# It checks port 8000 first and REUSES whatever is there rather than starting a
# second one. During development the backend is usually already up in another
# terminal with --reload, and the failure mode this avoids is nasty: a second
# uvicorn would fail to bind, this script would report success, and the console
# would talk to the older process — with a different STUDIO_EXECUTOR, possibly a
# different catalogue, and no indication anything was wrong.
#
# The trade is that `STUDIO_EXECUTOR=real scripts/studio-up.sh` does NOT change
# the mode of a backend that is already running in mock. The script says which
# mode the live backend is actually in, read from /api/health, rather than
# echoing back what you asked for.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/studio/backend"
FRONTEND_DIR="${REPO_ROOT}/studio/frontend"
VENV="${BACKEND_DIR}/.venv"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"

export STUDIO_EXECUTOR="${STUDIO_EXECUTOR:-mock}"

BACKEND_PID=""
cleanup() {
  if [[ -n "${BACKEND_PID}" ]] && kill -0 "${BACKEND_PID}" 2>/dev/null; then
    echo
    echo "==> stopping the backend we started (pid ${BACKEND_PID})"
    kill "${BACKEND_PID}" 2>/dev/null || true
    wait "${BACKEND_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --- prerequisites ------------------------------------------------------------
[[ -x "${VENV}/bin/uvicorn" ]] || {
  echo "error: no backend venv at ${VENV} (or its dependencies did not install)" >&2
  echo >&2
  # NAME AN INTERPRETER. A bare `python3` on a Mac is often Apple's 3.9 from the
  # Command Line Tools, and the pinned dependencies need >= 3.10 — on 3.9 pip
  # fails with "no matching distribution for fastapi", which reads like a bad pin.
  echo "  # use a Python >= 3.11 explicitly — \`python3\` may be Apple's 3.9" >&2
  echo "  rm -rf studio/backend/.venv" >&2
  echo "  \$(brew --prefix python@3.14)/bin/python3 -m venv studio/backend/.venv" >&2
  echo "  studio/backend/.venv/bin/pip install -r studio/backend/requirements.txt" >&2
  echo "  studio/backend/.venv/bin/python -V   # confirm before continuing" >&2
  exit 1
}
[[ -d "${FRONTEND_DIR}/node_modules" ]] || {
  echo "error: frontend dependencies are not installed" >&2
  echo "  (cd studio/frontend && npm install)" >&2
  exit 1
}

# --- backend ------------------------------------------------------------------
if curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1; then
  echo "==> backend already running on :${API_PORT} — reusing it"
else
  echo "==> starting backend on :${API_PORT}"
  ( cd "${BACKEND_DIR}" && exec "${VENV}/bin/uvicorn" app.main:app --port "${API_PORT}" ) &
  BACKEND_PID=$!

  for _ in $(seq 1 60); do
    if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
      echo "error: the backend exited during startup (a bad catalogue file?)" >&2
      exit 1
    fi
    curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

# Report the mode the BACKEND is actually in, not the one this script was asked
# for — they differ whenever an already-running backend was reused.
LIVE_MODE="$(curl -sf "http://127.0.0.1:${API_PORT}/api/health" \
  | "${VENV}/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("executor","?"))' \
  2>/dev/null || echo "?")"

# ---------------------------------------------------------------------------
# THE MISMATCH, SAID OUT LOUD
# ---------------------------------------------------------------------------
# THE EXECUTOR MODE IS FIXED WHEN THE BACKEND STARTS. It is read once, at
# startup, so that it cannot change under a pipeline that is already running —
# and that means an already-running backend CANNOT be switched by re-running
# this script with a different environment variable.
#
# Reporting the live mode in the banner was not enough. Someone who typed
# `STUDIO_EXECUTOR=real` and got a `mock` banner has to notice the difference
# between what they asked for and what they got, in a box they have already seen
# a dozen times — and the cost of not noticing is a demo that provisions nothing
# while everybody watches, or worse, believing a mock run was real.
#
# So when they differ, say so, and say exactly what to do about it.
if [[ "${LIVE_MODE}" != "?" && "${LIVE_MODE}" != "${STUDIO_EXECUTOR}" ]]; then
  echo
  echo "  !! YOU ASKED FOR '${STUDIO_EXECUTOR}' AND THE RUNNING BACKEND IS '${LIVE_MODE}'."
  echo
  echo "     A backend that is already up cannot be switched — the mode is read"
  echo "     once, at startup, so it cannot change under a running pipeline."
  echo "     This script reused the backend on :${API_PORT} rather than starting a"
  echo "     second one that would have failed to bind."
  echo
  echo "     To actually switch, stop it first:"
  echo
  echo "         scripts/studio-down.sh"
  if [[ "${STUDIO_EXECUTOR}" == "real" ]]; then
    echo "         STUDIO_EXECUTOR=real scripts/studio-up.sh"
  else
    echo "         scripts/studio-up.sh"
  fi
  echo
fi

CONSOLE_URL="http://localhost:${UI_PORT}"
API_URL="http://localhost:${API_PORT}/docs"
MODE_TEXT="${LIVE_MODE}"
# if/fi, NOT `[[ ... ]] && ...`. Under `set -e` a false one-liner test returns 1
# and takes the whole script down with it — so the `real` path would have exited
# here silently, which is the one path where exiting is least acceptable.
if [[ "${LIVE_MODE}" == "mock" ]]; then
  MODE_TEXT="mock  (nothing is really created)"
fi
if [[ "${LIVE_MODE}" == "real" ]]; then
  # Symmetry matters here. A banner that qualifies only one of the two modes
  # leaves the other looking like the unremarkable default, and `real` is the
  # one that touches the cluster.
  MODE_TEXT="real  (provisions on the host cluster)"
fi

echo
echo "  ┌──────────────────────────────────────────────────────────────┐"
printf "  │  console   %-50s│\n" "${CONSOLE_URL}"
printf "  │  api       %-50s│\n" "${API_URL}"
printf "  │  executor  %-50s│\n" "${MODE_TEXT}"
echo "  └──────────────────────────────────────────────────────────────┘"
echo

# The dev server runs in the foreground so Ctrl+C reaches it directly and the
# trap above takes the backend down with it.
cd "${FRONTEND_DIR}"
exec npm run dev -- --port "${UI_PORT}"
