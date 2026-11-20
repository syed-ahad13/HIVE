# Studio S7 — what a PI sees when it goes wrong

**Status: PASSED, after two fixes this audit found** · 2026-08-23 · `STUDIO_EXECUTOR=real`

The failure path is a feature, not an afterthought: a PI must see which step failed
and why, and the way out must actually work. This audit injected a failure at three
different points of a real provisioning run against a throwaway project, and found
**two defects — both in the recovery path, neither reachable in mock mode.**

| Injected at | Failure correct | No side effects | Retry resumes | Delete cleans |
|---|---|---|---|---|
| `namespace-pack` (first step) | ✅ | ✅ | — | ✅ *after fix 2* |
| `vcluster` (middle) | ✅ | ✅ | ✅ → `ready` | ✅ *after fix 1* |
| `ready-check` (last step) | ✅ | ✅ | — | ✅ *after fix 2* |

Throughout, **`p-inf-101`, `p-inf-103`, `p-jdi` and `p-demo-cv` were untouched** —
a third project was created and destroyed five times alongside them.

---

## 1. Failing at the first step

`STUDIO_FAIL_STEP=namespace-pack`

```
[provision] approve              succeeded
[provision] namespace-pack       failed      <- injected
[provision] model-seed           skipped
[provision] vcluster             skipped
[provision] inference-service    skipped
[provision] ready-check          skipped

side effects: namespace none · seed job none · vCluster none · host isvc none
```

Two things this establishes.

**The injection raises before any side effect.** `_raise_if_injected` is the first
statement of every step, so an injected failure has the same shape as a step that
fails on its first real action — nothing half-created, nothing to reconcile.

**The four later steps say `skipped`, not `pending`.** A failed run whose remaining
steps read "pending" looks like it is still going. `skipped` says plainly that they
never got their turn.

---

## 2. Failing in the middle, then resuming

`STUDIO_FAIL_STEP=vcluster`

```
[provision] approve              succeeded
[provision] namespace-pack       succeeded
[provision] model-seed           succeeded
[provision] vcluster             failed      <- injected
[provision] inference-service    skipped
[provision] ready-check          skipped

side effects: namespace Active · seed job Complete · vCluster none · host isvc none
```

The namespace and the seeded model survive — they were created by steps that
succeeded — and no vCluster exists, because the injection fired before
`vcluster create` ran. That is the point of injecting *before* the work: the
project is in exactly the state a genuine failure at that step would leave it.

**Retry, with the injection cleared:**

```
retry 202
after retry: ready
[provision] approve              succeeded
[provision] namespace-pack       succeeded
[provision] model-seed           succeeded
[provision] vcluster             succeeded
[provision] inference-service    succeeded
[provision] ready-check          succeeded
```

The three steps that already succeeded were not re-run — no namespace rebuilt, no
model re-downloaded. Resume, not restart.

---

## 3. Failing at the last step

`STUDIO_FAIL_STEP=ready-check`

```
[provision] approve              succeeded
[provision] namespace-pack       succeeded
[provision] model-seed           succeeded
[provision] vcluster             succeeded
[provision] inference-service    succeeded
[provision] ready-check          failed      <- injected

side effects: namespace Active · seed job Complete · vCluster Running
              host isvc failtest-x-default-x-failtest
```

Everything is built and the host InferenceService exists — correct, because
`ready-check` runs *after* `inference-service`. This is the most expensive failure
to clean up, which is what made it useful.

---

## 4. What the PI sees

From the S6 screenshots of the same UI, against a real failed run:

```
✕  Creating your private cluster (vCluster)                          0s
   This step failed. Everything after it was not attempted.
   ▾ What the step reported
     ┌──────────────────────────────────────────────┐
     │ --- vcluster started ---                     │
     │ FAILED: injected failure                     │
     └──────────────────────────────────────────────┘

–  Publishing your model to the shared platform (KServe)
–  Waiting for your endpoint to answer

What you can do
  Retry picks up from "Creating your private cluster (vCluster)" — the steps
  that already finished are not repeated. If it keeps failing, remove the
  project and start again with a different name.

  [ Retry step ]  [ Delete and start over ]
```

The log **expands by itself** the moment the step fails (fixed in S4 — a
`useState` initialiser had captured `pending` at mount and never re-evaluated), and
both actions are offered together. For a real failure the log also carries the
exact command and its stderr tail, which is how the S5 and S6 cluster bugs were
diagnosed without leaving the browser.

