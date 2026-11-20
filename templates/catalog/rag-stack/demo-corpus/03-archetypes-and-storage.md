# Archetypes and storage

## The two archetypes

Archetype behaviour lives entirely in Knative annotations on the
InferenceService, plus the `minReplicas` field that KServe actually honours.

**T-AO, always-on.** `autoscaling.knative.dev/min-scale: "1"` and
`minReplicas: 1`. The service never scales to zero and never pays a cold start,
so a researcher hitting it gets a warm response at any hour. The cost is that
the GPU is held permanently whether or not anyone is using it. Tenant Inf-101,
PI A, is the always-on archetype: a long-lived LLM service.

**T-SZ, scale-to-zero.** `autoscaling.knative.dev/min-scale: "0"` and
`minReplicas: 0`, with an idle window and a scale-down delay. When idle it
releases its GPU back to the pool, and the first request after a nap waits about
fifteen seconds while the model wakes. Tenant Inf-103, PI C, is the bursty
archetype.

The annotation alone does not work. KServe derives the Knative min-scale
annotation from `spec.predictor.minReplicas` and overwrites whatever the
InferenceService annotation said. With only the annotation, a scale-to-zero
service is silently clobbered to one replica and holds a GPU forever. Both are
written: `minReplicas` is the mechanism, the annotation declares the intent.

Locally the idle window and the scale-down delay are 60 seconds so that
scale-to-zero can be observed inside a test run. In production both are 30
minutes.

## Storage

Tenants get PersistentVolumeClaims in their host namespace. Locally the
StorageClass is `standard`, kind's local-path provisioner; in production it is
WekaFS CSI with directory quotas.

Two consequences of local-path are worth knowing. Its volume binding mode is
WaitForFirstConsumer, so a claim stays Pending until a pod actually mounts it —
a Pending claim is the provisioner waiting to learn which node to use, not a
fault. And the volume is a directory on one node's disk, so ReadWriteOnce is the
only honest access mode: pods sharing a claim must land on the same node.

Model artefacts are seeded once into a `models` claim by a Job and thereafter
mounted read-only by serving pods. Models are never fetched at request time.

## URLs are read, never constructed

Because HIVE-LOCAL runs Kourier rather than Istio, KServe publishes an
InferenceService URL that carries a `-predictor` segment which production will
not have. Nothing may hard-code a hostname. The URL is always read live from the
InferenceService's `.status.url`.
