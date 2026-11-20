#!/usr/bin/env bash
#
# studio-smoke.sh — verify the Studio BFF's read surface.
#
#   Usage:
#     scripts/studio-smoke.sh            # starts its own server, tears it down
#     scripts/studio-smoke.sh --running  # test a server already on :8000
#
# ---------------------------------------------------------------------------
# WHAT THIS PROVES
# ---------------------------------------------------------------------------
# Four endpoints answer 200 and carry the ids they are supposed to carry. That is
# a deliberately small claim, and it is the right one for S1: the BFF has no
# provisioning surface yet, so what can be tested is that the catalogue LOADS,
# that it loads COMPLETELY, and that the wizard's enum resolution works.
#
# The load-bearing assertion is the resolved `model` enum. In the file, that
# parameter is `source: models.yaml` with no values at all; if resolution silently
# failed, the endpoint would still return 200 and the wizard would render a model
# dropdown with nothing in it. So the test asserts the resolved values are present
# AND that a known model id is among them, rather than just checking the status
# code.
#
# By starting its own server on a scratch database, a run leaves nothing behind:
# no studio.db in the repo, no process on 8000.
#
# ---------------------------------------------------------------------------
# WHAT A FULL RUN COVERS, AND ROUGHLY HOW LONG IT TAKES (~3 minutes)
# ---------------------------------------------------------------------------
#   the read surface        health, catalogue, wizard schema, model list
#   offline unit tests      tests/test-render.py, tests/test-chunker.py
#   inference-endpoint      dry run, six-step pipeline, teardown        (~35s)
#   rag-stack               dry run, eleven-step pipeline, teardown     (~65s)
#   batch-inference         the seven-step plan and its own questions
#   failure injection       a failed step, then a retry that resumes    (~30s)
#
# Every deployment is MOCK. Nothing here touches a cluster, so it runs with
# Docker stopped — which is the condition most of this repository's frontend and
# schema work actually happens under.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/studio/backend"
VENV="${BACKEND_DIR}/.venv"
PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"

USE_RUNNING=0
[[ "${1:-}" == "--running" ]] && USE_RUNNING=1

PASS=0
FAIL=0
SERVER_PID=""
TMPDIR_SMOKE="$(mktemp -d)"

cleanup() {
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMPDIR_SMOKE}"
}
trap cleanup EXIT

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); }

# assert_json <label> <path> <jq-ish python expression over the parsed body>
#
# Uses the venv's python rather than jq: jq is not a documented prerequisite of
# this repo and the venv is guaranteed to exist by the time this script is useful.
check() {
  local label="$1" path="$2" expr="$3"
  local body status
  body="$(curl -sS -o "${TMPDIR_SMOKE}/body" -w '%{http_code}' "${BASE}${path}" 2>/dev/null)" || {
    bad "${label} — request failed"
    return
  }
  status="${body}"
  if [[ "${status}" != "200" ]]; then
    bad "${label} — HTTP ${status} (expected 200)"
    sed -n '1,3p' "${TMPDIR_SMOKE}/body" | sed 's/^/        /'
    return
  fi
  if "${VENV}/bin/python" -c "
import json, sys
d = json.load(open('${TMPDIR_SMOKE}/body'))
sys.exit(0 if (${expr}) else 1)
" 2>/dev/null; then
    ok "${label}"
  else
    bad "${label} — 200, but the body was not as expected"
    sed -n '1,6p' "${TMPDIR_SMOKE}/body" | sed 's/^/        /'
  fi
}

# ---------------------------------------------------------------------------
# start a server, unless one was handed to us
# ---------------------------------------------------------------------------
if [[ "${USE_RUNNING}" -eq 0 ]]; then
  if [[ ! -x "${VENV}/bin/uvicorn" ]]; then
    echo "error: no venv at ${VENV} — see scripts/studio-backend.sh" >&2
    exit 1
  fi

  # A scratch registry AND a scratch upload directory, so a smoke run never
  # writes to the real studio.db, never stages documents into tenants/_uploads/,
  # and leaves nothing behind in the repo.
  export STUDIO_DB="${TMPDIR_SMOKE}/smoke.db"
  export STUDIO_UPLOADS="${TMPDIR_SMOKE}/uploads"
  # Explicit, even though it is the default: this test must never provision.
  export STUDIO_EXECUTOR=mock

  echo "==> starting BFF on :${PORT} (scratch db, executor=mock)"
  ( cd "${BACKEND_DIR}" && exec "${VENV}/bin/uvicorn" app.main:app --port "${PORT}" ) \
    >"${TMPDIR_SMOKE}/server.log" 2>&1 &
  SERVER_PID=$!

  # Wait for readiness rather than sleeping a fixed amount. Also give up if the
  # process dies — a catalogue error exits immediately, and polling a dead server
  # for 30s would report a timeout instead of the actual reason.
  for _ in $(seq 1 60); do
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
      echo "error: the server exited during startup:" >&2
      sed 's/^/    /' "${TMPDIR_SMOKE}/server.log" >&2
      exit 1
    fi
    curl -sf "${BASE}/api/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

echo "==> GET ${BASE}/api/*"
echo

# --- 1. health ---------------------------------------------------------------
# executor must be `mock` unless deliberately overridden — the safe default is a
# property worth asserting, not just documenting.
check "/api/health           ok=true, executor present" \
      "/api/health" \
      "d['ok'] is True and d['executor'] in ('mock', 'real')"

# --- 2. the catalogue --------------------------------------------------------
# Every committed template, and available-first ordering.
#
# ASSERTED AGAINST THE DIRECTORY, NOT AGAINST A LIST WRITTEN HERE. This used to
# name the three templates that existed when it was written, so it went red the
# moment one was promoted, added or dropped — and a red smoke script that is red
# for a reason nobody is fixing stops being read at all. The catalogue is data;
# what is worth asserting is that the endpoint serves ALL of it, in the
# documented order, not which files happen to be committed today.
check "/api/templates        every committed template is served" \
      "/api/templates" \
      "{t['id'] for t in d} == {p.name for p in __import__('pathlib').Path('templates/catalog').iterdir() if p.is_dir()}"

check "/api/templates        available ones first, then alphabetical" \
      "/api/templates" \
      "[t['id'] for t in d] == sorted((t['id'] for t in d), key=lambda i: (next(x['maturity'] for x in d if x['id']==i) != 'available', i))"

# The three that provision. A `coming-soon` template must never be one of them:
# the console refuses to start a deployment from one (CLAUDE.md §6), and the
# catalogue loader only enforces its referential checks on `available` ones.
check "/api/templates        the three provisioning templates are available" \
      "/api/templates" \
      "sorted(t['id'] for t in d if t['maturity'] == 'available') == ['batch-inference','inference-endpoint','rag-stack']"

