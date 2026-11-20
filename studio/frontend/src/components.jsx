// ===========================================================================
// HIVE STUDIO — SHARED UI PIECES
// ===========================================================================
// Small building blocks used across pages. Anything appearing on more than one
// screen lives here so the console cannot drift into two spellings of the same
// idea — which matters most for the status vocabulary below.
// ===========================================================================

import { useEffect, useState } from "react";
import { ApiError, get } from "./api.js";

// ---------------------------------------------------------------------------
// STATUS PILL
// ---------------------------------------------------------------------------
// ONE CONTROLLED VOCABULARY. Six states, six words, six colours, defined once.
// The wording is user-side per invariant (e): a PI reads "sleeping", not
// "scaled to zero", and certainly not "min-scale 0".
//
// `sleeping` and `deleted` are both grey because both mean "nothing is running"
// and neither is a fault — an on-demand project is asleep most of its life, by
// design. `waking` is amber because the ~15 second cold start is a consequence
// of a local shortcut people must be warned about before they choose it.

const STATUS = {
  provisioning: { label: "Setting up", hint: "Your project is being built." },
  ready: { label: "Ready", hint: "Answering requests now." },
  sleeping: { label: "Sleeping", hint: "Idle, using no GPU. It wakes on the next request." },
  waking: { label: "Waking up", hint: "Starting after being idle — about 15 seconds." },
  failed: { label: "Failed", hint: "Something went wrong. See the steps for which one." },
  deleted: { label: "Removed", hint: "This project has been torn down." },
  // A RUN'S SUCCESS STATE. A batch run does not become "ready" — it finishes,
  // and nothing is left running (DEPLOYMENT_STATES in app/db.py). Without its
  // own entry a finished run fell through to the grey "unknown" pill, which on
  // the one screen where the PI has just got their answers read as a problem.
  completed: {
    label: "Finished",
    hint: "The run is done and its results are ready to download.",
  },
};

