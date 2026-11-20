// ===========================================================================
// HIVE STUDIO — THE PROJECT PAGE
// ===========================================================================
// Four tabs: Overview, Playground, Events, Settings.
//
// ---------------------------------------------------------------------------
// EVERYTHING ON THIS PAGE IS READ LIVE
// ---------------------------------------------------------------------------
// Invariant (b). The status pill, the replica count, the endpoint, the quota
// bars and the days remaining all come from `live`, which the BFF reads from the
// cluster on every request. None of it is the registry's memory of a finished
// run. That is what lets this page say "Sleeping" the moment the last pod goes
// away — the single most important thing it has to get right, because a console
// that showed "Ready" for a project holding no GPU would be teaching the
// audience the opposite of how the archetype works.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { del, get, patch, post } from "../api.js";
import { AmberBanner, ErrorState, Loading, StatusPill, useExecutorMode } from "../components.jsx";
import { humanBytes } from "../fields.jsx";
import Playground from "./Playground.jsx";
import { STEPS, stepLabel } from "../steps.js";

const TABS = ["Overview", "Playground", "Events", "Settings"];

// Poll fast while something is changing, slowly when it is not. A settled
// project still needs watching — it can fall asleep while the page is open, and
// that transition is the thing worth seeing — but every two seconds forever is a
// request per second per open tab against a shared cluster.
const POLL_ACTIVE = 2000;
const POLL_SETTLED = 8000;

function secondsSince(iso) {
  if (!iso) return 0;
  return Math.max(0, Math.round((Date.now() - new Date(iso.replace("Z", "+00:00")).getTime()) / 1000));
}

// --- small pieces -----------------------------------------------------------

function Copy({ text, label = "Copy" }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn--ghost btn--sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1600);
        } catch {
          // Clipboard access is refused in some contexts. The text is on screen
          // and selectable, so this is a convenience failing, not a feature.
          setDone(false);
        }
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

/** Parse a Kubernetes quantity into a comparable number. */
function quantity(v) {
  if (v == null) return 0;
  const s = String(v);
  const m = s.match(/^(\d+(?:\.\d+)?)([a-zA-Z]*)$/);
  if (!m) return 0;
  const n = parseFloat(m[1]);
  const unit = m[2];
  const scale = {
    "": 1, m: 0.001,
    Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4,
    K: 1e3, M: 1e6, G: 1e9, T: 1e12,
  };
  return n * (scale[unit] ?? 1);
}

const QUOTA_ROWS = [
  ["GPUs", "requests.nvidia.com/gpu"],
  ["CPU", "limits.cpu"],
  ["Memory", "limits.memory"],
  ["Storage", "requests.storage"],
];