check "/api/templates        cards carry id/name/category/maturity/description" \
      "/api/templates" \
      "all({'id','name','category','maturity','description'} <= set(t) for t in d)"

# --- 3. the wizard schema ----------------------------------------------------
check "/api/templates/{id}   inference-endpoint, 5 parameters in order" \
      "/api/templates/inference-endpoint" \
      "[p['name'] for p in d['spec']['parameters']] == ['projectName','model','availability','tier','durationDays']"

# THE LOAD-BEARING ONE. In the file this enum has no values, only
# `source: models.yaml`. If resolution broke, the endpoint would still be 200 and
# the wizard's model dropdown would simply be empty.
check "/api/templates/{id}   model enum RESOLVED from models.yaml" \
      "/api/templates/inference-endpoint" \
      "any(p['name']=='model' and p.get('values') and 'sklearn-iris' in [v['value'] for v in p['values']] and all(v.get('label') for v in p['values']) for p in d['spec']['parameters'])"

check "/api/templates/{id}   projectName keeps its reserved lists" \
      "/api/templates/inference-endpoint" \
      "any(p['name']=='projectName' and 'inf-101' in p.get('reservedNames',[]) and 'kube-' in p.get('reservedPrefixes',[]) for p in d['spec']['parameters'])"

check "/api/templates/{id}   resources + teardownOrder present" \
      "/api/templates/inference-endpoint" \
      "d['spec']['resources'] == ['namespace-pack','model-seed-job','vcluster','inference-service'] and d['spec']['lifecycle']['teardownOrder'] == ['inference-service','vcluster','namespace-pack']"

# 404 needs its own shape of check, since `check` asserts 200.
if [[ "$(curl -sS -o /dev/null -w '%{http_code}' "${BASE}/api/templates/no-such-template")" == "404" ]]; then
  ok "/api/templates/{id}   unknown id is 404"
else
  bad "/api/templates/{id}   unknown id should be 404"
fi

# --- 4. the model list -------------------------------------------------------
check "/api/models           all three catalogue models, full field contract" \
      "/api/models" \
      "{m['id'] for m in d} == {'sklearn-iris','qwen2.5-0.5b-instruct-q4','bge-small-en-v1.5'} and all({'id','displayName','interface','runtime','modelFormat','storageSubpath','seedUrl','invokePath','sampleInput'} <= set(m) for m in d)"

# The embedding entry is a DIFFERENT SHAPE and the loader enforces the split per
# interface: an `embed` entry has no runtime, no modelFormat and no seedUrl —
# it is loaded in-process by the RAG toolkit, not served — and must declare the
# four fields that say how.
check "/api/models           the embed entry has its own field contract" \
      "/api/models" \
      "any(m['id']=='bge-small-en-v1.5' and m['interface']=='embed' and m['provider']=='fastembed' and m['fastembedName']=='BAAI/bge-small-en-v1.5' and m['dims']==384 and m['distance']=='Cosine' and m['seedUrl']=='' and m['runtime']=='' for m in d)"

# runtime: "" on sklearn-iris is a MEANINGFUL value, not a missing one — it lets
# KServe resolve the runtime from modelFormat. A serializer that dropped empty
# strings would change what gets deployed, so it is asserted explicitly.
check "/api/models           sklearn-iris keeps runtime:'' (not dropped)" \
      "/api/models" \
      "any(m['id']=='sklearn-iris' and m['runtime']=='' and m['storageSubpath']=='' for m in d)"


# --- the wizard's own machinery ----------------------------------------------
# The console renders questions from the schema and steps from the plan, so
# these four fields are the contract between the catalogue and the UI. Each was
# something the frontend used to hard-code.

check "/api/templates/{id}   carries the step plan the backend will run" \
      "/api/templates/inference-endpoint" \
      "d['steps'] == ['approve','namespace-pack','model-seed','vcluster','inference-service','ready-check']"

check "/api/templates/{id}   every parameter declares which screen it is on" \
      "/api/templates/inference-endpoint" \
      "all(p.get('group') in ('basics','resources') for p in d['spec']['parameters']) and [p['name'] for p in d['spec']['parameters'] if p['group']=='basics'] == ['projectName','durationDays']"

# The archetype trade-off used to be a ternary in the wizard keyed on the option
# value. It is catalogue copy, so it lives in the catalogue.
check "/api/templates/{id}   availability options describe their consequences" \
      "/api/templates/inference-endpoint" \
      "any(p['name']=='availability' and all(v.get('description') for v in p['values']) for p in d['spec']['parameters'])"

check "/api/templates/rag-stack  declares interface 'rag' and an eleven-step plan" \
      "/api/templates/rag-stack" \
      "d['spec']['interface'] == 'rag' and len(d['steps']) == 11 and d['steps'][8] == 'ready-check' and d['steps'][9] == 'rag-api'"

check "/api/templates/rag-stack  corpusUploadId is an 'upload', not a text box" \
      "/api/templates/rag-stack" \
      "any(p['name']=='corpusUploadId' and p['type']=='upload' and p['group']=='basics' and p.get('required') is not True and p.get('pattern') for p in d['spec']['parameters'])"

# ---------------------------------------------------------------------------
# batch-inference — THE QUESTION WHOSE ANSWERS ARE NOT IN THE CATALOGUE
# ---------------------------------------------------------------------------
# `inputFields` is the one parameter in the whole console the catalogue cannot
# populate: its options are the columns of a file that did not exist when the
# template was written. So the endpoint MUST leave it empty — anything else
# would be the catalogue inventing columns — and the console fills it from the
# dataset, either the PI's upload or the bundled sample served below.
check "/api/templates/batch-inference  a seven-step plan, ending in collect-results" \
      "/api/templates/batch-inference" \
      "len(d['steps']) == 7 and d['steps'][-3:] == ['dataset-upload','batch-run','collect-results']"

check "/api/templates/batch-inference  inputFields is an unpopulated multiselect" \
      "/api/templates/batch-inference" \
      "any(p['name']=='inputFields' and p['type']=='multiselect' and not p.get('values') and not p.get('source') and p.get('required') is True for p in d['spec']['parameters'])"

# THE FALLBACK HAS TO BE INSPECTABLE OR IT IS NOT TAKEABLE. The dataset upload
# is optional and the help text promises a sample stands in — but the columns
# question is required, so without the sample's columns the advertised path
# through the wizard could not be walked. The preview is produced by the same
# function an upload goes through, keyed by the model interface that selects it.
check "/api/templates/batch-inference  ships a previewed sample per interface" \
      "/api/templates/batch-inference" \
      "set(d['sampleDatasets']) == {'chat','predict-v1'} and all(s['preview']['columns'] for s in d['sampleDatasets'].values())"

