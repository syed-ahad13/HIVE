// ===========================================================================
// HIVE STUDIO — UI CHECKS
// ===========================================================================
//   scripts/studio-ui-check.sh
//
// Mounts the components that carry real behaviour into a DOM and asserts what
// they render — including what happens when a disclosure is CLICKED, which is
// the whole point of two of these checks and cannot be seen from static markup.
//
// ---------------------------------------------------------------------------
// WHAT THIS IS FOR, AND WHAT IT IS NOT
// ---------------------------------------------------------------------------
// The RAG work made three claims about the console that are easy to state and
// easy to get quietly wrong:
//
//   1. the progress list renders WHATEVER PLAN the backend sends — eleven steps
//      for a RAG project, in its own order, all with human labels;
//   2. a step's log is readable even when the step SUCCEEDED, which is where the
//      ingestion step's INGEST_RESULT line lives;
//   3. the sources panel appears even when retrieval found NOTHING, because the
//      absence of sources is the evidence for the refusal.
//
// Each is asserted below against the real component. This is not a substitute
// for opening the console — it says nothing about whether any of it looks right.
// ===========================================================================

import { act } from "react";
import { createRoot } from "react-dom/client";

import { Progress, StepRow, KnowledgeBase, RunResults } from "../src/pages/DeploymentDetail.jsx";
import { ParameterField } from "../src/fields.jsx";
import Playground, { SourcesPanel } from "../src/pages/Playground.jsx";
import Wizard from "../src/pages/Wizard.jsx";
import { STEPS, stepLabel } from "../src/steps.js";

import { MemoryRouter, Route, Routes } from "react-router-dom";

// CAPTURED FROM THE RUNNING BFF, not hand-written. These are the exact bodies
// GET /api/templates/{id} and GET /api/models return, so the wizard below is
// rendering the real catalogue — including the `group`, `interface` and `steps`
// fields the console now depends on. Recapture them with:
//   curl -s localhost:8000/api/templates/rag-stack | python3 -m json.tool > ...
import inferenceTemplate from "./fixtures/template-inference-endpoint.json" with { type: "json" };
import ragTemplate from "./fixtures/template-rag-stack.json" with { type: "json" };
import batchTemplate from "./fixtures/template-batch-inference.json" with { type: "json" };
import modelsFixture from "./fixtures/models.json" with { type: "json" };
// GET /api/deployments/proj/results from a REAL batch run (2026-09-29): the
// iris classifier over 200 rows, columns chosen in feature order.
import resultsFixture from "./fixtures/results-batch.json" with { type: "json" };

let PASS = 0;
let FAIL = 0;

function check(label, condition, detail = "") {
  if (condition) {
    PASS += 1;
    console.log(`  \x1b[32mPASS\x1b[0m  ${label}`);
  } else {
    FAIL += 1;
    console.log(`  \x1b[31mFAIL\x1b[0m  ${label}`);
    if (detail) console.log(`        ${detail}`);
  }
}

/** Mount a node into a fresh container and hand back the DOM to poke at. */
function mount(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(node));
  return {
    host,
    root,
    text: () => host.textContent || "",
    // A placeholder lives in an attribute, not in textContent, so asking for
    // the compose box by its prompt is both the correct read and a stronger
    // claim than "these words appear somewhere".
    composePlaceholder: () =>
      host.querySelector(".pg__compose input")?.getAttribute("placeholder") ?? null,
  };
}

// The exact eleven-step plan the BFF computes for rag-stack, from
// pipeline.plan_steps — pasted rather than fetched so this runs with no server.
const RAG_PLAN = [
  "approve", "namespace-pack", "model-seed", "vcluster", "vector-db",
  "corpus-upload", "ingestion", "inference-service", "ready-check",
  "rag-api", "rag-ready-check",
];

const INGEST_LOG = [
  "--- ingestion started ---",
  "reading corpus from /mnt/scratch/corpus",
  "4 file(s) -> 23 chunk(s)",
  "indexed 23/23 chunks",
  'INGEST_RESULT {"files":4,"chunks":23,"collection":"ragdemo-corpus","dims":384}',
].join("\n");

function ragDeployment() {
  return {
    id: "ragdemo",
    steps: RAG_PLAN.map((stepId, i) => ({
      stepId,
      phase: "provision",
      state: "succeeded",
      durationSeconds: i + 1,
      startedAt: null,
      log: stepId === "ingestion" ? INGEST_LOG : `--- ${stepId} ---\nfine`,
    })),
  };
}

// ---------------------------------------------------------------------------

