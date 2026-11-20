// ===========================================================================
// HIVE STUDIO — PLAYGROUND
// ===========================================================================
// Three interfaces, one job: let a PI confirm their endpoint answers, without
// leaving the browser and without learning curl.
//
//   chat        a message list, for an OpenAI-compatible endpoint
//   predict-v1  a JSON request/response pair, prefilled with the model's own
//               known-good sampleInput
//   rag         a question box, an answer, and THE PASSAGES THE ANSWER CAME FROM
//
// ---------------------------------------------------------------------------
// WHICH ONE IS CHOSEN, AND BY WHAT
// ---------------------------------------------------------------------------
// The TEMPLATE decides first, then the model. `spec.interface` on a template is
// normally absent, because an inference-endpoint project IS its model and the
// model's own `interface` is the right answer. rag-stack declares
// `interface: rag`, because there the endpoint is NOT the model: it is the
// retrieval service in front of it, and the chat model underneath — still
// `interface: chat` in models.yaml — is not what anybody calls.
//
// Branching on the template id instead would have worked today and would have
// meant every future template needed a line here. Branching on a declared
// interface means a template says how it is talked to, which is the thing it
// actually knows.
//
// EVERY CALL GOES THROUGH THE BFF. The browser never talks to the cluster: a
// local endpoint is only reachable through a port-forward plus a `Host:` header,
// which is a local shortcut standing in for MetalLB + Kong. If the browser
// learned that shape, it would learn a URL that does not exist in production.
// ===========================================================================

import { useEffect, useRef, useState } from "react";

import { ApiError, post } from "../api.js";
import { AmberBanner } from "../components.jsx";
import { stepLabel } from "../steps.js";

