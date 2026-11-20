#!/usr/bin/env bash
#
# studio-down.sh — stop the console and the BFF. The counterpart to studio-up.sh.
#
#   Usage:
#     scripts/studio-down.sh              # stop the console + BFF
#     scripts/studio-down.sh --status     # show what is running, stop nothing
#
# ===========================================================================
# WHAT THIS STOPS, AND WHAT IT DELIBERATELY DOES NOT
# ===========================================================================
# STOPS:  the Vite dev server (:5173), the BFF (:8000), and the `kubectl
#         port-forward` children the BFF owns (:18080 and the per-project ones).
#
# LEAVES: the kind cluster, and every project deployed into it.
#
# That split is the point. A Studio project is a REAL TENANT — a namespace, a
# quota, a vCluster, a served model, a vector database — and it does not belong
# to the console any more than a running InferenceService belongs to `kubectl`.
# Closing the console is closing a window; it is not a teardown, and a script
# called `down` that quietly deleted a PI's project would be the single most
# destructive misunderstanding available here.
#
# To remove a project, use the console (Settings -> Danger zone) or
# DELETE /api/deployments/<id>, which runs the template's declared teardownOrder.
# To stop the cluster without losing anything, see the end of this file.
#
# ===========================================================================
# WHY SIGTERM AND NEVER `kill -9`
# ===========================================================================
# The BFF owns several `kubectl port-forward` child processes: the shared Kourier
# forward on 18080, plus a lazily-created one per project the playground has
# talked to. They are cleaned up by the FastAPI lifespan shutdown
# (`dataplane.data_path.stop()` and `forwards.stop_all()`), which only runs if
# the process is asked to exit politely.
#
# `kill -9` skips all of that. The children are reparented to init and keep
# running — holding ports, holding API-server connections, and invisible unless
# somebody thinks to look for them. So this sends SIGTERM, waits, and only
# escalates if a process genuinely refuses to go.
# ===========================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"

STATUS_ONLY=0
[[ "${1:-}" == "--status" ]] && STATUS_ONLY=1

# --- what is running --------------------------------------------------------
#
# MATCHED BY COMMAND LINE, NOT BY PORT, and that is a real safety decision.
# `lsof -ti :5173` returns every process with a socket on that port — which
# includes the BROWSER TAB you have open on the console. Killing by port would
# kill a Chrome helper process. Verified: on this machine `lsof -ti :5173`
# returns the Vite server and a Google Chrome Helper.
bff_pids()   { pgrep -f "uvicorn app.main:app" 2>/dev/null; }
vite_pids()  { pgrep -f "node .*node_modules/.bin/vite" 2>/dev/null; }
npm_pids()   { pgrep -f "npm run dev" 2>/dev/null; }
# Only the forwards the BFF spawned. A PI running their own `kubectl
# port-forward` in another terminal is none of this script's business, so the
# match is narrowed to the two shapes the BFF creates.
fwd_pids()   { pgrep -f "kubectl port-forward svc/(kourier|.*-rag|.*-qdrant) " 2>/dev/null; }

# `pgrep` prints one pid per line. Flattened to a single space-separated line
# before it reaches printf, or a three-pid group prints across three lines with
# the label on only the first — which reads as three separate broken entries.
flatten() { tr '\n' ' ' | tr -s ' ' | sed 's/^ *//;s/ *$//'; }

# WHICH MODE the running backend is in, asked of the backend rather than
# inferred from anything. The mode is fixed when the process starts and there is
# no way to read it from the outside except this — a `ps` line does not show it,
# because it arrives as an environment variable.
live_mode() {
  curl -sf -m 2 "http://127.0.0.1:${API_PORT}/api/health" 2>/dev/null \
    | sed -n 's/.*"executor" *: *"\([a-z]*\)".*/\1/p'
}