console.log("\n==> the progress list renders the plan it is given\n");

{
  const { text, host } = mount(
    <Progress d={ragDeployment()} tick={0} onRetry={() => {}} onDelete={() => {}} busy={false} />,
  );

  const rows = host.querySelectorAll(".prow");
  check("eleven rows for an eleven-step plan", rows.length === 11, `got ${rows.length}`);

  const missing = RAG_PLAN.filter((id) => !text().includes(stepLabel(id)));
  check("every step shows its human label, not its id", missing.length === 0, `missing: ${missing}`);

  // If any id fell through the label map, stepLabel returns the id itself and
  // the raw hyphenated string would appear on screen.
  const rawIds = RAG_PLAN.filter((id) => !STEPS[id]);
  check("every step id is in the one label map", rawIds.length === 0, `unlabelled: ${rawIds}`);

  check(
    "the five RAG steps are labelled in the PI's language",
    text().includes("Starting your vector database") &&
      text().includes("Uploading your documents") &&
      text().includes("Indexing your documents") &&
      text().includes("Starting your RAG service") &&
      text().includes("Asking a first question for you"),
  );

  check("the count reflects the whole plan", text().includes("11 of 11 done"), text().slice(0, 120));

  // Order matters: rag-api depends on what ready-check reads, so a list that
  // rendered them the other way round would mis-describe the pipeline.
  const labels = [...rows].map((r) => r.querySelector("b")?.textContent);
  check(
    "ready-check is shown BEFORE rag-api, as the pipeline runs them",
    labels.indexOf(stepLabel("ready-check")) < labels.indexOf(stepLabel("rag-api")),
    labels.join(" | "),
  );
}

console.log("\n==> a step's log is readable, not only a failed one\n");

{
  const step = ragDeployment().steps.find((s) => s.stepId === "ingestion");
  const { host, text } = mount(<StepRow step={step} tick={0} />);

  const toggle = [...host.querySelectorAll("button")].find((b) =>
    (b.textContent || "").includes("What the step reported"),
  );
  check("a SUCCEEDED step with a log offers the disclosure", Boolean(toggle));
  check("its log is collapsed to begin with", !text().includes("INGEST_RESULT"));

  act(() => toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));

  check("clicking it reveals the log tail", text().includes("indexed 23/23 chunks"));
  check(
    "the INGEST_RESULT line is visible when expanded",
    text().includes('INGEST_RESULT {"files":4,"chunks":23'),
    text().slice(-160),
  );
  check("the log is rendered as preformatted text", Boolean(host.querySelector("pre.yaml--log")));
}

{
  // A failed step opens itself — that log is why the PI is looking at the page.
  const step = {
    stepId: "ingestion",
    phase: "provision",
    state: "failed",
    log: "ERROR: no indexable content in /mnt/scratch/corpus",
    durationSeconds: 3,
  };
  const { text } = mount(<StepRow step={step} tick={0} />);
  check("a FAILED step shows its log without a click", text().includes("no indexable content"));
}

{
  const step = { stepId: "vcluster", phase: "provision", state: "pending", log: "" };
  const { host } = mount(<StepRow step={step} tick={0} />);
  const toggle = [...host.querySelectorAll("button")].find((b) =>
    (b.textContent || "").includes("What the step reported"),
  );
  check("a step with NO log offers no empty disclosure", !toggle);
}

console.log("\n==> the sources panel\n");

