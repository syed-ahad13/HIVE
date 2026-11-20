# HIVE architecture

HIVE is a multi-tenant GPU inference platform built by the KSL AI Platforms
team. Around twenty university research groups, called PIs, are its tenants.

## One control plane per tenant

Every PI receives their own Kubernetes control plane, delivered as a vCluster
running on one shared physical host cluster. A vCluster is a full Kubernetes API
server of the tenant's own: they are cluster-admin inside it, they can inspect
and change everything in it, and nothing they do there affects another tenant.
The host cluster underneath is shared, and the platform team administers it.

In the local rehearsal there is one kind host cluster named `hive-host` and two
vClusters: `inf-101` in host namespace `p-inf-101`, and `inf-103` in host
namespace `p-inf-103`.

## Exactly one KServe

There is exactly ONE KServe installation, and it runs on the host. It is not
installed per tenant. Every tenant deploys models through that single central
KServe, which runs in Serverless mode using Knative for everything. There is no
RawDeployment mode anywhere in HIVE.

The division of labour is: the tenant AUTHORS an InferenceService inside their
own vCluster, and the one central KServe on the host RECONCILES it. That shape
is what makes a tenant feel like they have their own cluster while the platform
keeps one thing to operate and upgrade.

## How a tenant asks for a GPU

Tenants request GPUs only as `resources.limits["nvidia.com/gpu"]` on the
InferenceService. That is the entire request surface, and it is identical
locally and in production, so a tenant's manifest migrates unchanged.

Locally the GPUs are fake: an extended resource patched onto node capacity, four
per worker. No device plugin is involved and none may be introduced. Only the
scheduler accounting is real — the scheduler counts requests against advertised
capacity and refuses to overcommit — but nothing is isolated and nothing is
executed on a GPU. In production the same field is satisfied by the NVIDIA GPU
Operator with DRA on real hardware.

## Network isolation

Tenant namespaces carry a default-deny ingress NetworkPolicy. Only four sources
are allowed through: the same namespace, `kourier-system`, `knative-serving`,
and `kserve`.