check "/api/templates/batch-inference  the classifier sample is iris, four columns" \
      "/api/templates/batch-inference" \
      "d['sampleDatasets']['predict-v1']['file'] == 'iris-5000.csv' and d['sampleDatasets']['predict-v1']['preview']['columns'] == ['sepal_length','sepal_width','petal_length','petal_width']"

# A template with no samples must carry NO key, not an empty object — which
# would read as "this one has samples and they are broken".
check "/api/templates/inference-endpoint  says nothing about samples" \
      "/api/templates/inference-endpoint" \
      "'sampleDatasets' not in d"

# ===========================================================================
# CORPUS UPLOAD
# ===========================================================================
# The wizard stages documents BEFORE submitting a deployment and sends the id it
# gets back. Everything below is that endpoint, including the rejections — which
# matter more than the happy path, because this is the one surface that takes a
# file from outside and turns its name into a path.

echo
echo "==> POST /api/uploads"
echo

UPDIR="${TMPDIR_SMOKE}/corpus"
mkdir -p "${UPDIR}"
printf '# Notes\n\nOne KServe, on the host.\n' > "${UPDIR}/notes.md"
printf 'plain text\n'                          > "${UPDIR}/extra.txt"
printf 'not indexable\n'                       > "${UPDIR}/paper.pdf"
: > "${UPDIR}/empty.md"

UPID="$("${VENV}/bin/python" - <<PY
import json, subprocess
out = subprocess.run(
    ["curl", "-sS", "-X", "POST", "${BASE}/api/uploads",
     "-F", "files=@${UPDIR}/notes.md", "-F", "files=@${UPDIR}/extra.txt"],
    capture_output=True, text=True).stdout
try:
    print(json.loads(out)["uploadId"])
except Exception:
    print("")
PY
)"

if [[ -n "${UPID}" ]]; then
  ok "POST /api/uploads      two documents accepted, id returned"
else
  bad "POST /api/uploads      expected an uploadId"
fi

check "GET  /api/uploads/{id}  manifest lists both files with sizes" \
      "/api/uploads/${UPID}" \
      "d['uploadId']=='${UPID}' and sorted(f['name'] for f in d['files'])==['extra.txt','notes.md'] and all(f['bytes']>0 for f in d['files']) and d['totalBytes']==sum(f['bytes'] for f in d['files'])"

# ALL OR NOTHING. One unusable file rejects the set, so a PI cannot end up with a
# project quietly built from the one file that happened to be readable.
upload_expect() {  # upload_expect <label> <expected-status> <curl -F args...>
  local label="$1" want="$2"; shift 2
  local got
  got="$(curl -sS -o "${TMPDIR_SMOKE}/upbody" -w '%{http_code}' -X POST "${BASE}/api/uploads" "$@")"
  if [[ "${got}" == "${want}" ]]; then
    ok "POST /api/uploads      ${label}"
  else
    bad "POST /api/uploads      ${label} — got HTTP ${got}, expected ${want}"
    sed -n '1,2p' "${TMPDIR_SMOKE}/upbody" | sed 's/^/        /'
  fi
}

upload_expect "a .pdf rejects the whole upload" 400 \
  -F "files=@${UPDIR}/notes.md" -F "files=@${UPDIR}/paper.pdf"
upload_expect "an empty file is refused, not indexed as nothing" 400 \
  -F "files=@${UPDIR}/empty.md"
upload_expect "two files with one name are refused" 400 \
  -F "files=@${UPDIR}/notes.md" -F "files=@${UPDIR}/notes.md"

# The message must name the rule AND the file — a bare "invalid upload" makes a
# PI try the same thing again.
if "${VENV}/bin/python" -c "
import json,sys
d=json.load(open('${TMPDIR_SMOKE}/upbody'))
sys.exit(0 if isinstance(d.get('detail'),dict) and d['detail'].get('message') else 1)
" 2>/dev/null; then
  ok "POST /api/uploads      rejections carry a precise message"
else
  bad "POST /api/uploads      rejection body has no message"
fi

# A multipart filename is client-supplied and may be a path. Only the basename
# survives, and the directory it lands in is server-minted.
TRAV="$("${VENV}/bin/python" - <<PY
import json, subprocess
out = subprocess.run(
    ["curl", "-sS", "-X", "POST", "${BASE}/api/uploads",
     "-F", "files=@${UPDIR}/notes.md;filename=../../../../etc/passwd.md"],
    capture_output=True, text=True).stdout
try:
    print(",".join(f["name"] for f in json.loads(out)["files"]))
except Exception:
    print("?")
PY
)"
if [[ "${TRAV}" == "passwd.md" ]]; then
  ok "POST /api/uploads      a path in a filename is reduced to its basename"
else
  bad "POST /api/uploads      traversal not neutralised (stored as '${TRAV}')"
fi

if [[ "$(curl -sS -o /dev/null -w '%{http_code}' "${BASE}/api/uploads/nosuchupload")" == "404" ]]; then
  ok "GET  /api/uploads/{id}  unknown id is 404"
else
  bad "GET  /api/uploads/{id}  unknown id should be 404"
fi

# ===========================================================================
# THE PIPELINE
# ===========================================================================
# Everything above is read-only. What follows exercises the provisioning engine
# end to end, in mock mode, against a scratch database.
#
# A full mock run is ~23s by design (see MockExecutor.DELAYS) — the sleeps
# approximate the real shape, where the vCluster control plane is the slowest
# step. So this section takes about a minute in total. That is the cost of
# testing the pipeline rather than testing that a function returns a dict.
# ===========================================================================

PROJECT="${SMOKE_PROJECT:-mocktest}"
# The second project this script deploys, through the rag-stack template. A
# separate name because both runs coexist in the same scratch registry and the
# assertions about one must not be satisfiable by the other.
RAGPROJECT="${SMOKE_RAG_PROJECT:-ragtest}"

post() {  # post <path> <json> -> prints "status body"
  curl -sS -o "${TMPDIR_SMOKE}/body" -w '%{http_code}' \
    -X "${1}" -H 'Content-Type: application/json' \
    ${3:+--data "${3}"} "${BASE}${2}"
}