{
  const sources = [
    { chunk_id: "b.md#1", source: "b.md", score: 0.41, text: "weakly related passage" },
    { chunk_id: "a.md#0", source: "a.md", score: 0.88, text: "the strongest passage" },
    { chunk_id: "c.md#2", source: "c.md", score: 0.63, text: "middling passage" },
  ];
  const { host, text } = mount(<SourcesPanel sources={sources} />);

  const files = [...host.querySelectorAll(".src__file")].map((n) => n.textContent);
  check("sources are ordered by score, best first", String(files) === String(["a.md", "c.md", "b.md"]), String(files));

  check("each row names its file and its chunk id", text().includes("a.md") && text().includes("a.md#0"));
  check("each row shows the similarity score", text().includes("0.880") && text().includes("0.410"));
  check("the chunk text is collapsed to begin with", !text().includes("the strongest passage"));

  const first = host.querySelector(".src__head");
  act(() => first.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  check("expanding a source shows the passage itself", text().includes("the strongest passage"));

  // Bands, not a gradient — a strong match and a weak one must be
  // distinguishable at a glance, and the score is still printed either way.
  check("a strong match is marked", Boolean(host.querySelector(".src--strong")));
  check("a weak match is marked", Boolean(host.querySelector(".src--weak")));
}

{
  // THE CASE THAT MATTERS MOST. When retrieval finds nothing the service answers
  // honestly without asking the model, and the empty panel is the evidence.
  const { text } = mount(<SourcesPanel sources={[]} />);
  check("an EMPTY result still renders the panel", text().includes("Sources used"));
  check(
    "...and says why the answer says what it says",
    text().includes("Nothing in your documents matched"),
    text(),
  );
}

console.log("\n==> the knowledge base card\n");

/**
 * A REAL-MODE deployment. `live` being present is what tells the card there is a
 * cluster behind it — its absence means mock mode, and the card says so rather
 * than presenting canned figures as live ones.
 */
function realDeployment(rag) {
  return {
    id: "ragdemo",
    params: { topK: 3 },
    outputs: { collectionName: "ragdemo-corpus", indexedChunks: 23, indexedFiles: 4 },
    live: { ready: true, rag },
  };
}

const HEALTHY_RAG = {
  collection: "ragdemo-corpus",
  vectorDb: { exists: true, ready: 1, desired: 1 },
  ragApi: { exists: true, ready: 1, desired: 1 },
  livePoints: 23,
  indexedChunks: 23,
  indexedFiles: 4,
};

{
  const { text } = mount(<KnowledgeBase d={realDeployment(HEALTHY_RAG)} rag={HEALTHY_RAG} />);
  check("shows the collection name", text().includes("ragdemo-corpus"));
  check("shows documents and passages indexed", text().includes("4") && text().includes("23"));
  check("shows top-k", text().includes("Passages used per question"));
  check("both services report running", (text().match(/running/g) || []).length >= 2, text());
  check("shows the live count beside the reported one", text().includes("Passages in the index now"));
}

{
  // `null` means "could not be read" and 0 means "the index is empty". Collapsing
  // the two would report a Qdrant we could not reach as an empty index.
  const unreadRag = { ...HEALTHY_RAG, livePoints: null };
  const unread = mount(<KnowledgeBase d={realDeployment(unreadRag)} rag={unreadRag} />);
  check("an unreadable count says so rather than showing zero", unread.text().includes("could not be read"));
  check("...and does not claim the index is empty", !unread.text().includes("Your index is empty"));

  const emptyRag = { ...HEALTHY_RAG, livePoints: 0 };
  const empty = mount(<KnowledgeBase d={realDeployment(emptyRag)} rag={emptyRag} />);
  check("a genuinely empty index is called out", empty.text().includes("Your index is empty"));
  check(
    "a live count that disagrees with the run is surfaced, not hidden",
    empty.text().includes("has changed since it was built"),
  );

  const downRag = {
    collection: "c",
    vectorDb: { exists: true, ready: 0, desired: 1 },
    ragApi: { ready: 0, desired: 1 },
    livePoints: null,
  };
  const down = mount(<KnowledgeBase d={realDeployment(downRag)} rag={downRag} />);
  check("a stopped search index is reported as starting, not running", down.text().includes("starting"));
}

{
  // MOCK MODE. The BFF returns no `live` block at all, so nothing on this card
  // describes anything that exists — and it has to say so. A rehearsal that
  // presented canned figures as live ones would teach the audience the opposite
  // of what it is demonstrating (invariant (e)).
  const mock = {
    id: "ragdemo",
    params: { topK: 3 },
    outputs: { collectionName: "ragdemo-corpus", indexedChunks: 37, indexedFiles: 4 },
    live: null,
  };
  const { text } = mount(<KnowledgeBase d={mock} rag={undefined} />);
  check("mock mode still shows what the run reported", text().includes("ragdemo-corpus") && text().includes("37"));
  check("mock mode says nothing was really indexed", text().includes("Nothing was really indexed"));
  check("mock mode shows no live count it cannot have", !text().includes("Passages in the index now"));
  check("mock mode shows no service pills for services that do not exist", !text().includes("running"));
}

console.log("\n==> the playground picks its mode, and refuses when it should\n");

const RAG_TEMPLATE = { spec: { interface: "rag" } };
const CHAT_MODEL = { id: "qwen", interface: "chat", sampleInput: {} };

function readyRagDeployment(overrides = {}) {
  const d = ragDeployment();
  return {
    ...d,
    derivedStatus: "ready",
    params: { topK: 3 },
    live: {
      endpointUrl: "http://ragdemo-predictor.p-ragdemo.hive.local",
      rag: {
        collection: "ragdemo-corpus",
        vectorDb: { exists: true, ready: 1, desired: 1 },
        ragApi: { exists: true, ready: 1, desired: 1 },
        livePoints: 23,
      },
    },
    ...overrides,
  };
}

{
  // THE TEMPLATE OVERRIDES THE MODEL. The chat model underneath is still
  // `interface: chat`; honouring that would give a PI a chat box wired to a
  // service that does not accept chat messages.
  const pg = mount(
    <Playground deployment={readyRagDeployment()} model={CHAT_MODEL} template={RAG_TEMPLATE} />,
  );
  const { text } = pg;
  check(
    "a rag template renders the question box, not the chat box",
    pg.composePlaceholder() === "Ask something about your documents…",
    String(pg.composePlaceholder()),
  );
  check(
    "...and says what it will do with the answer",
    text().includes("shows the passages it was built from"),
  );
  check("...and states how many passages each question uses", text().includes("the 3 closest passages"));
}

{
  // Without the template it falls back to the model's own interface, which is
  // what every project deployed before templates could declare one relies on.
  const chatOnly = {
    id: "demo",
    derivedStatus: "ready",
    steps: [],
    params: {},
    live: { endpointUrl: "http://demo.hive.local" },
  };
  const pg = mount(<Playground deployment={chatOnly} model={CHAT_MODEL} template={null} />);
  check(
    "with no template, the model's interface still decides",
    pg.composePlaceholder() === "Ask your model something…",
    String(pg.composePlaceholder()),
  );
}

{
  // The exact copy the task asked for, produced generically: the title is the
  // step's own label from the one map.
  const midIngestion = readyRagDeployment();
  midIngestion.steps = midIngestion.steps.map((s) =>
    s.stepId === "ingestion" ? { ...s, state: "running" } : s,
  );
  // Steps after it have not had their turn.
  const at = midIngestion.steps.findIndex((s) => s.stepId === "ingestion");
  midIngestion.steps = midIngestion.steps.map((s, i) =>
    i > at ? { ...s, state: "pending" } : s,
  );
  const pending = mount(
    <Playground deployment={midIngestion} model={CHAT_MODEL} template={RAG_TEMPLATE} />,
  );
  check(
    "mid-ingestion, the playground is disabled with the step's own words",
    pending.text().includes("Indexing your documents — this takes a moment"),
    pending.text(),
  );
  check("...and there is nowhere to type a question", pending.composePlaceholder() === null);
}

{
  const failedIngestion = readyRagDeployment();
  failedIngestion.steps = failedIngestion.steps.map((s) =>
    s.stepId === "ingestion" ? { ...s, state: "failed" } : s,
  );
  const { text } = mount(
    <Playground deployment={failedIngestion} model={CHAT_MODEL} template={RAG_TEMPLATE} />,
  );
  check("a failed step says so rather than saying 'wait'", text().includes("did not finish"));
  check("...and points at where the reason is", text().includes("Overview tab shows what the step reported"));
}

{
  // The vector database is an ordinary Deployment that never scales to zero, so
  // "not running" is always a fault — never the project resting.
  const dbDown = readyRagDeployment();
  dbDown.live.rag.vectorDb = { exists: true, ready: 0, desired: 1 };
  const down = mount(
    <Playground deployment={dbDown} model={CHAT_MODEL} template={RAG_TEMPLATE} />,
  );
  check("a stopped search index raises a banner", down.text().includes("Your search index is not available"));
  check(
    "...and says explicitly that this is not the project sleeping",
    down.text().includes("does not sleep when idle"),
  );
  check(
    "...while still offering the question box, so the failure is visible in place",
    down.composePlaceholder() === "Ask something about your documents…",
  );
}

console.log("\n==> the wizard renders whatever the template asks\n");

/**
 * Mount the real Wizard against a stubbed BFF.
 *
 * `fetch` is replaced rather than the components mocked, so everything from
 * useFetch down is the production path: the same request, the same ApiError
 * handling, the same resolved-enum shapes.
 */
function mountWizard(templateId, template) {
  const previous = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const path = String(url);
    // The wizard reads /api/health too (for the derived-hostname preview), so
    // the stub has to answer it. Returning the template for every path used to
    // be harmless and became a lie the moment a component asked a second
    // question.
    const body = path.includes("/api/health")
      ? { ok: true, executor: "mock" }
      : path.includes("/api/models")
        ? modelsFixture
        : template;
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  };
  const mounted = mount(
    <MemoryRouter initialEntries={[`/templates/${templateId}/deploy`]}>
      <Routes>
        <Route path="/templates/:id/deploy" element={<Wizard />} />
      </Routes>
    </MemoryRouter>,
  );
  // The two fetches resolve on microtasks; flush them inside act so React has
  // committed the result before anything is asserted.
  return { ...mounted, restore: () => (globalThis.fetch = previous) };
}