function Latency({ ms, cold }) {
  return (
    <span className={`lat${cold ? " lat--cold" : ""}`}>
      {ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`}
      {cold ? " · cold start" : ""}
    </span>
  );
}

// ---------------------------------------------------------------------------
// CHAT
// ---------------------------------------------------------------------------

function ChatPlayground({ project, asleep }) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    const next = [...messages, { role: "user", content: text }];
    setMessages(next);
    setDraft("");
    setBusy(true);
    setError(null);

    try {
      const res = await post(`/api/deployments/${project}/invoke`, {
        messages: next.map((m) => ({ role: m.role, content: m.content })),
      });
      const choice = res.body?.choices?.[0]?.message?.content;
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          // Show the raw body when the shape is not what an OpenAI-compatible
          // endpoint should return — that IS the useful information.
          content: choice ?? JSON.stringify(res.body, null, 2),
          latencyMs: res.latencyMs,
          cold: res.coldStart,
        },
      ]);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pg">
      {asleep && (
        <AmberBanner>
          <strong>Your model is asleep.</strong> The first message wakes it, which
          takes about 15 seconds longer than usual. Everything after that is fast.
        </AmberBanner>
      )}

      <div className="chat">
        {messages.length === 0 && (
          <p className="small muted">
            Send a message to try your endpoint. Nothing here is stored.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`bubble bubble--${m.role}`}>
            <div className="bubble__who">{m.role === "user" ? "You" : "Model"}</div>
            <div className="bubble__text">{m.content}</div>
            {m.latencyMs != null && <Latency ms={m.latencyMs} cold={m.cold} />}
          </div>
        ))}
        {busy && (
          <div className="bubble bubble--assistant">
            <div className="bubble__who">Model</div>
            <div className="bubble__text muted">
              {asleep ? "Waking your deployment — this one takes longer…" : "Thinking…"}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <div className="state state--error" role="alert">
          <div className="state__title">The model did not answer</div>
          <p className="small">{error.message}</p>
        </div>
      )}

      <div className="pg__compose">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask your model something…"
          disabled={busy}
        />
        <button type="button" className="btn btn--primary" onClick={send} disabled={busy || !draft.trim()}>
          {busy ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// PREDICT
// ---------------------------------------------------------------------------

function PredictPlayground({ project, model, asleep }) {
  // Prefilled with the catalogue's own sampleInput — a known-good body, so the
  // first thing a PI does is press a button rather than compose JSON.
  const [body, setBody] = useState(() => JSON.stringify(model?.sampleInput ?? {}, null, 2));
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function send() {
    setBusy(true);
    setError(null);
    setResult(null);
    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch (e) {
      setError(new Error(`That is not valid JSON — ${e.message}`));
      setBusy(false);
      return;
    }
    try {
      setResult(await post(`/api/deployments/${project}/invoke`, { input: parsed }));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pg">
      {asleep && (
        <AmberBanner>
          <strong>Your model is asleep.</strong> The first request wakes it and takes
          about 15 seconds longer. That is the trade you chose with “on demand”.
        </AmberBanner>
      )}

      <div className="pg__panes">
        <div className="field">
          <label htmlFor="pg-body">Request</label>
          <textarea
            id="pg-body"
            className="pg__json"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            spellCheck={false}
            rows={10}
          />
          <small>Prefilled with a known-good example for this model.</small>
        </div>

        <div className="field">
          <label>
            Response
            {result && <Latency ms={result.latencyMs} cold={result.coldStart} />}
          </label>
          <pre className="pg__json pg__json--out" tabIndex={0}>
            <code>
              {error
                ? error.message
                : result
                  ? `HTTP ${result.status}\n\n${JSON.stringify(result.body, null, 2)}`
                  : busy
                    ? asleep
                      ? "Waking your deployment — this one takes longer…"
                      : "Sending…"
                    : "—"}
            </code>
          </pre>
        </div>
      </div>

      <button type="button" className="btn btn--primary" onClick={send} disabled={busy}>
        {busy ? "Sending…" : "Send request"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RAG
// ---------------------------------------------------------------------------

/**
 * One retrieved passage, collapsed to its identity and expandable to its text.
 *
 * THE SCORE IS SHOWN EVEN WHEN IT IS BAD, and that is the whole reason this
 * panel exists. A confident answer over three passages scoring 0.31 is a
 * different thing from the same answer over three scoring 0.86, and only one of
 * them should be believed. Hiding weak sources would make the good case and the
 * bad case look identical.
 */
function Source({ source, index }) {
  const [open, setOpen] = useState(false);
  const score = typeof source.score === "number" ? source.score : null;
  // Bands, not a gradient: a number is precise and a colour is a judgement, and
  // three coarse judgements are more honest than a continuous one about a
  // similarity metric whose absolute values mean little.
  const band = score == null ? "" : score >= 0.7 ? " src--strong" : score >= 0.5 ? "" : " src--weak";

  return (
    <div className={`src${band}`}>
      <button
        type="button"
        className="src__head"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="disclose__caret">{open ? "▾" : "▸"}</span>
        <span className="src__n">{index + 1}</span>
        <span className="src__file">{source.source || "unknown file"}</span>
        <code className="src__chunk">{source.chunk_id}</code>
        {score != null && <span className="src__score">{score.toFixed(3)}</span>}
      </button>
      {open && <div className="src__text">{source.text || "(this passage came back empty)"}</div>}
    </div>
  );
}

export function SourcesPanel({ sources }) {
  // ORDERED BY SCORE, DESCENDING. The service returns them in Qdrant's order,
  // which is already this — but sorting here means the panel cannot silently
  // start lying if that ever changes, and the claim "best match first" is one a
  // PI will rely on when judging an answer.
  const ordered = [...(sources || [])].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));

  return (
    <div className="sources">
      <div className="sources__head">
        <b>Sources used</b>
        <span className="small muted">
          {ordered.length === 0
            ? "none — see below"
            : `${ordered.length} passage${ordered.length === 1 ? "" : "s"}, best match first`}
        </span>
      </div>

      {ordered.length === 0 ? (
        // THE EMPTY PANEL IS STILL SHOWN, deliberately. When retrieval finds
        // nothing the service answers honestly WITHOUT asking the model — and
        // the evidence for that is the absence of sources. Hiding the panel
        // would leave a refusal with no visible reason, which reads like the
        // model being unhelpful rather than the index being silent.
        <p className="small muted">
          Nothing in your documents matched this question closely enough to use,
          so the model was not asked. That is why the answer says what it says.
        </p>
      ) : (
        ordered.map((s, i) => <Source key={s.chunk_id || i} source={s} index={i} />)
      )}
    </div>
  );
}

/** The refusal the RAG service returns verbatim when it will not guess. */
const NOT_FOUND = "I don't find that in the indexed documents.";

function RagPlayground({ project, asleep, topK }) {
  const [turns, setTurns] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function ask() {
    const question = draft.trim();
    if (!question || busy) return;
    setDraft("");
    setBusy(true);
    setError(null);
    try {
      const res = await post(`/api/deployments/${project}/invoke`, { question });
      const body = res.body || {};

      // THE BFF ANSWERS 200 EVEN WHEN THE PROJECT DID NOT.
      //
      // It is a proxy: it reports whether IT could reach the service, and passes
      // the service's own status through in `status`. So a 503 from a RAG
      // service whose Qdrant is down arrives here as a successful fetch carrying
      // an unsuccessful answer, and rendering `body` into the answer bubble
      // would put a FastAPI error object where a PI expects prose.
      if (res.status !== 200) {
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : typeof body === "string"
              ? body
              : JSON.stringify(body);
        throw new Error(`Your project answered with ${res.status}. ${detail}`);
      }

      setTurns((t) => [
        ...t,
        {
          question,
          // The service's own shape. When it is not that shape — which would
          // mean the container is returning something this console does not
          // know — show what did arrive, because that IS the useful thing.
          answer:
            typeof body.answer === "string"
              ? body.answer
              : JSON.stringify(body, null, 2),
          sources: Array.isArray(body.sources) ? body.sources : [],
          // Two latencies, and they measure different things: the service's own
          // is retrieval plus the model, this one adds the forward and the
          // proxy. Showing the service's is the honest answer to "how fast is my
          // endpoint?"; the round trip is shown when it differs enough to matter.
          latencyMs: typeof body.latencyMs === "number" ? body.latencyMs : res.latencyMs,
          roundTripMs: res.latencyMs,
          contextChars: body.contextChars,
          modelCalled: body.modelCalled,
        },
      ]);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pg">
      {asleep && (
        <AmberBanner>
          <strong>Your model is asleep.</strong> Your search index is up and your
          endpoint is answering, but the first question wakes the model, which takes
          about 15 seconds longer. Everything after that is fast.
        </AmberBanner>
      )}

      <div className="chat">
        {turns.length === 0 && (
          <p className="small muted">
            Ask a question about your documents. Every answer shows the passages it
            was built from, so you can check it. Nothing here is stored.
          </p>
        )}

        {turns.map((t, i) => (
          <div key={i} className="qa">
            <div className="bubble bubble--user">
              <div className="bubble__who">You</div>
              <div className="bubble__text">{t.question}</div>
            </div>

            <div className="bubble bubble--assistant">
              <div className="bubble__who">
                Your documents
                {t.modelCalled === false && (
                  <span className="badge badge--grey" style={{ marginLeft: 8 }}>
                    model not asked
                  </span>
                )}
              </div>
              <div
                className={`bubble__text${
                  t.answer.trim() === NOT_FOUND ? " bubble__text--refusal" : ""
                }`}
              >
                {t.answer}
              </div>
              <div className="qa__meta">
                <Latency ms={t.latencyMs} />
                {t.roundTripMs != null && t.roundTripMs - t.latencyMs > 150 && (
                  <span className="small muted">
                    · {Math.round((t.roundTripMs - t.latencyMs) / 100) / 10}s of that
                    getting there and back
                  </span>
                )}
                {t.contextChars != null && (
                  <span className="small muted">
                    · {t.contextChars.toLocaleString()} characters of context
                  </span>
                )}
              </div>
            </div>

            <SourcesPanel sources={t.sources} />
          </div>
        ))}

        {busy && (
          <div className="bubble bubble--assistant">
            <div className="bubble__who">Your documents</div>
            <div className="bubble__text muted">
              {asleep
                ? "Waking the model — this first one takes longer…"
                : "Finding the passages, then answering…"}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <div className="state state--error" role="alert">
          <div className="state__title">That question did not get an answer</div>
          <p className="small">{error.message}</p>
        </div>
      )}

      <div className="pg__compose">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask()}
          placeholder="Ask something about your documents…"
          disabled={busy}
        />
        <button
          type="button"
          className="btn btn--primary"
          onClick={ask}
          disabled={busy || !draft.trim()}
        >
          {busy ? "Asking…" : "Ask"}
        </button>
      </div>

      {topK != null && (
        <p className="small muted">
          Each question retrieves the {topK} closest passages from your documents.
        </p>
      )}
    </div>
  );
}

/**
 * How this project's endpoint is talked to.
 *
 * The TEMPLATE overrides the model, and only rag-stack does — see the header.
 * Falling back to the model keeps every existing project behaving exactly as it
 * did, including one deployed before templates could declare an interface.
 */
export function interfaceOf(template, model) {
  return template?.spec?.interface || model?.interface || null;
}

export default function Playground({ deployment, model, template }) {
  const status = deployment.derivedStatus;
  const asleep = status === "sleeping" || status === "waking";
  const kind = interfaceOf(template, model);
  const live = deployment.live || {};
  const rag = live.rag;

  // --- still being built ------------------------------------------------
  //
  // A RAG project reaches this tab with a usable endpoint long before it can
  // ANSWER: the chat model publishes its URL while the index is still being
  // filled and the retrieval service has not started. Letting a PI ask a
  // question then produces a failure that looks like a broken project.
  //
  // GENERIC, AND ON PURPOSE. Rather than naming the steps that matter, this
  // finds the first provisioning step that has not succeeded and reports THAT —
  // taking its words from the one label map, so a template with steps this file
  // has never heard of still explains itself. For a RAG project mid-ingestion
  // that renders exactly "Indexing your documents — this takes a moment",
  // because "Indexing your documents" is what steps.js calls that step.
  const provision = (deployment.steps || []).filter((s) => s.phase === "provision");
  const pending = provision.find((s) => s.state !== "succeeded" && s.state !== "skipped");
  if (pending) {
    const failed = pending.state === "failed";
    return (
      <div className="state">
        <div className="state__title">
          {failed
            ? `${stepLabel(pending.stepId)} — this step did not finish`
            : `${stepLabel(pending.stepId)} — this takes a moment`}
        </div>
        <p className="small muted">
          {failed
            ? "Until that is fixed your project cannot answer. The Overview tab shows what the step reported."
            : "Your endpoint becomes available as soon as your project has finished setting up."}
        </p>
      </div>
    );
  }

  if (!deployment.live?.endpointUrl) {
    return (
      <div className="state">
        <div className="state__title">No endpoint to call yet</div>
        <p className="small muted">
          The playground becomes available once your project finishes setting up.
        </p>
      </div>
    );
  }

  if (!kind) {
    return (
      <div className="state">
        <div className="state__title">This project&rsquo;s model is no longer in the catalog</div>
        <p className="small muted">
          The playground needs to know how to talk to it, and that comes from the
          model list.
        </p>
      </div>
    );
  }

  if (kind === "rag") {
    // The vector database is not part of the model's lifecycle: it is an
    // ordinary Deployment that does not scale to zero, so if it is down it is
    // DOWN — not asleep. Saying so beats letting every question fail with a
    // gateway error.
    const dbDown = rag && rag.vectorDb && rag.vectorDb.exists && rag.vectorDb.ready === 0;
    const dbGone = rag && rag.vectorDb && !rag.vectorDb.exists;
    return (
      <>
        {(dbDown || dbGone) && (
          <div className="state state--error" role="alert">
            <div className="state__title">Your search index is not available</div>
            <p className="small">
              {dbGone
                ? "The vector database for this project is not there. Questions cannot be answered until it is back."
                : "The vector database is not running. Questions will fail until it restarts."}
            </p>
            <p className="small muted">
              It does not sleep when idle, so this is not the project resting — the
              Events tab shows what the cluster did.
            </p>
          </div>
        )}
        <RagPlayground
          project={deployment.id}
          asleep={asleep}
          topK={deployment.params?.topK}
        />
      </>
    );
  }

  if (kind === "chat") return <ChatPlayground project={deployment.id} asleep={asleep} />;
  return <PredictPlayground project={deployment.id} model={model} asleep={asleep} />;
}