---

## 5. Defect 1 — the injection sabotaged its own escape hatch

**Found by:** deleting a project while `STUDIO_FAIL_STEP` was still set.

Teardown called the same `_raise_if_injected(step_id)` as provisioning. So
`STUDIO_FAIL_STEP=vcluster` broke the provisioning run **and then broke
"Delete and start over"** at the identically-named teardown step. The documented
way out of a failed run was disabled by the very flag that caused the failure.

A failure-injection hook that disables the recovery path is the one thing it must
never do. Teardown injection now needs its own spelling:

```
STUDIO_FAIL_STEP=vcluster            fails while BUILDING     (recovery still works)
STUDIO_FAIL_STEP=teardown:vcluster   fails while REMOVING     (explicit, opt-in)
```

**Verified:** case 2's delete ran with `STUDIO_FAIL_STEP=vcluster` still set and
reached `deleted`, namespace gone.

> This extends the `STUDIO_FAIL_STEP` contract in CLAUDE.md §6, which describes the
> value as "a step id". Worth reflecting there.

---

## 6. Defect 2 — early failures could not be deleted at all

**Found by:** case 1's delete returning 202 but leaving the row `failed` forever.

Teardown's first step removes the InferenceService from inside the tenant's
vCluster, via `_vc()` → `vcluster connect --print`. **That command fails when no
vCluster was ever created.** A project that failed at `namespace-pack` or
`model-seed` therefore stopped on the first teardown step and could never be
removed — and those are precisely the runs most likely to need removing.

Masked by defect 1: while the injection was also breaking teardown, the two looked
like one symptom.

Two changes, both making teardown best-effort in the way an escape hatch has to be:

- **A missing tenant cluster is success, not failure.** There being no isvc inside
  a vCluster that does not exist is the desired end state, already reached. The
  host-side mirror deletion — the one that actually releases the GPU — runs
  regardless.
- **A namespace-delete timeout is not a failure either.** A namespace with
  finalizers routinely outlasts `--timeout` while terminating perfectly well, and
  kubectl exits non-zero. The step now verifies by **polling for absence**: gone is
  gone, whatever the exit code said. It fails only if the namespace is still there
  after 180 s, which means a finalizer really is stuck.

**Verified — the exact case that was broken:**

```
THE CASE THAT WAS BROKEN: fail at the FIRST step, then Delete and start over
  POST 202
  provisioning state: failed
  namespace created?: (none — injection raised before side effects)

  --- DELETE (injection still set) ---
  DELETE 202
  state after delete: deleted
  namespace now     : GONE
```

---

## 7. A fourth case, by accident

Docker Desktop stopped in the middle of the audit. The runs that followed are not
valid injection tests, but they exercised something better than a simulation:

```
FAILED: cannot provision — nvidia.com/gpu capacity: could not be read —
The connection to the server 127.0.0.1:51264 was refused
```

`approve` refused at the precondition check, before creating anything, against a
genuinely dead cluster. The message names which precondition failed and why. Two
guards behaved correctly under a condition no test had been written for.

The same day, a real user deployment confirmed the other precondition in ordinary
use — a project selecting the Qwen model failed at `approve` with the
`hive-llama-cpp`/Step 9b message, and **no namespace was created**.

---

## 8. Why this had to run against a real cluster

Neither defect is reachable in mock mode. `MockExecutor.teardown` is three sleeps:
it has no kubeconfig to open, no namespace to wait for, and no finalizers. The
whole S1–S4 smoke suite — 36 assertions, all passing — would have gone on passing
with both defects in place.

The failure path was built against a mock in S2 and it did its job there: it
produced the progress list, the per-step logs and the retry that made these two
bugs diagnosable. But **the recovery path itself could only be tested where things
can genuinely be half-built**, and that is the argument for this audit existing
separately from the smoke suite.

---

## 9. Not covered

- **Teardown failures.** Now injectable via `teardown:<step>`, but not exercised —
  the fix removed the accidental coverage this audit had been getting.
- **Failure during a queued run.** One project at a time was tested.
- **Partial teardown.** A namespace whose finalizer is genuinely stuck reports the
  180 s failure, but nothing retries the teardown from where it stopped.
- **`approve` rejections** (reserved names, duplicates) are covered by the smoke
  suite rather than here, because they never create anything.