async function settle() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

{
  const w = mountWizard("rag-stack", ragTemplate);
  await settle();

  const labelsOf = () => [...w.host.querySelectorAll(".field > label")].map((n) => n.textContent);

  // BASICS: exactly the three parameters rag-stack marks `group: basics`, in the
  // order the template declares them.
  check(
    "Basics shows the template's own basics questions, in order",
    String(labelsOf()) === String(["Project name", "Your documents", "How long do you need it?"]),
    String(labelsOf()),
  );

  check(
    "the upload parameter renders a drop zone, not a text box",
    Boolean(w.host.querySelector(".drop")) &&
      w.host.querySelector('input[type="file"]')?.multiple === true,
  );
  check(
    "the drop zone accepts only what can be indexed",
    w.host.querySelector('input[type="file"]')?.getAttribute("accept")?.includes(".md"),
  );
  check(
    "the fallback is stated before it is needed",
    w.text().includes("No files? We’ll index the bundled HIVE documents so you can try it immediately.") ||
      w.text().includes("No files? We'll index the bundled HIVE documents so you can try it immediately."),
    w.text().slice(0, 400),
  );

  // Move to Resources. The project name is empty, so Continue must refuse.
  const continueBtn = () =>
    [...w.host.querySelectorAll("button")].find((b) => b.textContent === "Continue");
  act(() => continueBtn().dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  check("Continue refuses while a required answer is missing", w.text().includes("is needed"));

  const nameInput = w.host.querySelector("#projectName");
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value",
    ).set;
    setter.call(nameInput, "reefdemo");
    nameInput.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  check("typing a name shows what it becomes on the cluster", w.text().includes("p-reefdemo"));

  act(() => continueBtn().dispatchEvent(new window.MouseEvent("click", { bubbles: true })));

  // RESOURCES: the other six, again in the template's order.
  check(
    "Resources shows the remaining questions, in order",
    String(labelsOf()) ===
      String([
        "Model that answers the questions",
        "How your documents are indexed",
        "Search index",
        "How many passages to use",
        "Availability",
        "Size",
      ]),
    String(labelsOf()),
  );

  check(
    "vectorDb is a read-only statement, not a dropdown of one",
    Boolean(w.host.querySelector(".readonly")) &&
      w.text().includes("Qdrant (provisioned inside your project)") &&
      !w.host.querySelector("#vectorDb select"),
  );
  check(
    "topK is a bounded number with the help line",
    w.host.querySelector("#topK")?.getAttribute("type") === "number" &&
      w.host.querySelector("#topK")?.getAttribute("min") === "1" &&
      w.host.querySelector("#topK")?.getAttribute("max") === "8" &&
      w.text().includes("How many passages are retrieved per question."),
  );
  check("topK is pre-filled with the template's default", w.host.querySelector("#topK")?.value === "3");
  check(
    "availability renders as comparable cards, from the catalogue's own copy",
    w.host.querySelectorAll(".choice").length === 2 &&
      w.text().includes("Frees the GPU when nobody is asking"),
  );
  check(
    "the two model enums offer only what fits their slot",
    [...w.host.querySelectorAll("#baseModel option")].some((o) => o.textContent.includes("Qwen")) &&
      [...w.host.querySelectorAll("#embeddingModel option")].some((o) =>
        o.textContent.includes("BGE"),
      ) &&
      ![...w.host.querySelectorAll("#embeddingModel option")].some((o) =>
        o.textContent.includes("Qwen"),
      ),
  );

  w.restore();
}

