# Tenancy, ceilings and lifetimes

## Ceilings are enforced at admission time

A tenant's ceiling is a ResourceQuota in that tenant's HOST namespace — the
namespace the PI cannot reach. It is enforced at admission time, by the API
server, which refuses to create an over-quota pod at all. Nothing over-quota
ever runs, not even briefly. HIVE does not reconcile usage after the fact and
then take things away.

Two tiers exist. Medium is one GPU and Small is one GPU on the local rehearsal
cluster; they are still modelled separately because they diverge in production.

The GPU quota is deliberately set to the tier plus one. Knative's validating
webhook dry-run-creates a sample pod to validate every Revision template,
requesting the same resources as the real pod, so a ceiling of exactly one can
never be satisfied while the real pod holds it. The InferenceService then stays
at Ready=False forever even though its pod is running. Raising the quota by one
fixes it. The consequence, which is written down rather than hidden, is that the
enforced concurrent ceiling is two, so the quota alone no longer proves the
tier.

A ResourceQuota that limits `limits.cpu` and `limits.memory` obliges every
container in the namespace to state both. So every tenant namespace also ships a
LimitRange with defaults. Without it, everything the PI did not write is
rejected: the vCluster control-plane pod, Knative's queue-proxy sidecar, and
KServe's storage-initializer.

## Lifetimes

A tenant's lifetime is governed by the annotation `hive.ksl/expiry` on the
tenant namespace, written in RFC3339 UTC. A host CronJob reads that annotation
and drives the project through four states: Active, then Draining, then
Suspended, then Deleted.

The clock starts when the project is created and it never pauses. A project that
sleeps for its entire allocation still expires on schedule — sleeping is a
cost-saving behaviour, not a suspension of the lifetime. When the allocation runs
out the project is removed automatically, along with its storage, so nothing is
left holding hardware after the researcher has finished with it.

Extending a project is a decision about scarce shared hardware, so it is not
something a PI can grant themselves. The request is recorded and routed to the
Research Committee.