function QuotaBars({ quota }) {
  if (!quota) return <p className="small muted">No quota reported for this project.</p>;
  return (
    <div className="bars">
      {QUOTA_ROWS.map(([label, key]) => {
        const used = quantity(quota.used?.[key]);
        const hard = quantity(quota.hard?.[key]);
        const pct = hard > 0 ? Math.min(100, (used / hard) * 100) : 0;
        return (
          <div className="bar" key={key}>
            <div className="bar__head">
              <span>{label}</span>
              <span className="bar__num">
                {quota.used?.[key] ?? "0"} / {quota.hard?.[key] ?? "—"}
              </span>
            </div>
            <div className="bar__track">
              <div
                className={`bar__fill${pct >= 90 ? " bar__fill--full" : ""}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function DaysRing({ expiry, daysLeft, total }) {
  const days = daysLeft ?? 0;
  const frac = total > 0 ? Math.max(0, Math.min(1, days / total)) : 0;
  const r = 34;
  const c = 2 * Math.PI * r;
  return (
    <div className="ring">
      <svg width="88" height="88" viewBox="0 0 88 88" aria-hidden="true">
        <circle cx="44" cy="44" r={r} className="ring__track" />
        <circle
          cx="44" cy="44" r={r}
          className={`ring__fill${days <= 3 ? " ring__fill--low" : ""}`}
          strokeDasharray={`${c * frac} ${c}`}
          transform="rotate(-90 44 44)"
        />
      </svg>
      <div className="ring__mid">
        <b>{days}</b>
        <span>days</span>
      </div>
      <div className="ring__side">
        <div className="small">
          Removed automatically on{" "}
          <b>{expiry?.expiresAt ? expiry.expiresAt.slice(0, 10) : "—"}</b>
        </div>
      </div>
    </div>
  );
}

// --- KNOWLEDGE BASE (rag-stack only) ---------------------------------------

/**
 * Small pill for a dependency that is up or is not.
 *
 * NOT the StatusPill vocabulary. That one describes a PROJECT — and its
 * `sleeping` state means "idle by design, holding no GPU", which is a good
 * thing. A vector database is an ordinary Deployment that never scales to zero,
 * so "not running" is always a fault, and borrowing a vocabulary in which
 * not-running is sometimes healthy would blur exactly the distinction a PI needs.
 */
function ServicePill({ ready, desired, label }) {
  if (!desired) {
    return <span className="badge badge--red">{label} missing</span>;
  }
  if (ready >= desired) {
    return <span className="badge badge--green">{label} running</span>;
  }
  return <span className="badge badge--amber">{label} starting</span>;
}

/**
 * What a RAG project has that an inference project does not.
 *
 * ---------------------------------------------------------------------------
 * TWO NUMBERS, AND THEY ARE DIFFERENT NUMBERS
 * ---------------------------------------------------------------------------
 * `indexedChunks` is what the ingestion run REPORTED — registry history, the
 * output of a step that finished. `livePoints` is what Qdrant holds RIGHT NOW,
 * read on every request through a port-forward (invariant (b)).
 *
 * They are shown side by side rather than collapsed into one, because when they
 * disagree that IS the information: an index that was rebuilt, dropped, or never
 * committed what the Job thought it had. A single number would have to pick one,
 * and either choice would sometimes be a lie.
 *
 * `livePoints: null` means "could not be read", which is not the same as 0.
 */
export function KnowledgeBase({ d, rag }) {
  const outputs = d.outputs || {};
  // WHAT THE RUN PRODUCED comes from the pipeline's outputs whether or not there
  // is a cluster to ask; WHAT IS THERE NOW only exists when there is. In real
  // mode the BFF copies the first into `live.rag` beside the second, so both
  // reads resolve; in mock mode there is no `live` at all and the outputs are
  // all there is. Falling back rather than showing dashes is what makes this
  // card usable for building the console with Docker stopped, which is the mode
  // most of this work happens in.
  const collection = rag?.collection ?? outputs.collectionName;
  const live = rag?.livePoints;
  const files = rag?.indexedFiles ?? outputs.indexedFiles;
  const chunks = rag?.indexedChunks ?? outputs.indexedChunks;
  const topK = d.params?.topK;

  // No `live` block at all means the BFF is in mock mode: there is no cluster to
  // read from, so every number here is what a run REPORTED and none of it
  // describes anything that exists. Saying so is invariant (e) — a console that
  // showed canned figures as live ones would teach the audience the opposite of
  // what it is demonstrating.
  const mock = !d.live;

  const mismatch =
    typeof live === "number" && typeof chunks === "number" && live !== chunks;

  return (
    <div className="card">
      <h3>Knowledge base</h3>

      {!mock && (
      <div className="kbpills">
        <ServicePill
          ready={rag?.vectorDb?.ready ?? 0}
          desired={rag?.vectorDb?.desired ?? 0}
          label="Search index"
        />
        <ServicePill
          ready={rag?.ragApi?.ready ?? 0}
          desired={rag?.ragApi?.desired ?? 0}
          label="Endpoint"
        />
      </div>
      )}

      <div className="kv">
        <span>Collection</span>
        <b>
          <code>{collection || "—"}</code>
        </b>
      </div>
      <div className="kv">
        <span>Documents indexed</span>
        <b>{files ?? "—"}</b>
      </div>
      <div className="kv">
        <span>Passages indexed</span>
        <b>{chunks ?? "—"}</b>
      </div>
      {!mock && (
        <div className="kv">
          <span>Passages in the index now</span>
          <b>
            {live == null ? (
              <span className="muted">could not be read</span>
            ) : (
              live.toLocaleString()
            )}
          </b>
        </div>
      )}
      <div className="kv">
        <span>Passages used per question</span>
        <b>{topK ?? "—"}</b>
      </div>

      {mismatch && (
        <p className="small muted">
          Your last indexing run produced {chunks} passages and the index now holds{" "}
          {live}. That difference is real, not a rounding — the index has changed
          since it was built.
        </p>
      )}

      {mock && (
        <AmberBanner>
          <strong>Nothing was really indexed.</strong> This project was set up in
          rehearsal mode, so the figures above are what a real run would report,
          not what any search index holds. There is no vector database to read.
        </AmberBanner>
      )}

      {!mock && live === 0 && (
        <div className="state state--error" style={{ marginTop: 10 }}>
          <div className="state__title">Your index is empty</div>
          <p className="small">
            There is nothing to search, so every question will be answered
            &ldquo;not in the indexed documents&rdquo;. The Overview steps show what
            the indexing run reported.
          </p>
        </div>
      )}

      {!mock && (
        <p className="small muted">
          The last two rows are read from your running search index each time this
          page loads. The ones above them are what your indexing run reported —
          shown together, because when they disagree that is worth knowing.
        </p>
      )}
    </div>
  );
}

// --- OVERVIEW ---------------------------------------------------------------

function Overview({ d, model, template, onRefresh }) {
  const live = d.live || {};
  const rag = live.rag;
  const isRag = Boolean(rag) || template?.spec?.interface === "rag";
  const host = live.endpointUrl
    ? live.endpointUrl.replace(/^https?:\/\//, "").split("/")[0]
    : `${d.id}.example`;
  const total = d.params?.durationDays ?? 30;

  // The two curl forms, with the Host header, because that header IS the routing
  // locally and a PI copying this without it gets a 404 in 8ms and no clue why.
  const curlPredict = `# ${d.id} — KServe V1 predict
# The Host header is how the gateway knows which model you mean.
# production: add  -H "Authorization: Bearer $TOKEN"   (Keycloak-issued)
curl -X POST http://127.0.0.1:8080/v1/models/${live.syncedName || d.id}:predict \\
  -H 'Host: ${host}' \\
  -H 'Content-Type: application/json' \\
  -d '${JSON.stringify(model?.sampleInput ?? { instances: [[0]] })}'`;

  const curlChat = `# ${d.id} — OpenAI-compatible chat
# production: add  -H "Authorization: Bearer $TOKEN"   (Keycloak-issued)
curl -X POST http://127.0.0.1:8080/v1/chat/completions \\
  -H 'Host: ${host}' \\
  -H 'Content-Type: application/json' \\
  -d '{"model":"${model?.id ?? d.id}","messages":[{"role":"user","content":"Hello"}]}'`;

  // A RAG project's endpoint takes a QUESTION, not messages and not instances.
  // It is an ordinary ClusterIP Service inside the project rather than something
  // behind the gateway, which is why this form has no Host header — and why the
  // note about production is different: there the same request goes through the
  // gateway with a bearer token, exactly as the other two do.
  const curlRag = `# ${d.id} — ask your documents a question
# Reachable inside the cluster. From your laptop, forward the service first:
#   kubectl port-forward svc/${d.id}-rag -n p-${d.id} 8080:8080
#
# production: this same request goes through the gateway instead —
#   curl -X POST https://<your-endpoint>/chat \\
#     -H "Authorization: Bearer $TOKEN"        (Keycloak-issued)
curl -X POST http://127.0.0.1:8080/chat \\
  -H 'Content-Type: application/json' \\
  -d '{"question":"What do these documents say about ...?"}'

# The reply carries the answer AND the passages it came from:
#   {"answer": "...", "sources": [{"chunk_id":"notes.md#3","score":0.83,...}], ...}`;

  const curl = isRag ? curlRag : model?.interface === "chat" ? curlChat : curlPredict;

  return (
    <div className="tabbody">
      <div className="ovgrid">
        <div className="card">
          <h3>Status</h3>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <StatusPill status={d.derivedStatus} />
            <button type="button" className="btn btn--ghost btn--sm" onClick={onRefresh}>
              Refresh
            </button>
          </div>
          {d.derivedStatus === "sleeping" && (
            <p className="small muted">
              Idle, holding no GPU — <b>wakes on first request</b>, in about 15 seconds.
              This is what &ldquo;on demand&rdquo; means and it is working correctly.
            </p>
          )}
          {d.derivedStatus === "waking" && (
            <p className="small muted">Starting up after being idle…</p>
          )}
          <div className="kv">
            <span>Running copies</span>
            <b>{live.predictorPods ?? 0}</b>
          </div>
          <div className="kv">
            <span>Created</span>
            <b>{d.createdAt?.replace("T", " ").replace("Z", " UTC")}</b>
          </div>
          <div className="kv">
            <span>Template</span>
            <b>{d.templateId}</b>
          </div>
        </div>

        <div className="card">
          <h3>Time remaining</h3>
          <DaysRing expiry={live.expiry} daysLeft={d.daysLeft} total={total} />
          <AmberBanner>
            Expiry is <strong>recorded but not yet enforced</strong>. Automatic removal
            arrives with the platform TTL service; today the date is written on your
            project and nothing acts on it.
          </AmberBanner>
        </div>
      </div>

      <div className="card">
        <h3>Your endpoint</h3>
        {isRag && rag?.ragEndpointUrl && (
          <>
            <div className="endpoint">
              <code>{rag.ragEndpointUrl}</code>
              <Copy text={rag.ragEndpointUrl} />
            </div>
            <p className="small muted">
              This is what you ask questions of. The model below is what writes the
              answers — you can call it directly to compare an answer with and
              without your documents behind it.
            </p>
          </>
        )}
        {live.endpointUrl ? (
          <>
            <div className="endpoint">
              <code>{live.endpointUrl}</code>
              <Copy text={live.endpointUrl} />
            </div>
            <p className="small muted">
              Read live from your running service — not stored, not guessed.
            </p>
          </>
        ) : (
          <p className="small muted">No endpoint yet.</p>
        )}

        <h3 style={{ marginTop: 8 }}>Call it from a terminal</h3>
        <div className="endpoint">
          <pre className="yaml" tabIndex={0}>
            <code>{curl}</code>
          </pre>
        </div>
        <Copy text={curl} label="Copy command" />
      </div>

      {isRag && <KnowledgeBase d={d} rag={rag} />}

      <div className="card">
        <h3>What you can use at once</h3>
        <QuotaBars quota={live.quota} />
        <p className="small muted">
          Requests beyond your ceiling are refused rather than queued.
        </p>
      </div>

      <div className="card">
        <h3>Your private cluster (vCluster)</h3>
        <p className="small muted">
          Admin credentials for the Kubernetes control plane that is yours. Use it with{" "}
          <code>kubectl --kubeconfig</code> to inspect or change anything inside your
          own project.
        </p>
        <div>
          <a
            className="btn btn--ghost btn--sm"
            href={`/api/deployments/${d.id}/kubeconfig`}
            download
          >
            Download kubeconfig
          </a>
        </div>
      </div>
    </div>
  );
}

// --- RESULTS (run-shaped templates: batch-inference) ------------------------
//
// A RUN IS NOT A SERVICE, AND ITS PAGE SAID OTHERWISE. A finished batch project
// used to get the service Overview: "Your endpoint — No endpoint yet", a curl
// command for an endpoint that never exists, "Running copies 0" — and nowhere
// on the page were the predictions the run had just produced. They sat on disk
// in tenants/<project>/artifacts/, reachable only by someone who knew the path.
//
// So a run's Overview leads with its RESULTS: what it did, the answers
// themselves, and the files. Everything here is read from disk through
// GET /api/deployments/{id}/results — the files collect-results actually
// fetched — never from the registry's memory of what the run said it wrote.

/** "sepal_length: 4.9\nsepal_width: 3.5" -> { sepal_length: "4.9", ... } */
function parseInput(text) {
  const out = {};
  if (typeof text !== "string") return out;
  for (const line of text.split("\n")) {
    const at = line.indexOf(": ");
    if (at > 0) out[line.slice(0, at)] = line.slice(at + 2);
  }
  return out;
}

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 1) return "under a second";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

/**
 * One prediction, with its meaning when the catalogue knows it.
 *
 * A classifier answers with a class index; `2` on its own tells a PI nothing
 * about their iris. models.yaml's `outputLabels` says what each index means,
 * and the raw value stays beside the label so nothing is hidden by the
 * translation. Long text answers (chat models) are cut to one line here and
 * kept whole in the tooltip and the downloaded file.
 */
function Prediction({ value, labels }) {
  if (value !== null && typeof value === "object") {
    const text = JSON.stringify(value);
    return <span title={text}>{text.length > 120 ? `${text.slice(0, 120)}…` : text}</span>;
  }
  const label = labels?.[String(value)];
  if (label) {
    return (
      <span>
        <b>{label}</b> <span className="muted small">{String(value)}</span>
      </span>
    );
  }
  const text = String(value ?? "");
  return <span title={text}>{text.length > 140 ? `${text.slice(0, 140)}…` : text}</span>;
}

export function RunResults({ d, model, mode, results }) {
  const [fetched, setFetched] = useState(null);
  const [error, setError] = useState(null);
  const data = results ?? fetched;

  // Re-read when the run's state changes, so results appear the moment the run
  // finishes rather than on the next full page load.
  useEffect(() => {
    if (results) return undefined;
    let cancelled = false;
    get(`/api/deployments/${d.id}/results?limit=50`)
      .then((body) => !cancelled && (setFetched(body), setError(null)))
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [d.id, d.state, results]);

  if (error) return <ErrorState error={error} />;
  if (!data) return <Loading verb="Reading your results…" />;

  const rows = data.rows || [];
  const totals = data.summary?.totals || {};
  const artifacts = data.artifacts || [];

  if (!rows.length && !artifacts.length) {
    return (
      <div className="state">
        <div className="state__title">No results to show</div>
        <p className="small">
          {mode === "mock"
            ? "This was a rehearsal run: Studio is in mock mode, so no model was actually run over your data and there are no predictions. Run it again with Studio in real mode to get them."
            : "This run did not leave any results behind. If it finished, they may have been removed by hand; the steps above say how far it got."}
        </p>
      </div>
    );
  }

  // THE COLUMNS THE PI CHOSE, IN THE ORDER THEY CHOSE THEM — which for a
  // classifier is the order of the feature vector, and so the one worth showing.
  // Falls back to whatever the first row carries for a run whose params do not
  // say (a JSONL dataset whose fields were free-form).
  const columns =
    (d.params?.inputFields?.length && d.params.inputFields) ||
    Object.keys(parseInput(rows[0]?.input));
  const labels = model?.outputLabels;
  const failed = totals.rowsFailed ?? (data.errors || []).length;

  return (
    <>
      <div className="runstats">
        <div>
          <span>Rows processed</span>
          <b>{(totals.rowsProcessed ?? rows.length).toLocaleString()}</b>
        </div>
        <div className={failed ? "runstats--bad" : undefined}>
          <span>Failed</span>
          <b>{failed.toLocaleString()}</b>
        </div>
        <div>
          <span>Took</span>
          <b>{fmtDuration(totals.durationSeconds)}</b>
        </div>
      </div>

      <h3 style={{ marginTop: 18 }}>Predictions</h3>
      <p className="small muted">
        {rows.length < (totals.rowsProcessed ?? 0)
          ? `The first ${rows.length} of ${totals.rowsProcessed.toLocaleString()} rows. Download results.jsonl below for all of them.`
          : `All ${rows.length} rows.`}
      </p>
      <div className="tablewrap runtable">
        <table>
          <thead>
            <tr>
              <th>#</th>
              {columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
              <th>Prediction</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const cells = parseInput(r.input);
              return (
                <tr key={r.row_index}>
                  <td className="muted">{r.row_index + 1}</td>
                  {columns.map((c) => (
                    <td key={c}>{cells[c] ?? "—"}</td>
                  ))}
                  <td>
                    <Prediction value={r.output} labels={labels} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {failed > 0 && (data.errors || []).length > 0 && (
        <div className="state state--error" style={{ marginTop: 14 }}>
          <div className="state__title">
            {failed.toLocaleString()} row{failed === 1 ? "" : "s"} failed
          </div>
          {data.errors.slice(0, 3).map((e) => (
            <p className="small" key={e.row_index}>
              <b>Row {e.row_index + 1}:</b> {String(e.error).slice(0, 200)}
            </p>
          ))}
          <p className="small muted">Every failure, with its row, is in errors.jsonl.</p>
        </div>
      )}

      <h3 style={{ marginTop: 18 }}>Download</h3>
      <div className="rundl">
        {artifacts.map((a) => (
          <a
            key={a.name}
            className="btn btn--ghost btn--sm"
            href={`/api/deployments/${d.id}/artifacts/${encodeURIComponent(a.name)}`}
            download
          >
            {a.name} <span className="muted">· {humanBytes(a.bytes)}</span>
          </a>
        ))}
      </div>
      <p className="small muted">
        <code>results.jsonl</code> has one line per row with its input and prediction;{" "}
        <code>run-summary.json</code> records exactly what ran.
      </p>
    </>
  );
}

function RunOverview({ d, model, onRefresh }) {
  const live = d.live || {};
  const mode = useExecutorMode();
  const retention = d.params?.artifactRetentionDays ?? 14;

  return (
    <div className="tabbody">
      <div className="ovgrid">
        <div className="card">
          <h3>Status</h3>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <StatusPill status={d.derivedStatus} />
            <button type="button" className="btn btn--ghost btn--sm" onClick={onRefresh}>
              Refresh
            </button>
          </div>
          <p className="small muted">
            A batch run has no endpoint: it ran your model over your file once, and
            its answers are below. Nothing is left running and no GPU is held.
          </p>
          <div className="kv">
            <span>Model</span>
            <b>{model?.displayName ?? d.params?.model}</b>
          </div>
          <div className="kv">
            <span>Created</span>
            <b>{d.createdAt?.replace("T", " ").replace("Z", " UTC")}</b>
          </div>
          <div className="kv">
            <span>Template</span>
            <b>{d.templateId}</b>
          </div>
        </div>

        <div className="card">
          <h3>Results kept for</h3>
          <DaysRing expiry={live.expiry} daysLeft={d.daysLeft} total={retention} />
          <AmberBanner>
            Retention is <strong>recorded but not yet enforced</strong>. Automatic
            removal arrives with the platform TTL service; today the date is written
            on your project and nothing acts on it.
          </AmberBanner>
        </div>
      </div>

      <div className="card">
        <h3>Your results</h3>
        <RunResults d={d} model={model} mode={mode} />
      </div>

      <div className="card">
        <h3>What you can use at once</h3>
        <QuotaBars quota={live.quota} />
        <p className="small muted">
          The GPU goes back to the shared pool when the run ends, which is why it
          reads zero here.
        </p>
      </div>

      <div className="card">
        <h3>Your private cluster (vCluster)</h3>
        <p className="small muted">
          Admin credentials for the Kubernetes control plane that is yours. Use it with{" "}
          <code>kubectl --kubeconfig</code> to inspect anything inside your project.
        </p>
        <div>
          <a
            className="btn btn--ghost btn--sm"
            href={`/api/deployments/${d.id}/kubeconfig`}
            download
          >
            Download kubeconfig
          </a>
        </div>
      </div>
    </div>
  );
}

// --- EVENTS -----------------------------------------------------------------

function Events({ project }) {
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      setEntries((await get(`/api/deployments/${project}/events`)).entries);
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [project]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={load} />;
  if (!entries) return <Loading verb="Loading events…" />;

  return (
    <div className="tabbody">
      <p className="small muted">
        What Studio did, and what Kubernetes did about it — newest first.
      </p>
      <div className="evlist">
        {entries.length === 0 && <p className="small muted">Nothing recorded yet.</p>}
        {entries.map((e, i) => (
          <div className={`ev ev--${e.source}`} key={i}>
            <span className="ev__src">{e.source === "pipeline" ? e.step : "cluster"}</span>
            <span className="ev__text">{e.text}</span>
          </div>
        ))}
      </div>
      <button type="button" className="btn btn--ghost btn--sm" onClick={load}>
        Refresh
      </button>
    </div>
  );
}

// --- SETTINGS ---------------------------------------------------------------

function Settings({ d, onChanged }) {
  const navigate = useNavigate();
  const [name, setName] = useState(d.displayName || d.id);
  const [saved, setSaved] = useState(false);
  const [renewal, setRenewal] = useState(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function rename() {
    setError(null);
    try {
      await patch(`/api/deployments/${d.id}`, { displayName: name });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      onChanged();
    } catch (err) {
      setError(err);
    }
  }

  async function requestRenewal() {
    setError(null);
    try {
      await post(`/api/deployments/${d.id}/renewal`);
      setRenewal("Request recorded — routed to the Research Committee in production.");
      setTimeout(() => setRenewal(null), 6000);
    } catch (err) {
      setError(err);
    }
  }

  async function destroy() {
    setBusy(true);
    setError(null);
    try {
      await del(`/api/deployments/${d.id}`);
      // Stay on the page: teardown is a pipeline too, and watching it is the
      // point. The Overview switches to the teardown checklist.
      onChanged();
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <div className="tabbody">
      <div className="card">
        <h3>Display name</h3>
        <div className="field">
          <input value={name} onChange={(e) => setName(e.target.value)} />
          <small>
            A label for you. Your project id <code>{d.id}</code> stays the same — it is
            your cluster name and your endpoint address, so it cannot change.
          </small>
        </div>
        <div>
          <button type="button" className="btn btn--ghost btn--sm" onClick={rename}>
            {saved ? "Saved" : "Save name"}
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Need more time?</h3>
        <p className="small muted">
          Your project is removed when its time runs out. Ask for an extension and it
          is recorded against the project.
        </p>
        {renewal && <div className="toast">{renewal}</div>}
        <div>
          <button type="button" className="btn btn--ghost btn--sm" onClick={requestRenewal}>
            Request renewal
          </button>
        </div>
      </div>

      {error && (
        <div className="state state--error">
          <div className="state__title">That did not work</div>
          <p className="small">{error.message}</p>
        </div>
      )}

      {!d.deletedAt && (
        <div className="card card--bad">
          <h3>Danger zone</h3>
          <p className="small">
            Removing this project takes down the model, your private cluster and
            everything stored in it. It cannot be undone.
          </p>
          <div className="field">
            <label htmlFor="confirm-del">
              Type <code>{d.id}</code> to confirm
            </label>
            <input
              id="confirm-del"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div>
            <button
              type="button"
              className="btn btn--danger"
              disabled={typed.trim() !== d.id || busy}
              onClick={destroy}
            >
              {busy ? "Removing…" : "Remove this project"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// --- PROGRESS (kept from S4, shown while the pipeline runs) -----------------

/**
 * One step in the progress list.
 *
 * ---------------------------------------------------------------------------
 * EVERY STEP'S LOG IS READABLE, NOT ONLY A FAILED ONE
 * ---------------------------------------------------------------------------
 * This used to render the log for `failed` steps and hide it otherwise, on the
 * reasonable-sounding grounds that a successful step has nothing to explain.
 * That is false for any step whose OUTPUT is the interesting part, and the RAG
 * pipeline is full of them: `ingestion` prints how many files it read, how many
 * passages it produced and the machine-readable INGEST_RESULT line;
 * `rag-ready-check` prints the smoke question and the answer it got back.
 * Hiding those meant the most informative thing the pipeline produced was
 * visible only if it broke.
 *
 * So: a failed step opens ITSELF, because that log is why the PI is looking. A
 * successful step offers its log behind a disclosure. This is a generic change —
 * no step id appears anywhere in this component — so any future step that
 * reports something useful is readable without another edit here.
 */
export function StepRow({ step, tick }) {
  const meta = STEPS[step.stepId];
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (step.state === "failed") setOpen(true);
  }, [step.state]);
  const failed = step.state === "failed";
  const hasLog = Boolean(step.log && step.log.trim());

  const icons = { succeeded: "✓", running: "●", failed: "✕", skipped: "–", pending: "○" };
  const cls = { succeeded: "ok", running: "run", failed: "bad", skipped: "skip", pending: "wait" };
  const timing =
    step.state === "running"
      ? `${secondsSince(step.startedAt)}s elapsed`
      : step.durationSeconds != null
        ? `${step.durationSeconds}s`
        : "";

  return (
    <div className={`prow prow--${step.state}`} data-tick={tick}>
      <span className={`sicon sicon--${cls[step.state] || "wait"}`}>
        {icons[step.state] || "○"}
      </span>
      <div className="prow__body">
        <div className="prow__head">
          <b>{stepLabel(step.stepId)}</b>
          <span className="prow__time">{timing}</span>
        </div>
        {meta && !failed && <span className="prow__detail">{meta.detail}</span>}
        {failed && (
          <span className="prow__detail prow__detail--bad">
            This step failed. Everything after it was not attempted.
          </span>
        )}

        {hasLog && (
          <>
            <button
              type="button"
              className="disclose disclose--sm"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
            >
              <span className="disclose__caret">{open ? "▾" : "▸"}</span>
              What the step reported
            </button>
            {open && (
              <pre className="yaml yaml--log" tabIndex={0}>
                <code>{step.log}</code>
              </pre>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export function Progress({ d, tick, onRetry, onDelete, busy }) {
  const provision = d.steps.filter((s) => s.phase === "provision");
  const teardown = d.steps.filter((s) => s.phase === "teardown");
  const failed = provision.find((s) => s.state === "failed");
  const done = provision.filter((s) => s.state === "succeeded").length;

  return (
    <div className="tabbody">
      <section className="section">
        <div className="section__head">
          <h2>{teardown.length ? "Removing" : "Setting up"}</h2>
          <span className="section__count">
            {done} of {provision.length} done
          </span>
        </div>
        <div className="plist">
          {provision.map((s) => (
            <StepRow key={s.stepId} step={s} tick={tick} />
          ))}
        </div>
      </section>

      {teardown.length > 0 && (
        <section className="section">
          <div className="section__head">
            <h2>Teardown</h2>
          </div>
          <div className="plist">
            {teardown.map((s) => (
              <StepRow key={`t-${s.stepId}`} step={s} tick={tick} />
            ))}
          </div>
        </section>
      )}

      {failed && (
        <div className="card card--bad">
          <h3>What you can do</h3>
          <p className="small">
            <b>Retry</b> picks up from &ldquo;{stepLabel(failed.stepId)}&rdquo; — the
            steps that already finished are not repeated.
          </p>
          <div style={{ display: "flex", gap: 10 }}>
            <button type="button" className="btn btn--primary" onClick={onRetry} disabled={busy}>
              Retry step
            </button>
            <button type="button" className="btn btn--danger" onClick={onDelete} disabled={busy}>
              Delete and start over
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// --- PAGE -------------------------------------------------------------------

export default function DeploymentDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "Overview";

  const [d, setD] = useState(null);
  const [models, setModels] = useState([]);
  // The deployment's template, for its declared `interface` — which decides
  // what the playground renders and whether the Overview shows a knowledge base.
  // Fetched once and separately from the polling loop: a template is committed
  // data that changes by a git commit and a restart, so re-reading it every two
  // seconds would be a request per poll for an answer that cannot change.
  const [template, setTemplate] = useState(null);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const body = await get(`/api/deployments/${id}`);
      setD(body);
      setError(null);
      return body;
    } catch (err) {
      setError(err);
      return null;
    }
  }, [id]);

  useEffect(() => {
    get("/api/models").then(setModels).catch(() => setModels([]));
  }, []);

  useEffect(() => {
    if (!d?.templateId) return;
    // Failure is tolerated: without the template the playground falls back to
    // the model's own interface, which is what every project did before
    // templates could declare one. A page that refused to render because a
    // secondary fetch failed would be worse than one that degrades.
    get(`/api/templates/${d.templateId}`)
      .then(setTemplate)
      .catch(() => setTemplate(null));
  }, [d?.templateId]);

  useEffect(() => {
    let stopped = false;
    async function loop() {
      const body = await load();
      setTick((t) => t + 1);
      if (stopped) return;
      const active =
        body &&
        (body.running ||
          ["provisioning", "waking"].includes(body.derivedStatus) ||
          body.state === "requested");
      timer.current = setTimeout(loop, active ? POLL_ACTIVE : POLL_SETTLED);
    }
    loop();
    return () => {
      stopped = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  if (error && !d)
    return (
      <div className="page">
        <ErrorState error={error} onRetry={load} />
      </div>
    );
  if (!d)
    return (
      <div className="page">
        <Loading verb="Loading your project…" />
      </div>
    );

  // WHICH PARAMETER HOLDS THE SERVED MODEL depends on the template:
  // inference-endpoint calls it `model`, rag-stack calls it `baseModel` and adds
  // `embeddingModel` beside it. Mirrors selected_models() in app/main.py, which
  // is the backend's one place that knows the same thing — and the reason this
  // is a helper rather than an inline `??` is that getting it wrong is silent:
  // the playground simply reports the model as missing from the catalogue.
  const model = models.find((m) => m.id === (d.params?.model || d.params?.baseModel));
  // A RUN, not a service: it finishes, has results instead of an endpoint, and
  // has nothing to call. Read from the template's own `lifecycle.kind` — or,
  // until the template has loaded, from the live `run` block the BFF adds only
  // for run-shaped projects, so the page does not flash the service layout first.
  const isRun = template?.spec?.lifecycle?.kind === "run" || Boolean(d.live?.run);
  const tabs = isRun ? TABS.filter((t) => t !== "Playground") : TABS;
  const shownTab = tabs.includes(tab) ? tab : "Overview";
  const inFlight =
    d.running || ["provisioning"].includes(d.derivedStatus) || d.state === "failed";

  async function retry() {
    setBusy(true);
    try {
      await post(`/api/deployments/${id}/retry`);
      await load();
    } finally {
      setBusy(false);
    }
  }
  async function destroy() {
    setBusy(true);
    try {
      await del(`/api/deployments/${id}`);
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <div className="page__head">
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <h1>{d.displayName || d.id}</h1>
          <StatusPill status={d.derivedStatus} />
        </div>
        <p className="small muted">
          Project id <code>{d.id}</code>
          {d.displayName && d.displayName !== d.id ? " · renamed label" : ""}
        </p>
      </div>

      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={shownTab === t}
            className={`tab${shownTab === t ? " tab--on" : ""}`}
            onClick={() => setParams({ tab: t })}
          >
            {t}
          </button>
        ))}
      </div>

      {/* While the pipeline is running or has failed, the Overview tab shows the
          checklist instead — that is the thing worth watching, and hiding it
          behind a tab would bury the reason a project is not working. */}
      {shownTab === "Overview" &&
        (inFlight ? (
          <Progress d={d} tick={tick} onRetry={retry} onDelete={destroy} busy={busy} />
        ) : isRun ? (
          <RunOverview d={d} model={model} onRefresh={load} />
        ) : (
          <Overview d={d} model={model} template={template} onRefresh={load} />
        ))}
      {shownTab === "Playground" && (
        <Playground deployment={d} model={model} template={template} />
      )}
      {shownTab === "Events" && <Events project={id} />}
      {shownTab === "Settings" && <Settings d={d} onChanged={load} />}

      <div>
        <Link className="btn btn--ghost btn--sm" to="/deployments">
          All projects
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// WHY StepRow, Progress AND KnowledgeBase ARE EXPORTED
// ---------------------------------------------------------------------------
// Not because anything else on the page imports them — nothing does — but so
// that tests/ui-check.jsx can mount them directly. They are the three pieces
// whose behaviour is worth asserting rather than eyeballing: the step list has
// to render whatever plan the backend hands it, a step's log has to be readable
// whether or not the step failed, and the knowledge base has to tell "could not
// be read" apart from "zero".
//
// The alternative was driving the whole page through a mock BFF to reach three
// components, which tests the mocks as much as the code.