{
  // THE REGRESSION CHECK. The same generic renderer must produce the original
  // template's original form — five questions, split two and three.
  const w = mountWizard("inference-endpoint", inferenceTemplate);
  await settle();
  const labelsOf = () => [...w.host.querySelectorAll(".field > label")].map((n) => n.textContent);

  check(
    "inference-endpoint's Basics is unchanged",
    String(labelsOf()) === String(["Project name", "How long do you need it?"]),
    String(labelsOf()),
  );
  check("durationDays keeps its default", w.host.querySelector("#durationDays")?.value === "30");
  check("it asks for no documents", !w.host.querySelector(".drop"));

  w.restore();
}

console.log("\n==> mode-specific copy tells the truth in both modes\n");

{
  // The console must not claim "nothing is really created" while it is creating
  // real namespaces, vClusters and served models — and must still say it when
  // that is true. Both directions are asserted, because a fix that silenced the
  // rehearsal warning everywhere would break invariant (e) rather than serve it.
  const { __resetExecutorMode } = await import("../src/components.jsx");
  const TemplateDetail = (await import("../src/pages/TemplateDetail.jsx")).default;

  async function mountTemplatePage(executor) {
    // Start a fresh "tab". The mode cache is per-tab and correct as such; a
    // test that renders both modes has to say it is starting over.
    __resetExecutorMode();
    const previous = globalThis.fetch;
    globalThis.fetch = async (url) => ({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify(String(url).includes("/api/health") ? { ok: true, executor } : ragTemplate),
    });
    const mounted = mount(
      <MemoryRouter initialEntries={["/templates/rag-stack"]}>
        <Routes>
          <Route path="/templates/:id" element={<TemplateDetail />} />
        </Routes>
      </MemoryRouter>,
    );
    await settle();
    await settle();
    globalThis.fetch = previous;
    return mounted;
  }

  const real = await mountTemplatePage("real");
  check(
    "real mode does NOT claim nothing is created",
    !real.text().includes("Nothing is really created") &&
      !real.text().includes("pretend web address"),
    real.text().slice(0, 200),
  );
  check(
    "real mode says what it actually does",
    real.text().includes("create real infrastructure on the cluster"),
  );
  check(
    "real mode does not promise a timing from the other mode",
    !real.text().includes("about 25 seconds") && !real.text().includes("about a minute"),
  );
  check(
    "the GPU deviation is still stated — it is true in BOTH modes",
    real.text().includes("GPUs are") && real.text().includes("simulated"),
  );

  // THE OTHER DIRECTION, which matters just as much. A "fix" that deleted the
  // rehearsal warning everywhere would pass every check above and break
  // invariant (e): a mock run that does not announce itself teaches the
  // audience that Studio created something it did not.
  const mocked = await mountTemplatePage("mock");
  check(
    "mock mode STILL says nothing is really created",
    mocked.text().includes("Nothing is really created"),
  );
  check(
    "mock mode does not claim to create real infrastructure",
    !mocked.text().includes("create real infrastructure on the cluster"),
  );
  __resetExecutorMode();
}

