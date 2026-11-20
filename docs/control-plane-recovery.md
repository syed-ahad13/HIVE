# When the host control plane stops working

**First seen 2026-09-29.** A Studio deploy failed at `vcluster`, and deleting a
project timed out with its namespace stuck `Terminating`. Neither was a Studio
fault, and neither message said so at the time. This page is where those
messages now point.

---

## What you see

Any of these, usually several at once:

- a deploy fails at **`vcluster`**, *"did not become ready in 300s"*
- **`model-seed`** takes minutes for a file that downloads in seconds
- a delete fails at **`namespace-pack`**, with the namespace stuck `Terminating`
- inside that namespace, PVCs are still **`Bound`** and old Jobs are still
  there: nothing is being removed at all

Since this fix, Studio names the cause itself. `approve` refuses to provision:

```
[FAIL] host control plane: the host cluster's own control plane is not running:
  - kube-controller-manager (...): CrashLoopBackOff, 89 restarts; its leader
    lease was last renewed 5m08s ago
  - kube-scheduler (...): CrashLoopBackOff, 94 restarts; no leader holds its lease
```

## Confirm it (30 seconds)

```bash
kubectl get pods -n kube-system -l tier=control-plane
```

If `kube-controller-manager` or `kube-scheduler` shows **`CrashLoopBackOff`**, this
page applies. To confirm the cause:

```bash
# why it died: expect "Failed to renew lease ... context deadline exceeded"
kubectl logs -n kube-system kube-controller-manager-hive-host-control-plane --previous | grep -i lease

# the VM is swapping: SwapFree near zero, memory "some" pressure above ~10
docker exec hive-host-control-plane sh -c 'grep -E "Swap(Total|Free)" /proc/meminfo; cat /proc/pressure/memory'
```

## Why it happens

| | |
|---|---|
| 1 | The Docker Desktop VM runs out of memory and starts swapping. |
| 2 | etcd's pages are evicted and faulted back in, so every request waits on disk. Its disk is not slow — `slow fdatasync` stays at zero — but request latency reaches seconds. On 2026-09-29: median 265ms, p95 6.4s, worst 35s. |
| 3 | `kube-controller-manager` and `kube-scheduler` must renew a leader lease every 10 seconds. A renewal that waits on etcd misses the deadline, the process exits, and the kubelet restarts it. |
| 4 | Every restart re-lists the whole cluster and re-reads the binary from disk. That adds load to the thing that is already too slow, so the loop feeds itself and does not settle. |
| 5 | With those two down, nothing reconciles. `kubectl apply` succeeds, and then no pod is created, nothing is scheduled, and no namespace is emptied. |

**Why memory runs out.** Every tenant is a whole control plane. The platform alone
holds about 4GiB before any Studio project: the host `kube-apiserver` ~1.2GiB,
Calico ~0.9GiB, the two reference vClusters ~0.55GiB each, Knative + KServe
~0.4GiB. Each Studio project adds another vCluster (~0.55GiB) plus its workload;
a RAG project is ~1.1GiB. A 7.7GiB VM fits two or three projects.

**What tips it over** is a cold start: after a Docker Desktop restart every pod
starts at once. That is exactly what happened on 2026-09-29, four minutes before
the failing deploy.

## Recover

In order. Do not delete or retry until step 3 passes: both will time out again,
and the retry is what adds load.

1. **Give the VM more memory.** Docker Desktop → Settings → Resources →
   **Memory 12GB** (on a 16GB Mac), **Swap 2GB**. Apply & restart.

2. **Re-run the bring-up.** The restart wipes the fake GPU capacity, and
   `hive-up.sh` restores it:

   ```bash
   scripts/hive-up.sh --skip-serving
   ```

3. **Wait until the control plane is stable**, not merely running. Both lines
   should show `Running`, with a restart count that stops climbing over two
   minutes:

   ```bash
   kubectl get pods -n kube-system -l tier=control-plane -w
   ```

4. **Finish what failed.** Nothing is lost:
   - a failed **deploy** → *Retry* on the project page. It resumes at the
     failed step; the `vcluster` step reuses the vCluster that already exists.
   - a failed **delete** → press *Delete* again. Teardown is idempotent: it
     re-runs every step, skips what is already gone, and finishes.

A namespace that was stuck `Terminating` usually finishes on its own within a
minute of step 3. The controller-manager picks up the queued deletion.

## Prevent it

- **Keep the VM at 10GiB or more.** `scripts/hive-up.sh` now warns below that.
- **Delete projects you are finished with.** A project removed from the registry
  but still running on the cluster costs the same memory and cannot be seen in
  the console. Check with:

  ```bash
  kubectl get ns -l hive.ksl/kind=tenant
  ```

  Compare that list against the console's project list.