report() {
  local found=0 name pids mode
  mode="$(live_mode)"
  while IFS='|' read -r name pids; do
    [[ -z "${name}" ]] && continue
    if [[ -n "${pids}" ]]; then
      printf '  %-22s running   pid %s\n' "${name}" "${pids}"
      found=1
    else
      printf '  %-22s stopped\n' "${name}"
    fi
  done <<EOF
BFF (:${API_PORT})|$(bff_pids | flatten)
console (:${UI_PORT})|$( { vite_pids; npm_pids; } | flatten)
port-forwards|$(fwd_pids | flatten)
EOF

  # THE ANSWER TO THE QUESTION PEOPLE ACTUALLY HAVE when they run --status.
  # "Is it up?" is usually a proxy for "is it going to create real things if I
  # click Deploy?", and that cannot be read off a process list.
  if [[ -n "${mode}" ]]; then
    if [[ "${mode}" == "real" ]]; then
      printf '  %-22s %s\n' "executor" "real   — Deploy PROVISIONS ON THE CLUSTER"
    else
      printf '  %-22s %s\n' "executor" "mock   — nothing is really created"
    fi
  fi
  return $((1 - found))
}

echo "==> what is running"
report
running=$?

if [[ "${STATUS_ONLY}" -eq 1 ]]; then
  echo
  echo "  (--status: nothing was stopped)"
  exit 0
fi

if [[ "${running}" -ne 0 ]]; then
  echo
  echo "  nothing to stop."
  exit 0
fi

# --- stop, politely ---------------------------------------------------------
#
# ORDER MATTERS, mildly: the console first, so that a page left open stops
# polling an API that is about to disappear and does not fill the terminal with
# connection errors on the way out.
echo
echo "==> stopping"

stop_group() {  # stop_group <label> <pids...>
  local label="$1"; shift
  local pids=("$@")
  [[ ${#pids[@]} -eq 0 ]] && return 0
  kill "${pids[@]}" 2>/dev/null
  # Up to 10s for a graceful exit. The BFF needs a moment: its shutdown closes
  # every port-forward it owns before the process itself goes.
  for _ in $(seq 1 20); do
    local alive=0
    for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [[ "${alive}" -eq 0 ]] && { printf '  %-22s stopped cleanly\n' "${label}"; return 0; }
    sleep 0.5
  done
  # Only now, and say so — an escalation that happens silently is one nobody
  # investigates, and for the BFF it means orphaned forwards.
  printf '  %-22s did not exit in 10s — sending SIGKILL\n' "${label}"
  kill -9 "${pids[@]}" 2>/dev/null
  return 1
}

# shellcheck disable=SC2046
stop_group "console" $(vite_pids) $(npm_pids)
# shellcheck disable=SC2046
stop_group "BFF" $(bff_pids)

# --- sweep ------------------------------------------------------------------
#
# The BFF's own shutdown should have taken these with it. Checking anyway,
# because "should have" is how orphaned processes accumulate — and if any are
# left it means the graceful path did not run, which is worth saying out loud.
sleep 1
leftover="$(fwd_pids | tr '\n' ' ' | sed 's/ *$//')"
if [[ -n "${leftover}" ]]; then
  echo "  port-forwards          ${leftover} survived the BFF — closing"
  # shellcheck disable=SC2086
  kill ${leftover} 2>/dev/null
  sleep 1
fi

echo
echo "==> after"
report >/dev/null
report

# --- what is still out there ------------------------------------------------
echo
if kubectl cluster-info >/dev/null 2>&1; then
  projects="$(kubectl get ns -l hive.ksl/kind=tenant -o jsonpath='{range .items[*]}{.metadata.labels.hive\.ksl/project}{" "}{end}' 2>/dev/null)"
  echo "==> still running on the cluster (NOT stopped by this script)"
  echo "  tenants: ${projects:-none}"
  echo
  echo "  These are real projects and they keep running. To remove one, use the"
  echo "  console's Settings -> Danger zone, or:"
  echo "      curl -X DELETE http://127.0.0.1:${API_PORT}/api/deployments/<id>   # needs the BFF up"
  echo
  echo "  To stop the whole cluster without losing anything:"
  echo "      docker stop \$(kind get nodes --name hive-host | tr '\\n' ' ')"
  echo "      docker start \$(kind get nodes --name hive-host | tr '\\n' ' ')   # to bring it back"
  echo
  echo "  !! After ANY restart of the nodes or Docker Desktop, re-run"
  echo "     scripts/fake-gpus.sh — the fake nvidia.com/gpu capacity is a node-status"
  echo "     patch and the kubelet wipes it on restart. Symptom: pods Pending with"
  echo "     'Insufficient nvidia.com/gpu'."
else
  echo "==> the cluster is not reachable (already stopped, or Docker is down)"
fi