export function StatusPill({ status }) {
  const known = STATUS[status];
  if (!known) {
    // An unrecognised status is shown as itself rather than mapped onto a
    // plausible-looking one. Inventing a colour for a state we do not know about
    // is how a console starts lying.
    return <span className="pill pill--sleeping">{status || "unknown"}</span>;
  }
  return (
    <span className={`pill pill--${status}`} title={known.hint}>
      {known.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// MATURITY BADGE
// ---------------------------------------------------------------------------
// Amber for coming-soon, and that is a deliberate use of the reserved colour: a
// template that does not work yet is a way this build differs from the finished
// platform, which is exactly what amber means.

export function MaturityBadge({ maturity }) {
  if (maturity === "available") {
    return <span className="badge badge--green">Available</span>;
  }
  return <span className="badge badge--amber">On the roadmap</span>;
}

// ---------------------------------------------------------------------------
// AMBER BANNER
// ---------------------------------------------------------------------------

export function AmberBanner({ children, bar = false }) {
  return (
    <div className={`banner-amber${bar ? " banner-amber--bar" : ""}`} role="note">
      <span className="banner-amber__icon" aria-hidden="true">
        ▲
      </span>
      <div>{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// LOADING / ERROR / EMPTY
// ---------------------------------------------------------------------------

/** A skeleton that names what it is waiting for. "Loading…" alone says nothing. */
export function Loading({ verb }) {
  return (
    <div className="state" aria-busy="true" aria-live="polite">
      <div className="state__title">{verb}</div>
      <div className="skeleton">
        <div className="skeleton__bar" />
        <div className="skeleton__bar" />
        <div className="skeleton__bar" />
      </div>
    </div>
  );
}

/**
 * A failed fetch: what broke, the detail, and a way to try again.
 *
 * The Retry button matters more than it looks. The most likely error in this
 * console by far is "the backend is not running" — and once someone starts it,
 * the right move is to try again, not to reload the whole page and lose where
 * they were.
 */
export function ErrorState({ error, onRetry }) {
  const status = error instanceof ApiError ? error.status : null;
  return (
    <div className="state state--error" role="alert">
      <div className="state__title">
        {status === 0 ? "Cannot reach the Studio backend" : "Something went wrong"}
      </div>
      <p className="small">{error.message}</p>
      {status === 0 && (
        <p className="small muted">
          Start it with <code>scripts/studio-backend.sh</code>, then try again.
        </p>
      )}
      {onRetry && (
        <button type="button" className="btn btn--ghost btn--sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, children }) {
  return (
    <div className="state">
      <div className="state__title">{title}</div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// useExecutorMode
// ---------------------------------------------------------------------------
// WHICH MODE THE BACKEND IS IN, so the console can stop telling a PI that
// "nothing is really created" while it is creating real namespaces, real
// vClusters and real served models.
//
// READ FROM THE BACKEND, never assumed. The mode is fixed when the backend
// starts and there is no way to know it from the browser except to ask.
//
// ---------------------------------------------------------------------------
// WHY THE ANSWER IS CACHED FOR THE LIFE OF THE TAB
// ---------------------------------------------------------------------------
// The mode CANNOT change while the console is open: it is read once at backend
// startup, so a change means the backend was restarted, which means every
// in-flight request already failed and the page will be reloaded anyway. So one
// fetch per tab, shared between every component that asks — rather than each
// page fetching /api/health for itself and four components racing on mount.
//
// `null` means "not known yet". Callers must treat that as neither mode: a
// component that assumed `mock` while the answer was in flight would flash the
// rehearsal banner on a real deployment, which is the exact lie this exists to
// stop. Every use site below renders nothing mode-specific until it resolves.
let _modePromise = null;
let _modeCache = null;

/**
 * Forget the cached mode. TEST ONLY.
 *
 * Exported for the same reason StepRow and KnowledgeBase are: the cache is
 * correct for a browser tab, where the mode cannot change, and wrong for a test
 * process that has to render both modes one after the other. Rather than weaken
 * the cache to suit the test, the test says explicitly that it is starting a
 * new "tab".
 *
 * Nothing in the console calls this, and nothing should.
 */
export function __resetExecutorMode() {
  _modePromise = null;
  _modeCache = null;
}

export function useExecutorMode() {
  const [mode, setMode] = useState(_modeCache);

  useEffect(() => {
    if (_modeCache) return;
    if (!_modePromise) {
      _modePromise = get("/api/health")
        .then((body) => {
          const answer = body?.executor;
          // ONLY A RECOGNISED MODE IS CACHED. Anything else — a health body
          // without `executor`, a proxy error page that happened to parse, a
          // future backend that renames the field — leaves the cache empty and
          // clears the in-flight promise, so the next component to ask tries
          // again.
          //
          // Caching the failure instead would mean one bad response at page
          // load pins the console to "mode unknown" for the life of the tab,
          // and every mode-specific line stays hidden with nothing to explain
          // why. A retry costs one more request; the alternative costs the
          // feature.
          if (answer === "mock" || answer === "real") {
            _modeCache = answer;
            return answer;
          }
          _modePromise = null;
          return null;
        })
        .catch(() => {
          // A failed health check must not be reported as either mode. Leaving
          // it null means the mode-specific copy stays hidden, which is the
          // safe direction: saying nothing beats saying the wrong thing.
          _modePromise = null;
          return null;
        });
    }
    let cancelled = false;
    _modePromise.then((m) => {
      if (!cancelled) setMode(m);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return mode;
}

// ---------------------------------------------------------------------------
// useFetch
// ---------------------------------------------------------------------------

/**
 * Fetch one URL, with the three states every fetch in this console must have.
 *
 * Returns { data, error, loading, reload }. `reload` is what the Retry button
 * calls.
 *
 * The `cancelled` flag guards the classic React bug: navigating away before a
 * response arrives, then setting state on a component that is gone. Harmless in
 * development, but it also means a slow first request can overwrite a fast
 * second one, showing stale data with no clue why.
 */
export function useFetch(url, verb) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    get(url)
      .then((body) => {
        if (!cancelled) setData(body);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [url, nonce]);

  return {
    data,
    error,
    loading,
    verb,
    reload: () => setNonce((n) => n + 1),
  };
}

/** Render the right one of loading / error / content. */
export function Fetched({ state, children }) {
  if (state.loading) return <Loading verb={state.verb} />;
  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
  return children(state.data);
}
