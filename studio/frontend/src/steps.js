// ===========================================================================
// HIVE STUDIO — THE SIX PIPELINE STEPS, IN HUMAN LANGUAGE
// ===========================================================================
// THE ONE PLACE step ids become words. The template page pre-discloses these
// before a PI commits; the progress view names them as they run; the failure
// panel names the one that broke. All three read this map, so the six steps are
// described identically wherever they appear — a PI who read the template page
// recognises exactly the same words while watching it happen.
//
// The KEYS are the canonical ids from CLAUDE.md §6, shared with the backend, the
// registry and STUDIO_FAIL_STEP. They are translated here, never respelled.
//
// `label` is the step's name. `detail` is the user-side explanation underneath —
// which is where the plain-language obligation of invariant (e) is met, because
// two of the labels ("namespace & quota", "central KServe") are platform terms a
// PI would not otherwise know.
// ===========================================================================

// THERE IS NO STEP_ORDER IN THIS FILE, AND THAT IS THE POINT.
//
// There was one — the six ids `inference-endpoint` runs, hard-coded — and it was
// read by the template page and the wizard's review step to pre-disclose the
// pipeline before a deployment exists. It was correct while one template existed
// and silently wrong the moment a second one had eleven steps in a different
// order.
//
// ORDER IS NOW ALWAYS THE BACKEND'S, from the one function that computes it
// (pipeline.plan_steps):
//
//   before a deployment exists   GET /api/templates/{id} -> `steps`
//   after it exists              GET /api/deployments/{id} -> `steps[]`
//
// This file translates ids into words. It does not know what order they come in,
// and nothing here needs to be edited when a template changes its pipeline.

// S7 COPY PASS. Two of these labels were platform language and are now written
// from the PI's side, per invariant (e). "Creating namespace & quota" named a
// thing a PI does not have; "Deploying to central KServe" named a component they
// have never heard of. Both now say what they GET, with the real term kept in
// parentheses where it is worth being discoverable — the same
// "your private cluster (vCluster)" pattern CLAUDE.md gives as the model.
export const STEPS = {
  approve: {
    label: "Checking your request",
    detail: "Making sure the name is free and the settings are allowed.",
  },
  "namespace-pack": {
    label: "Setting aside your share of the hardware",
    detail: "A ceiling your project cannot go past, however busy it gets.",
  },
  "model-seed": {
    label: "Copying your model into project storage",
    detail: "Fetched once, from the reviewed list — never at request time.",
  },
  vcluster: {
    label: "Creating your private cluster (vCluster)",
    detail: "Your own Kubernetes control plane. You get admin access to it.",
  },
  "inference-service": {
    label: "Publishing your model to the shared platform (KServe)",
    detail: "You author it inside your own cluster; the platform runs it.",
  },
  "ready-check": {
    label: "Waiting for your endpoint to answer",
    detail: "The web address is read from the running service, never guessed.",
  },

  // ---------------------------------------------------------------------
  // RAG STACK (studio R1)
  // ---------------------------------------------------------------------
  // Same rule as above: say what the PI GETS, not what Kubernetes does. None of
  // these five names a component — there is no "Qdrant", no "Job", no
  // "Deployment" — because a PI who wanted to know those has the project page,
  // the events feed and admin access to their own cluster.
  //
  // These keys are the canonical ids from CLAUDE.md §6 and app/catalog.py, and
  // they are shared with the registry's history rows and STUDIO_FAIL_STEP.
  // Translated here, never respelled.
  "vector-db": {
    label: "Starting your vector database",
    detail: "Where your documents are stored in a form that can be searched.",
  },
  "corpus-upload": {
    label: "Uploading your documents",
    detail: "Copied into your project's own storage, and deleted with it.",
  },
  ingestion: {
    label: "Indexing your documents",
    detail: "Read, split into passages, and made searchable. The longest step.",
  },
  "rag-api": {
    label: "Starting your RAG service",
    detail: "The endpoint you ask questions of. It finds passages, then answers.",
  },
  "rag-ready-check": {
    label: "Asking a first question for you",
    detail:
      "We try one question ourselves, so yours is not the first — and so a " +
      "problem shows up here rather than when you click.",
  },
  // --- batch inference (studio B2) ----------------------------------------
  // A run's steps, in the PI's language. "Running the model over your dataset"
  // rather than "batch-run": the PI is not deploying a Job, they are waiting
  // for work to be done on their file.
  "dataset-upload": {
    label: "Uploading your dataset",
    detail: "Copied into your project once, for this run.",
  },
  "batch-run": {
    label: "Running the model over your dataset",
    detail: "One row at a time, or in batches — the progress bar shows how far.",
  },
  "collect-results": {
    label: "Collecting your results",
    detail: "Fetched out of your project so you can download them.",
  },
};

export function stepLabel(id) {
  return STEPS[id] ? STEPS[id].label : id;
}