# poll_state <target-state> <timeout-seconds> [project]
#
# The project is an ARGUMENT with a default rather than only the ambient
# ${PROJECT}, because this script now deploys two of them. A prefix assignment
# (`PROJECT=x poll_state ...`) would work in bash and would also leak the value
# into the rest of the script, which is the kind of thing that makes a later
# assertion pass against the wrong deployment.
poll_state() {
  local target="$1" limit="$2" proj="${3:-${PROJECT}}" waited=0 state=""
  while [[ "${waited}" -lt "${limit}" ]]; do
    state="$("${VENV}/bin/python" - <<PY 2>/dev/null || echo "?"
import json, urllib.request
with urllib.request.urlopen("${BASE}/api/deployments/${proj}") as r:
    print(json.load(r)["state"])
PY
)"
    [[ "${state}" == "${target}" ]] && { echo "${state}"; return 0; }
    [[ "${state}" == "failed" && "${target}" != "failed" ]] && { echo "${state}"; return 1; }
    sleep 2; waited=$((waited + 2))
  done
  echo "${state:-timeout}"
  return 1
}

# step_table [project]
step_table() {
  local proj="${1:-${PROJECT}}"
  "${VENV}/bin/python" - <<PY
import json, urllib.request
with urllib.request.urlopen("${BASE}/api/deployments/${proj}") as r:
    d = json.load(r)
print(f"        {'PHASE':<10} {'STEP':<19} {'STATE':<10} {'SECS':>5}")
for s in d["steps"]:
    secs = "" if s["durationSeconds"] is None else f"{s['durationSeconds']:.1f}"
    print(f"        {s['phase']:<10} {s['stepId']:<19} {s['state']:<10} {secs:>5}")
print(f"        deployment state = {d['state']}   daysLeft = {d['daysLeft']}")
PY
}

echo
echo "==> pipeline, mock executor  (project '${PROJECT}')"
echo

BODY='{"templateId":"inference-endpoint","params":{"projectName":"'"${PROJECT}"'","model":"sklearn-iris","availability":"on-demand","tier":"small","durationDays":14}}'

# --- rejection paths, before the happy path -----------------------------------
BAD='{"templateId":"inference-endpoint","params":{"projectName":"inf-101","model":"sklearn-iris","availability":"on-demand","tier":"small","durationDays":14}}'
if [[ "$(post POST /api/deployments "${BAD}")" == "400" ]]; then
  ok "POST /api/deployments   reserved name 'inf-101' rejected 400"
else
  bad "POST /api/deployments   reserved name should be 400"
fi

# A TEMPLATE THAT DOES NOT EXIST IS A 404, AND THAT IS THE CHECK NOW.
#
# This used to post to `fine-tune` and assert 409, standing for "a coming-soon
# template cannot be started". Every sketch has since been either promoted or
# dropped, so the request 404'd and the check went red for a reason that was
# nothing to do with what it was testing. A catalogue with no sketches in it
# cannot demonstrate that rule; what it CAN still demonstrate is that the
# endpoint refuses a templateId it does not have, rather than starting
# something. The 409-on-coming-soon path is asserted where it can be:
# `/api/templates` above proves every available template is one that provisions,
# and app/main.py refuses any other maturity before validation runs.
BAD2='{"templateId":"no-such-template","params":{"projectName":"ftest"}}'
if [[ "$(post POST /api/deployments "${BAD2}")" == "404" ]]; then
  ok "POST /api/deployments   unknown template refused 404"
else
  bad "POST /api/deployments   unknown template should be 404"
fi

# --- dry run: renders, creates nothing ----------------------------------------
# The wizard's review step shows a PI these exact files before they commit. The
# assertion that matters is not that it returns 200 but that the CONTENT is right:
# the tier variant selected, the archetype annotations present or absent, and the
# expiry stamped — because those three are what a PI is actually being asked to
# approve, and a preview that got them wrong would be worse than no preview.
DRY="$(curl -sS -X POST -H 'Content-Type: application/json' --data "${BODY}" \
        "${BASE}/api/deployments?dry_run=1")"
echo "${DRY}" > "${TMPDIR_SMOKE}/dry.json"

export TMPDIR_SMOKE
if "${VENV}/bin/python" - <<'PY' 2>/dev/null
import json, os, sys
d = json.load(open(os.path.join(os.environ["TMPDIR_SMOKE"], "dry.json")))
sys.exit(0 if d.get("dryRun") is True and len(d["files"]) == 3 else 1)
PY
then ok "POST ?dry_run=1        three manifests rendered"
else bad "POST ?dry_run=1        expected 3 rendered files"; fi

if "${VENV}/bin/python" - <<'PY' 2>/dev/null
import json, os, re, sys
d = json.load(open(os.path.join(os.environ["TMPDIR_SMOKE"], "dry.json")))
by = {f["name"]: f["content"] for f in d["files"]}
ns, isvc = by["namespace-pack.yaml"], by["inference-service.yaml"]
checks = [
    re.search(r'hive\.ksl/expiry: "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"', ns),
    ns.count("kind: ResourceQuota") == 1,                 # one tier variant only
    "hive.ksl/tier: small" in ns,
    'min-scale: "0"' in isvc and "minReplicas: 0" in isvc, # on-demand
    "scale-down-delay" in isvc,                            # T-SZ annotations kept
    'storageUri: "pvc://models"' in isvc,                  # no trailing slash
    not re.search(r"^\s+runtime:", isvc, re.M),            # empty runtime omitted
]
sys.exit(0 if all(checks) else 1)
PY
then ok "POST ?dry_run=1        content correct (tier, archetype, expiry, uri)"
else bad "POST ?dry_run=1        rendered content is wrong"; fi

