// ===========================================================================
// HIVE STUDIO — TIER CEILINGS
// ===========================================================================
// ⚠ THE ONLY DUPLICATED GOVERNANCE DATA IN THE FRONTEND.
//
// These numbers mirror templates/catalog/inference-endpoint/resources/
// namespace-pack.yaml.tmpl. They are here because the BFF exposes no endpoint
// answering "what does Small get?" — the right fix, and a later step's work.
// Until then, changing the fragment means changing this file too.
//
// THE GPU FIGURE IS THE TIER (1), NOT THE QUOTA IN THE FRAGMENT (2). The extra
// GPU is validation headroom for Knative's admission webhook, recorded in
// DEVIATIONS.md — a platform implementation detail, not capacity a PI is being
// offered. Showing 2 would promise something the tier does not grant.
// ===========================================================================

export const TIERS = {
  small: {
    label: "Small",
    gpu: "1 GPU",
    cpu: "8 cores",
    memory: "16 GB",
    storage: "12 GB",
    services: "Up to 2 models at once",
  },
  medium: {
    label: "Medium",
    gpu: "1 GPU",
    cpu: "8 cores",
    memory: "16 GB",
    storage: "12 GB",
    services: "Up to 2 models at once",
  },
};

export const TIER_ROWS = [
  ["GPUs", "gpu"],
  ["CPU", "cpu"],
  ["Memory", "memory"],
  ["Storage", "storage"],
  ["Models", "services"],
];