// ===========================================================================
// THE COLUMN PICKER
// ===========================================================================
// A `multiselect` whose choices come from the PI's OWN FILE rather than from the
// catalogue. batch-inference's `inputFields` is the only one, and it is the one
// question in the console whose options cannot be written down in advance.
//
// THIS EXISTS BECAUSE IT SILENTLY DID NOT WORK. The template declared the
// parameter, the field could render it, the backend already detected the
// columns and returned them on the upload manifest — and the wizard passed no
// options down, so the field showed its "nothing to offer" state forever. The
// question is REQUIRED, so a batch deployment could not be completed at all.
// Every layer was right on its own; the one missing wire was invisible to a
// check that only looked at layers.
console.log("\n==> the column picker offers the dataset's own columns\n");

const IRIS_COLUMNS = ["sepal_length", "sepal_width", "petal_length", "petal_width"];

function inputFieldsParam() {
  return batchTemplate.spec.parameters.find((p) => p.name === "inputFields");
}

{
  const param = inputFieldsParam();

  check(
    "the catalogue really does declare no options for it",
    !!param && !(param.values || []).length && !param.source,
    JSON.stringify(param?.values),
  );

  // --- nothing read yet -------------------------------------------------
  const empty = mount(
    <ParameterField param={param} value={[]} onChange={() => {}} options={[]} />,
  );
  check(
    "with no columns it explains what to do, and does not say 'above'",
    empty.text().includes("could not read any columns") && !empty.text().includes("above"),
    empty.text(),
  );
  check("with no columns there is nothing to click", !empty.host.querySelector(".multi__opt"));

  // --- the dataset's columns, handed in ---------------------------------
  let chosen = [];
  const picked = mount(
    <ParameterField
      param={param}
      value={chosen}
      onChange={(v) => (chosen = v)}
      options={IRIS_COLUMNS}
    />,
  );
  const buttons = [...picked.host.querySelectorAll(".multi__opt")];
  check(
    "every column in the file is offered",
    String(buttons.map((b) => b.textContent)) === String(IRIS_COLUMNS),
    String(buttons.map((b) => b.textContent)),
  );

  // ORDER IS THE ANSWER, not a detail. These four columns ARE the feature
  // vector a classifier receives, so picking petal before sepal is a different
  // question — which is why this is click-ordered chips and not <select
  // multiple>. Asserted by clicking out of order on purpose.
  act(() => buttons[2].dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  check(
    "clicking a column selects it",
    String(chosen) === String(["petal_length"]),
    String(chosen),
  );

  const second = mount(
    <ParameterField
      param={param}
      value={["petal_length"]}
      onChange={(v) => (chosen = v)}
      options={IRIS_COLUMNS}
    />,
  );
  const again = [...second.host.querySelectorAll(".multi__opt")];
  act(() => again[0].dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  check(
    "a second click APPENDS, so the order is the click order",
    String(chosen) === String(["petal_length", "sepal_length"]),
    String(chosen),
  );
  check(
    "the chosen column carries its position",
    again[0].closest(".field").textContent.includes("1"),
  );
}

// ===========================================================================
// THE BUNDLED SAMPLE IS DESCRIBED, NOT IMPLIED
// ===========================================================================
// The upload is OPTIONAL and the template promises a sample stands in. That
// promise is only keepable if the console can see inside the sample, because
// the next screen asks about its columns — so the BFF ships each sample's
// preview with the template, keyed by model interface.
{
  const samples = batchTemplate.sampleDatasets || {};
  check(
    "the template carries a sample for each interface it can run",
    Boolean(samples["predict-v1"] && samples.chat),
    String(Object.keys(samples)),
  );
  check(
    "the classifier's sample is the iris file, with its four feature columns",
    samples["predict-v1"]?.file === "iris-5000.csv" &&
      String(samples["predict-v1"].preview.columns) === String(IRIS_COLUMNS),
    JSON.stringify(samples["predict-v1"]?.preview?.columns),
  );
  check(
    "the chat sample is prose, not features",
    String(samples.chat?.preview?.columns) === String(["id", "text"]),
    JSON.stringify(samples.chat?.preview?.columns),
  );
  // The templates that ship no samples must carry no key at all. An empty
  // object would read as "this one has samples and they are broken".
  check(
    "templates without samples say nothing about samples",
    !("sampleDatasets" in ragTemplate) && !("sampleDatasets" in inferenceTemplate),
  );
}

{
  // The wizard still renders the batch template's own first screen, and the
  // drop zone advertises the template's OWN file types rather than the RAG
  // template's.
  const w = mountWizard("batch-inference", batchTemplate);
  await settle();
  const labelsOf = () => [...w.host.querySelectorAll(".field > label")].map((n) => n.textContent);
  check(
    "batch Basics asks for a name, a dataset and the two run clocks",
    String(labelsOf()) ===
      String(["Project name", "Your dataset", "Give up after", "Keep the results for"]),
    String(labelsOf()),
  );
  check(
    "the drop zone accepts datasets, not documents",
    w.host.querySelector(".drop input[type=file]")?.getAttribute("accept") === ".csv,.jsonl",
    w.host.querySelector(".drop input[type=file]")?.getAttribute("accept"),
  );
  w.restore();
}

// ===========================================================================
// THE WIRE ITSELF, END TO END THROUGH THE REAL WIZARD
// ===========================================================================
// Everything above tests a component that was HANDED its options. The bug was
// that nothing handed them over: the template declared the question, the field
// could render it, the BFF had already read the columns — and the wizard passed
// no `options` down, so the field showed its empty state forever.
//
// A check per layer would have passed throughout. So this one drives the actual
// wizard the way a PI does: type a project name, continue, choose a model, and
// look at what the column question is offering. It fails if any single link in
// template -> sampleDatasets -> chosenModel -> dataset -> optionsFor ->
// ParameterField is broken, which is the only way to catch a missing wire.
console.log("\n==> the wizard hands the dataset's columns to the question\n");

/** Type into a controlled React input the way a keystroke would. */
function type(input, value) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  ).set;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}

