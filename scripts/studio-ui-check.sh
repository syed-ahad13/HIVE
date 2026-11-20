#!/usr/bin/env bash
#
# studio-ui-check.sh — mount the console's stateful components and assert what
#                      they render.
#
#   scripts/studio-ui-check.sh
#
# ===========================================================================
# WHY THIS EXISTS
# ===========================================================================
# `npm run build` proves the console COMPILES. It says nothing about whether the
# progress list renders eleven steps, whether a successful step's log can be
# opened, or whether the sources panel appears when retrieval found nothing —
# and those three are exactly the claims the RAG work makes about the UI.
#
# So this compiles studio/frontend/tests/ui-check.jsx with the project's own
# Vite config (same JSX transform, same aliases, no second toolchain to keep in
# step) and runs it in Node against a jsdom document. The components are the real
# ones, imported from src/; nothing is reimplemented for the test.
#
# ===========================================================================
# jsdom IS A DEV-ONLY, UNSAVED DEPENDENCY
# ===========================================================================
# Installed with `npm install --no-save` so it never enters package.json. The
# console itself must keep its three runtime dependencies — react, react-dom and
# the router — and adding a test-only DOM to the manifest would put a 30MB
# implementation of the browser into the dependency list of a page that runs IN a
# browser. If it is missing, this script installs it and says so.
# ===========================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="${REPO_ROOT}/studio/frontend"
OUT="${FRONTEND}/.ui-check"

cd "${FRONTEND}"

if [[ ! -d node_modules ]]; then
  echo "error: no node_modules — run 'npm install' in studio/frontend first" >&2
  exit 1
fi

if [[ ! -d node_modules/jsdom ]]; then
  echo "==> installing jsdom (test only, not saved to package.json)"
  npm install --no-save --silent jsdom@30.0.1
fi

echo "==> compiling the checks with the project's own Vite config"
# --ssr: a Node-targeted build, so react and react-dom stay external and the
# bundle is one file we can import. The output is gitignored scratch.
npx vite build \
  --ssr tests/ui-check.jsx \
  --outDir "${OUT}" \
  --emptyOutDir \
  --logLevel warn

echo "==> mounting the components"
echo
node "${REPO_ROOT}/studio/frontend/tests/run-ui-check.mjs"