if [[ -z "$(curl -s "${BASE}/api/deployments?all=1" | tr -d '[]" \n')" ]]; then
  ok "POST ?dry_run=1        created nothing in the registry"
else
  bad "POST ?dry_run=1        should not have created a registry row"
fi

# --- create -------------------------------------------------------------------
if [[ "$(post POST /api/deployments "${BODY}")" == "202" ]]; then
  ok "POST /api/deployments   accepted 202"
else
  bad "POST /api/deployments   expected 202"
  sed -n '1,4p' "${TMPDIR_SMOKE}/body" | sed 's/^/        /'
fi

if [[ "$(post POST /api/deployments "${BODY}")" == "400" ]]; then
  ok "POST /api/deployments   duplicate project rejected 400"
else
  bad "POST /api/deployments   duplicate should be 400"
fi

# --- watch it run -------------------------------------------------------------
echo "  ..   waiting for the pipeline (~23s of mock sleeps)"
STATE="$(poll_state ready 90)" || true
if [[ "${STATE}" == "ready" ]]; then
  ok "pipeline               reached 'ready'"
else
  bad "pipeline               ended in '${STATE}', expected 'ready'"
fi

echo
step_table
echo

check "GET  /api/deployments/{id}  six provisioning steps, all succeeded" \
      "/api/deployments/${PROJECT}" \
      "[s['stepId'] for s in d['steps'] if s['phase']=='provision'] == ['approve','namespace-pack','model-seed','vcluster','inference-service','ready-check'] and all(s['state']=='succeeded' for s in d['steps'] if s['phase']=='provision')"

# Logs must exist per step, not just at the end — the progress UI is made of these.
check "GET  /api/deployments/{id}  every step carries a log" \
      "/api/deployments/${PROJECT}" \
      "all(s['log'] for s in d['steps'] if s['phase']=='provision')"

check "GET  /api/deployments/{id}  outputs captured by ready-check" \
      "/api/deployments/${PROJECT}" \
      "d['outputs']['endpointUrl']=='http://${PROJECT}.mock.hive.local' and d['outputs']['syncedName']=='${PROJECT}-x-default-x-${PROJECT}' and d['outputs']['kubeconfigAvailable'] is False"

check "GET  /api/deployments/{id}  daysLeft derived from durationDays" \
      "/api/deployments/${PROJECT}" \
      "d['daysLeft'] == 14"

check "GET  /api/deployments       project listed" \
      "/api/deployments" \
      "any(x['id']=='${PROJECT}' and x['state']=='ready' for x in d)"

# --- the two routes the project page needs and nothing else did --------------
# THESE EXIST BECAUSE THEY WERE ONCE DELETED BY ACCIDENT. A refactor of the
# uploads endpoint spliced out the block of main.py that happened to contain
# them; every other test still passed, the console still built, and the only
# symptom was a 404 on a tab nobody had clicked. Two cheap assertions close
# that gap for good.
check "GET  /api/deployments/{id}/events  merges pipeline history" \
      "/api/deployments/${PROJECT}/events" \
      "isinstance(d.get('entries'), list) and len(d['entries']) > 0 and all({'source','text'} <= set(e) for e in d['entries']) and any(e['source']=='pipeline' for e in d['entries'])"

# A mock project has no vCluster, so there is no kubeconfig to serve — and the
# 404 must be the ROUTE saying "not for this project", not FastAPI saying "no
# such route". The message is what tells them apart.
if [[ "$(curl -sS -o "${TMPDIR_SMOKE}/kc" -w '%{http_code}' "${BASE}/api/deployments/${PROJECT}/kubeconfig")" == "404" ]] \
   && grep -q "provisioned for real" "${TMPDIR_SMOKE}/kc"; then
  ok "GET  /api/deployments/{id}/kubeconfig  route exists, explains why there is none"
else
  bad "GET  /api/deployments/{id}/kubeconfig  route missing or wrong 404"
  sed -n '1,2p' "${TMPDIR_SMOKE}/kc" | sed 's/^/        /'
fi

# --- delete -------------------------------------------------------------------
if [[ "$(post DELETE "/api/deployments/${PROJECT}")" == "202" ]]; then
  ok "DELETE /api/deployments/{id}  teardown accepted 202"
else
  bad "DELETE /api/deployments/{id}  expected 202"
fi

echo "  ..   waiting for teardown"
STATE="$(poll_state deleted 60)" || true
if [[ "${STATE}" == "deleted" ]]; then
  ok "teardown               reached 'deleted'"
else
  bad "teardown               ended in '${STATE}'"
fi

check "GET  /api/deployments/{id}  teardown ran in template order" \
      "/api/deployments/${PROJECT}" \
      "[s['stepId'] for s in d['steps'] if s['phase']=='teardown'] == ['inference-service','vcluster','namespace-pack']"

# The provisioning history must SURVIVE the teardown — that is the whole reason
# the steps table carries a phase rather than letting teardown overwrite.
check "GET  /api/deployments/{id}  provisioning history survived teardown" \
      "/api/deployments/${PROJECT}" \
      "len([s for s in d['steps'] if s['phase']=='provision']) == 6"

check "GET  /api/deployments       soft-deleted, hidden by default" \
      "/api/deployments" \
      "not any(x['id']=='${PROJECT}' for x in d)"

check "GET  /api/deployments?all=1 soft-deleted, still in history" \
      "/api/deployments?all=1" \
      "any(x['id']=='${PROJECT}' and x['state']=='deleted' and x['deletedAt'] for x in d)"

# ---------------------------------------------------------------------------
# EVERY PATH THE CONSOLE CALLS IS A ROUTE THE BFF HAS
# ---------------------------------------------------------------------------
# A cross-check rather than a test: it reads the paths out of studio/frontend/src
# and the routes out of the running server's own OpenAPI schema, and complains
# about any the console calls and the BFF does not serve.
#
# It exists because that is exactly what went wrong once — a refactor removed two
# routes, and nothing noticed until a tab 404'd. Assertions catch the routes
# somebody thought to assert; this catches the ones nobody did.
# BASE and REPO_ROOT reach Python as ENVIRONMENT VARIABLES, not by shell
# interpolation. The heredoc is quoted (<<'PY') so the shell leaves the Python
# alone — which it must, because the script is full of `$` in regexes and
# f-strings that an unquoted heredoc would eat.
if SMOKE_BASE="${BASE}" SMOKE_SRC="${REPO_ROOT}/studio/frontend/src" \
   "${VENV}/bin/python" - <<'PY' 2>"${TMPDIR_SMOKE}/routes.err"
import json, os, re, sys, urllib.request
from pathlib import Path

base = os.environ["SMOKE_BASE"]
src = Path(os.environ["SMOKE_SRC"])

with urllib.request.urlopen(f"{base}/openapi.json") as r:
    served = set(json.load(r)["paths"])


def norm(path: str) -> str:
    """Reduce both sides to a comparable shape.

    A template placeholder in the frontend (${id}) and a path parameter in
    FastAPI ({deployment_id}) name the same segment, so both become "{}".

    The truncation at a bare `$` handles the one real pattern the simple
    substitution cannot: `/api/deployments${showAll ? "?all=1" : ""}` — a
    template literal whose expression contains quotes, so the outer match ends
    inside it. The base path is what this check is about, so cutting there is
    the right answer rather than a workaround.
    """
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = path.split("$")[0]
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.split("?")[0].rstrip("/")


served_norm = {norm(p) for p in served}

called = set()
for f in sorted(src.rglob("*.js*")):
    for m in re.finditer(r"""["'`](/api/[^"'`]*)["'`]""", f.read_text()):
        raw = m.group(1)
        if "..." in raw:          # the illustrative path in api.js's header
            continue
        called.add(norm(raw))

missing = sorted(c for c in called if c not in served_norm)
if missing:
    print("the console calls paths the BFF does not serve:", file=sys.stderr)
    for path in missing:
        print(f"  {path}", file=sys.stderr)
    sys.exit(1)
print(f"{len(called)} paths, all served")
PY
then
  ok "console/BFF     every /api path the console calls is a real route"
else
  bad "console/BFF     the console calls a path the BFF does not serve"
  sed 's/^/        /' "${TMPDIR_SMOKE}/routes.err"
fi

# ===========================================================================
# THE RAG STACK  (studio R1)
# ===========================================================================
# The second template that provisions. Everything below is MOCK MODE — no
# cluster is touched, no Qdrant is started, no document is read — so what is
# proved here is the part a cluster could not prove any faster:
#
#   * the wizard schema resolves TWO filtered model enums from one closed list,
#     and each offers only models that could actually work in that slot;
#   * the plan is eleven steps in the right order, with `ready-check` in the
#     MIDDLE rather than at the end, because rag-api needs what it produces;
#   * the dry run renders seven manifests, under the names the executor writes,
#     and the RAG-specific content in them is correct;
#   * the server refuses what it must: an immutable parameter that was changed,
#     a chat model chosen as the embedding model, an upload id that is a path;
#   * a full run reaches `ready` with the declared outputs, and teardown removes
#     it in the template's declared order while the history survives.
#
# The renderer and the chunker have their own offline tests, run first, because
# a rendering fault should be reported as a rendering fault rather than as a
# pipeline that mysteriously failed at namespace-pack.
#
# A mock RAG run is ~54s of deliberate sleeps — five more steps than an
# inference run — plus ~9s of teardown.
# ===========================================================================

echo
echo "==> offline unit tests (no server, no cluster)"
echo

for suite in test-render test-chunker; do
  if "${VENV}/bin/python" "${REPO_ROOT}/tests/${suite}.py" >"${TMPDIR_SMOKE}/${suite}.log" 2>&1; then
    ok "tests/${suite}.py     $(grep -o '[0-9]* passed, [0-9]* failed' "${TMPDIR_SMOKE}/${suite}.log" | tail -1)"
  else
    bad "tests/${suite}.py     failed"
    tail -12 "${TMPDIR_SMOKE}/${suite}.log" | sed 's/^/        /'
  fi
done

echo
echo "==> rag-stack, mock executor  (project '${RAGPROJECT}')"
echo

# --- the wizard schema -------------------------------------------------------
check "/api/templates/rag-stack  nine parameters in order" \
      "/api/templates/rag-stack" \
      "[p['name'] for p in d['spec']['parameters']] == ['projectName','baseModel','embeddingModel','vectorDb','corpusUploadId','topK','availability','tier','durationDays']"

# THE LOAD-BEARING SCHEMA ASSERTION. Both enums come from one closed list and
# are FILTERED by interface. If filtering broke, both dropdowns would show every
# model and a PI could pick an embedding model to answer questions with — which
# would deploy, and then fail at the runtime with nothing explaining why.
check "/api/templates/rag-stack  baseModel offers ONLY chat models" \
      "/api/templates/rag-stack" \
      "any(p['name']=='baseModel' and [v['value'] for v in p.get('values',[])] == ['qwen2.5-0.5b-instruct-q4'] for p in d['spec']['parameters'])"

check "/api/templates/rag-stack  embeddingModel offers ONLY embed models" \
      "/api/templates/rag-stack" \
      "any(p['name']=='embeddingModel' and [v['value'] for v in p.get('values',[])] == ['bge-small-en-v1.5'] and p.get('default')=='bge-small-en-v1.5' for p in d['spec']['parameters'])"

check "/api/templates/rag-stack  vectorDb is an immutable enum with one option" \
      "/api/templates/rag-stack" \
      "any(p['name']=='vectorDb' and p.get('immutable') is True and [v['value'] for v in p.get('values',[])] == ['qdrant'] and p.get('default')=='qdrant' for p in d['spec']['parameters'])"

check "/api/templates/rag-stack  topK bounded 1-8 by the runtime's context" \
      "/api/templates/rag-stack" \
      "any(p['name']=='topK' and p['minimum']==1 and p['maximum']==8 and p['default']==3 for p in d['spec']['parameters'])"

check "/api/templates/rag-stack  eight resources, five outputs, rag-small quota" \
      "/api/templates/rag-stack" \
      "d['spec']['resources'] == ['namespace-pack','model-seed','vcluster','vector-db','corpus-upload','ingestion','inference-service','rag-api'] and d['spec']['outputs'] == ['ragEndpointUrl','chatEndpointUrl','collectionName','indexedChunks','kubeconfig'] and d['spec']['quotaProfile'] == 'rag-small'"

check "/api/templates/rag-stack  teardown is NOT the reverse of creation" \
      "/api/templates/rag-stack" \
      "d['spec']['lifecycle']['teardownOrder'] == ['rag-api','ingestion','vector-db','inference-service','vcluster','namespace-pack']"

# --- rejection paths ---------------------------------------------------------
RAGBODY='{"templateId":"rag-stack","params":{"projectName":"'"${RAGPROJECT}"'","baseModel":"qwen2.5-0.5b-instruct-q4","embeddingModel":"bge-small-en-v1.5","vectorDb":"qdrant","corpusUploadId":"","topK":3,"availability":"on-demand","tier":"small","durationDays":30}}'

# The wizard renders vectorDb read-only. That is a hint; THIS is the control.
RAGBAD='{"templateId":"rag-stack","params":{"projectName":"ragbad","baseModel":"qwen2.5-0.5b-instruct-q4","embeddingModel":"bge-small-en-v1.5","vectorDb":"pinecone","corpusUploadId":"","topK":3,"availability":"on-demand","tier":"small","durationDays":30}}'
if [[ "$(post POST /api/deployments "${RAGBAD}")" == "400" ]]; then
  ok "POST /api/deployments   immutable vectorDb refused server-side 400"
else
  bad "POST /api/deployments   changed immutable parameter should be 400"
fi

# The closed list is enforced PER SLOT, not merely per file: a chat model is not
# a valid answer to "how should my documents be indexed?"
RAGBAD2='{"templateId":"rag-stack","params":{"projectName":"ragbad","baseModel":"qwen2.5-0.5b-instruct-q4","embeddingModel":"qwen2.5-0.5b-instruct-q4","vectorDb":"qdrant","corpusUploadId":"","topK":3,"availability":"on-demand","tier":"small","durationDays":30}}'
if [[ "$(post POST /api/deployments "${RAGBAD2}")" == "400" ]]; then
  ok "POST /api/deployments   a chat model refused as the embedding model 400"
else
  bad "POST /api/deployments   cross-slot model should be 400"
fi

# corpusUploadId is joined onto tenants/_uploads/. A traversal must never get
# past validation — and the executor resolves the path and checks again.
RAGBAD3='{"templateId":"rag-stack","params":{"projectName":"ragbad","baseModel":"qwen2.5-0.5b-instruct-q4","embeddingModel":"bge-small-en-v1.5","vectorDb":"qdrant","corpusUploadId":"../../etc","topK":3,"availability":"on-demand","tier":"small","durationDays":30}}'
if [[ "$(post POST /api/deployments "${RAGBAD3}")" == "400" ]]; then
  ok "POST /api/deployments   path traversal in corpusUploadId refused 400"
else
  bad "POST /api/deployments   traversal in corpusUploadId should be 400"
fi

# --- dry run -----------------------------------------------------------------
curl -sS -X POST -H 'Content-Type: application/json' --data "${RAGBODY}" \
     "${BASE}/api/deployments?dry_run=1" > "${TMPDIR_SMOKE}/ragdry.json"

if "${VENV}/bin/python" "${REPO_ROOT}/tests/assert-rag-dryrun.py" \
     "${TMPDIR_SMOKE}/ragdry.json" "${RAGPROJECT}" >"${TMPDIR_SMOKE}/ragdry.log" 2>&1; then
  ok "POST ?dry_run=1        seven manifests, eleven steps, RAG content correct"
else
  bad "POST ?dry_run=1        the rendered RAG deployment is wrong"
  sed 's/^/        /' "${TMPDIR_SMOKE}/ragdry.log"
fi

if [[ -z "$(curl -s "${BASE}/api/deployments?all=1" | "${VENV}/bin/python" -c "
import json,sys
print(','.join(x['id'] for x in json.load(sys.stdin) if x['id'] == '${RAGPROJECT}'))")" ]]; then
  ok "POST ?dry_run=1        created nothing in the registry"
else
  bad "POST ?dry_run=1        should not have created a registry row"
fi

# --- the run -----------------------------------------------------------------
if [[ "$(post POST /api/deployments "${RAGBODY}")" == "202" ]]; then
  ok "POST /api/deployments   rag-stack accepted 202"
else
  bad "POST /api/deployments   expected 202"
  sed -n '1,4p' "${TMPDIR_SMOKE}/body" | sed 's/^/        /'
fi

echo "  ..   waiting for the RAG pipeline (~54s of mock sleeps, 11 steps)"
RAGSTATE="$(poll_state ready 150 "${RAGPROJECT}")" || true
if [[ "${RAGSTATE}" == "ready" ]]; then
  ok "pipeline               rag-stack reached 'ready'"
else
  bad "pipeline               ended in '${RAGSTATE}', expected 'ready'"
fi

echo
step_table "${RAGPROJECT}"
echo

check "GET  /api/deployments/{id}  eleven provisioning steps, all succeeded" \
      "/api/deployments/${RAGPROJECT}" \
      "[s['stepId'] for s in d['steps'] if s['phase']=='provision'] == ['approve','namespace-pack','model-seed','vcluster','vector-db','corpus-upload','ingestion','inference-service','ready-check','rag-api','rag-ready-check'] and all(s['state']=='succeeded' for s in d['steps'] if s['phase']=='provision')"

# The template declares five outputs; a run that reached `ready` must have
# produced them, and they come from three DIFFERENT steps — ingestion,
# ready-check and rag-ready-check — which is what the accumulating context in
# StepContext.outputs is for.
check "GET  /api/deployments/{id}  RAG outputs captured across three steps" \
      "/api/deployments/${RAGPROJECT}" \
      "d['outputs']['collectionName']=='${RAGPROJECT}-corpus' and d['outputs']['indexedChunks']==37 and d['outputs']['indexedFiles']==4 and d['outputs']['ragEndpointUrl'].startswith('http://${RAGPROJECT}-rag.p-${RAGPROJECT}') and d['outputs']['chatEndpointUrl'].startswith('http://')"

# The mock prints a canned INGEST_RESULT line spelled exactly as ingest.py
# prints it, so the executor's parser is exercised without a cluster.
check "GET  /api/deployments/{id}  the ingestion step logged an INGEST_RESULT line" \
      "/api/deployments/${RAGPROJECT}" \
      "any(s['stepId']=='ingestion' and 'INGEST_RESULT' in (s['log'] or '') for s in d['steps'])"

check "GET  /api/deployments/{id}  the corpus step names the demo-corpus fallback" \
      "/api/deployments/${RAGPROJECT}" \
      "any(s['stepId']=='corpus-upload' and 'demo-corpus' in (s['log'] or '') for s in d['steps'])"

# Mock mode is honest about what it did not do. A canned index reported as a
# real one would make a mock run indistinguishable from a real one on the
# project page, which is exactly what invariant (e) forbids.
check "GET  /api/deployments/{id}  the mock says its numbers are canned" \
      "/api/deployments/${RAGPROJECT}" \
      "any(s['stepId']=='ingestion' and 'NOTHING WAS INDEXED' in (s['log'] or '') for s in d['steps']) and d['outputs']['smokeOk'] is False"

# --- teardown ----------------------------------------------------------------
if [[ "$(post DELETE "/api/deployments/${RAGPROJECT}")" == "202" ]]; then
  ok "DELETE /api/deployments/{id}  rag-stack teardown accepted 202"
else
  bad "DELETE /api/deployments/{id}  expected 202"
fi

echo "  ..   waiting for RAG teardown (6 steps)"
RAGSTATE="$(poll_state deleted 90 "${RAGPROJECT}")" || true
if [[ "${RAGSTATE}" == "deleted" ]]; then
  ok "teardown               rag-stack reached 'deleted'"
else
  bad "teardown               ended in '${RAGSTATE}'"
fi

check "GET  /api/deployments/{id}  teardown ran in the template's order" \
      "/api/deployments/${RAGPROJECT}" \
      "[s['stepId'] for s in d['steps'] if s['phase']=='teardown'] == ['rag-api','ingestion','vector-db','inference-service','vcluster','namespace-pack']"

check "GET  /api/deployments/{id}  provisioning history survived teardown" \
      "/api/deployments/${RAGPROJECT}" \
      "len([s for s in d['steps'] if s['phase']=='provision']) == 11"

# ===========================================================================
# FAILURE INJECTION, AND WHY THE RETRY WORKS
# ===========================================================================
# STUDIO_FAIL_STEP is read FRESH at the moment each step executes, not once at
# startup (pipeline.injected_failure_step). That is what makes this section
# possible in a single run of a single server process:
#
#   1. set STUDIO_FAIL_STEP=vcluster in the SERVER's environment -> the run fails
#      at vcluster, with the three later steps marked `skipped`, not `pending`.
#   2. clear it -> a retry resumes from vcluster and completes.
#
# Had the variable been captured at startup, step 2 would be impossible without
# restarting the server, and the failure path could only ever be half-tested.
#
# ONE PRACTICAL LIMIT, STATED PLAINLY. A shell script cannot reach into a running
# process and change its environment, so this script restarts the server between
# the two halves rather than clearing the variable in place. The dynamic read is
# still what makes the retry work at all — it is why the resumed run consults the
# variable again instead of reusing a decision made at startup — but proving the
# in-process case would need a test that runs inside the server, which this is
# not. What is proven here: the failure lands on the named step, later steps are
# marked `skipped`, the reason reaches the log, and the retry resumes from the
# failed step without re-running the ones that already succeeded.
# ===========================================================================

echo
echo "==> failure injection (STUDIO_FAIL_STEP=vcluster)"
echo

FAILPROJ="failtest"
if [[ "${USE_RUNNING}" -eq 0 ]]; then
  kill "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true

  export STUDIO_FAIL_STEP=vcluster
  ( cd "${BACKEND_DIR}" && exec "${VENV}/bin/uvicorn" app.main:app --port "${PORT}" ) \
    >"${TMPDIR_SMOKE}/server2.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    curl -sf "${BASE}/api/health" >/dev/null 2>&1 && break
    sleep 0.5
  done

  FBODY='{"templateId":"inference-endpoint","params":{"projectName":"'"${FAILPROJ}"'","model":"sklearn-iris","availability":"always-on","tier":"medium","durationDays":30}}'
  post POST /api/deployments "${FBODY}" >/dev/null

  echo "  ..   waiting for the injected failure"
  FSTATE=""
  for _ in $(seq 1 30); do
    FSTATE="$("${VENV}/bin/python" -c "
import json,urllib.request
try:
    print(json.load(urllib.request.urlopen('${BASE}/api/deployments/${FAILPROJ}'))['state'])
except Exception: print('?')
" 2>/dev/null)"
    [[ "${FSTATE}" == "failed" ]] && break
    sleep 2
  done

  if [[ "${FSTATE}" == "failed" ]]; then
    ok "STUDIO_FAIL_STEP       run failed as instructed"
  else
    bad "STUDIO_FAIL_STEP       expected 'failed', got '${FSTATE}'"
  fi

  "${VENV}/bin/python" - <<PY
import json, urllib.request
d = json.load(urllib.request.urlopen("${BASE}/api/deployments/${FAILPROJ}"))
print(f"        {'STEP':<19} {'STATE':<10}")
for s in d["steps"]:
    print(f"        {s['stepId']:<19} {s['state']:<10}")
PY

  # Steps before the failure succeeded; the failed one is named; the rest are
  # `skipped`, which is what distinguishes "never ran" from "still running".
  if "${VENV}/bin/python" -c "
import json,urllib.request,sys
d=json.load(urllib.request.urlopen('${BASE}/api/deployments/${FAILPROJ}'))
st={s['stepId']:s['state'] for s in d['steps']}
sys.exit(0 if st.get('vcluster')=='failed'
             and st.get('approve')=='succeeded'
             and st.get('inference-service')=='skipped'
             and st.get('ready-check')=='skipped' else 1)
" 2>/dev/null; then
    ok "STUDIO_FAIL_STEP       vcluster failed, later steps 'skipped' not 'pending'"
  else
    bad "STUDIO_FAIL_STEP       step states after failure are wrong"
  fi

  if "${VENV}/bin/python" -c "
import json,urllib.request,sys
d=json.load(urllib.request.urlopen('${BASE}/api/deployments/${FAILPROJ}'))
log=[s['log'] for s in d['steps'] if s['stepId']=='vcluster'][0] or ''
sys.exit(0 if 'injected failure' in log else 1)
" 2>/dev/null; then
    ok "STUDIO_FAIL_STEP       reason recorded in the step's log"
  else
    bad "STUDIO_FAIL_STEP       'injected failure' missing from the log"
  fi

  # --- the retry, with the injection cleared ----------------------------------
  # The variable is read per step at execution time, so clearing it and restarting
  # the SERVER is not required for correctness — but a shell cannot reach into a
  # running process's environment, so the server is restarted without it. The
  # deployment row, its steps and its logs all persist in the scratch database,
  # which is what the retry resumes from.
  kill "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true
  unset STUDIO_FAIL_STEP

  ( cd "${BACKEND_DIR}" && exec "${VENV}/bin/uvicorn" app.main:app --port "${PORT}" ) \
    >"${TMPDIR_SMOKE}/server3.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    curl -sf "${BASE}/api/health" >/dev/null 2>&1 && break
    sleep 0.5
  done

  if [[ "$(post POST "/api/deployments/${FAILPROJ}/retry")" == "202" ]]; then
    ok "POST .../retry         accepted 202"
  else
    bad "POST .../retry         expected 202"
    sed -n '1,3p' "${TMPDIR_SMOKE}/body" | sed 's/^/        /'
  fi

  echo "  ..   waiting for the resumed run"
  RSTATE=""
  for _ in $(seq 1 40); do
    RSTATE="$("${VENV}/bin/python" -c "
import json,urllib.request
try:
    print(json.load(urllib.request.urlopen('${BASE}/api/deployments/${FAILPROJ}'))['state'])
except Exception: print('?')
" 2>/dev/null)"
    [[ "${RSTATE}" == "ready" || "${RSTATE}" == "failed" ]] && break
    sleep 2
  done

  if [[ "${RSTATE}" == "ready" ]]; then
    ok "retry                  resumed and reached 'ready'"
  else
    bad "retry                  ended in '${RSTATE}', expected 'ready'"
  fi

  # The steps that had already succeeded must NOT have run again — that is what
  # "resume from the failed step" means, as opposed to "start over".
  if "${VENV}/bin/python" -c "
import json,urllib.request,sys
d=json.load(urllib.request.urlopen('${BASE}/api/deployments/${FAILPROJ}'))
logs={s['stepId']:(s['log'] or '') for s in d['steps']}
sys.exit(0 if 'already completed in an earlier attempt' in logs.get('approve','') else 1)
" 2>/dev/null; then
    ok "retry                  earlier steps skipped, not re-run"
  else
    bad "retry                  earlier steps appear to have re-run"
  fi
fi

echo
echo "==> ${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]] || exit 1