function clickText(host, label) {
  const button = [...host.querySelectorAll("button")].find(
    (b) => b.textContent.trim() === label,
  );
  act(() => button.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  return Boolean(button);
}

{
  const w = mountWizard("batch-inference", batchTemplate);
  await settle();

  // --- Basics: a name, and no file, which is the advertised path ----------
  type(w.host.querySelector("#projectName"), "irisrun");
  check("the derived names appear once the project name is valid",
        w.text().includes("p-irisrun"), w.text().slice(0, 120));
  check("Continue moves to the Resources screen", clickText(w.host, "Continue"));
  await settle();

  const columnsField = () =>
    [...w.host.querySelectorAll(".field")].find((f) =>
      f.textContent.includes("Which columns the model reads"),
    );

  check(
    "before a model is chosen there is nothing to offer, and it says so",
    columnsField()?.textContent.includes("could not read any columns"),
    columnsField()?.textContent?.slice(0, 120),
  );

  // --- choose the classifier ----------------------------------------------
  // WHICH SAMPLE STANDS IN DEPENDS ON THE MODEL, so this is the moment the
  // columns can first be known without an upload.
  const select = w.host.querySelector("#model");
  const setSelect = Object.getOwnPropertyDescriptor(
    window.HTMLSelectElement.prototype,
    "value",
  ).set;
  act(() => {
    setSelect.call(select, "sklearn-iris");
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
  });
  await settle();

  const offered = [...(columnsField()?.querySelectorAll(".multi__opt") || [])].map(
    (b) => b.textContent,
  );
  check(
    "choosing the classifier offers the iris sample's four feature columns",
    String(offered) === String(IRIS_COLUMNS),
    String(offered),
  );

  // --- and picking them is a real answer -----------------------------------
  const opts = [...columnsField().querySelectorAll(".multi__opt")];
  for (const opt of opts) {
    act(() => opt.dispatchEvent(new window.MouseEvent("click", { bubbles: true })));
  }
  await settle();
  check(
    "all four can be chosen, and the order is shown back",
    columnsField().textContent.includes("Used in this order: " + IRIS_COLUMNS.join(", ")),
    columnsField().textContent.slice(-180),
  );

  w.restore();
}

// ===========================================================================
// A BATCH RUN SHOWS ITS PREDICTIONS
// ===========================================================================
// The run succeeded and wrote 200 predictions, and the project page showed none
// of them — it offered "Your endpoint: No endpoint yet" instead, because it only
// knew how to describe a service. These checks mount the results view against a
// real run's output and assert the thing a PI came for: the answers, readable.
console.log("\n==> a finished batch run shows its predictions\n");

{
  const iris = modelsFixture.find((m) => m.id === "sklearn-iris");
  const run = {
    id: "proj",
    state: "completed",
    params: { inputFields: ["sepal_length", "sepal_width", "petal_length", "petal_width"] },
  };
  const r = mount(<RunResults d={run} model={iris} mode="real" results={resultsFixture} />);
  const headers = [...r.host.querySelectorAll("thead th")].map((n) => n.textContent);

  check(
    "the table's columns are the ones chosen, in the order chosen, then the prediction",
    String(headers) ===
      String(["#", "sepal_length", "sepal_width", "petal_length", "petal_width", "Prediction"]),
    String(headers),
  );
  check("it shows the rows it was given", r.host.querySelectorAll("tbody tr").length === 50);

  const first = [...r.host.querySelectorAll("tbody tr")[0].querySelectorAll("td")].map(
    (n) => n.textContent,
  );
  check(
    "a row reads as its inputs and a NAMED class, with the raw value kept beside it",
    String(first) === String(["1", "4.9", "3.5", "1.4", "0.1", "setosa 0"]),
    String(first),
  );
  check(
    "the totals come from the driver's own summary, not from counting rows",
    r.text().includes("200") && r.text().includes("The first 50 of 200 rows"),
    r.text().slice(0, 200),
  );
  const links = [...r.host.querySelectorAll(".rundl a")].map((a) => a.getAttribute("href"));
  check(
    "every artifact is a download link to the BFF",
    links.length === 3 && links.every((h) => h.startsWith("/api/deployments/proj/artifacts/")),
    String(links),
  );
  check(
    "a run with no failures shows no failure panel",
    !r.host.querySelector(".state--error"),
  );
}

{
  // A mock run computed nothing, and must SAY so rather than show an empty table
  // that looks like a model that answered nothing.
  const r = mount(
    <RunResults
      d={{ id: "m", state: "completed", params: {} }}
      model={null}
      mode="mock"
      results={{ rows: [], errors: [], artifacts: [], summary: null }}
    />,
  );
  check(
    "a mock run says it was a rehearsal, not that there were no answers",
    r.text().includes("rehearsal") && !r.host.querySelector("table"),
    r.text().slice(0, 160),
  );
}

console.log(`\n==> ${PASS} passed, ${FAIL} failed\n`);
if (FAIL) process.exitCode = 1;
